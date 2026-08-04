# How I used AI to build a visa slot monitor (and relearned a few product lessons along the way)

A few weeks ago I needed an Estonia D-visa appointment slot that simply wasn't opening up. Not a rare problem if you've ever tried to book a government appointment anywhere in the world. The dropdown that's supposed to show "Long-stay visa (D-visa) application" just kept showing one lonely option: pickup of an e-resident's digital ID. Nothing else.

The obvious fix was to check the booking page constantly. The less obvious, more interesting problem was: how do you actually build "check constantly" in a way that's cheap, reliable, and doesn't need babysitting? That question turned into a small side project that taught me more about product thinking than most actual sprints do. This is the story of how it evolved, and where AI (Claude, specifically) fit into every stage of it.

The full technical setup, the rationale behind every tool choice, and the exact commands to reproduce this are all in the repo's README, linked at the end. This is the story behind those decisions, not a repeat of them.

## The first version was wrong, and that was useful

My first instinct was to use an agent. I built a Gumloop flow that ran on a schedule, hit the booking page, parsed the result, and pinged me on Telegram if the slot opened up. It worked. It also burned through credits fast, because every single scheduled run spun up an LLM to interpret the output and decide what to do with it, even though the actual task never changed. Check the page. Look for one specific string. Send a message if found. That's not a task that needs judgment every time. It needs judgment exactly once, at design time, and then a dumb, reliable, repeatable execution after that.

This is the first product lesson that got reinforced hard: agents are for tasks where the next step depends on reasoning about what just happened. If the logic is the same every single time, you don't want an agent in the loop at runtime, you want an agent in the loop at *build* time, writing you a script that then runs without needing to think again. I asked Claude directly whether this was doable non-agentically, and that reframing is what kicked off the whole rest of the project.

## Getting something simple to actually run reliably

The script itself was the easy part. Claude wrote it in one pass, and we tested the actual request flow live against the real site to confirm the logic held up before trusting it.

What took real effort was making it run reliably, unattended, for days at a time. That journey went through a few distinct failure modes, each one teaching me something different about infrastructure I'd never had to think about as a PM: a scheduler that quietly doesn't run when it says it will, a network path that gets treated with suspicion depending on where it originates from, and a certificate chain problem that only shows up depending on which HTTP client is asking. None of these were code bugs. All of them were the kind of thing you only learn by hitting them.

The part worth calling out specifically is *how* we found the root cause each time, because it's the clearest example in the whole project of what a good AI collaborator actually does differently from a code generator. It didn't just throw fixes at the wall. It formed a hypothesis, found a way to test that hypothesis independently of the code itself (a live browser session, a direct diagnostic command against the server, a system-level log check), updated the hypothesis when a fix didn't work, and kept narrowing until the actual cause was confirmed rather than guessed at. That loop, hypothesis, independent test, revise, repeat, is the same loop I'd want out of a good engineer, and it's the difference between AI as autocomplete and AI as a thinking partner.

## Making it boring on purpose

Once the checks were reliable, the next phase was making failure boring instead of silent. A monitor that quietly stops working is worse than no monitor, because it gives you false confidence. So the system grew a whole layer of guardrails: retries before treating anything as a real failure, a distinctly worded failure alert so I can never confuse "the slot opened" with "the monitor broke," a cooldown so an extended outage sends one alert instead of spamming, and a permanent record of every real event that survives even if I clear the working log during future debugging.

That last one exists because of a mistake, not foresight. I lost a real "it's available" log entry to my own debugging session at one point. Instead of just noting it and moving on, that became a design requirement: a separate file that only records real events and that I'm not allowed to casually wipe. That's a very PM instinct applied to infrastructure, turn an incident into a permanent constraint, not just a lesson learned.

None of this is exciting engineering. All of it is the actual work of running something in production, even a tiny personal one. The AI's role here shifted again, from "solve a hard technical mystery" to "help me think like an SRE for a system that matters to exactly one person."

## From a text message to something you can't ignore

The last stretch of the first phase was about urgency, not reliability. A Telegram message is easy to miss if a slot opens and closes within minutes. So the alerting escalates: a message every single check, uncapped, to multiple people, and a phone call the moment a slot is actually found, because a ringing phone survives silent mode and Do Not Disturb in a way a push notification doesn't. But the call is throttled, capped, and eventually paused, because if I haven't picked up after a few tries, calling again immediately isn't help, it's noise.

That throttling logic is a small thing, but it's the part I'm most proud of from a product-thinking angle. It would have been easy to just "add more alerting" until the problem felt solved. The actual work was matching each channel's urgency to how badly I'd want to know and how likely I was to actually notice it, and then explicitly deciding when *more* alerting stops adding value and starts adding noise.

## When detection stopped being enough

The monitor worked. It told me the instant a slot opened. And then, the first time it actually fired, I still lost the slot, because by the time I'd opened my laptop, clicked through the form, and started filling it in, someone else had already taken it. Detection was never actually the hard part. Booking fast enough was.

So the project grew a second stage: a Playwright-driven booker that the monitor triggers the moment it finds a slot, running through the whole flow, embassy, service, applicant details, date and time selection, and submission, without a human in the loop at all.

Predictably, the site noticed. Partway through this, a CAPTCHA appeared at exactly the step automation needed to clear, which is its own small lesson: the moment you build something to reliably beat a race condition, you should expect the other side to start defending it.

## The bug that looked like a data problem but wasn't

The first real bug in the booker was a good one, in the sense that it taught me something about *how* to debug with AI rather than just fixing itself. The symptom was simple: the booker would reach the calendar, correctly detect an available date, and then report zero time slots for it, every single time, across three different embassies. The obvious suspect was the parsing logic. That diagnosis was plausible. It was also incomplete.

The actual root cause was two layers deeper: the code was trying to click a calendar day by matching a locale-formatted string ("August 18, 2026") against an ISO-formatted one ("2026-08-18"), a comparison that could never once succeed, which meant the click silently never fired at all. No exception, no timeout, just a no-op. Layered on top, a second piece of code was shoving a malformed date into a hidden form field, actively resetting the page and wiping out whatever slots a correct click would have loaded.

The part worth calling out: I ran this same debugging problem past a second AI agent independently, and it found the second bug that the first pass had missed entirely, while missing the first bug that the first pass had caught. Neither diagnosis was wrong. Neither was complete on its own. The lesson wasn't "trust AI less," it was the opposite of what people usually assume: treat one AI's confident diagnosis the same way you'd treat one engineer's, useful, often right, but worth a second independent pass when the stakes justify it.

## Chasing milliseconds without losing sight of the point

Once the booker worked end to end, the question was: how fast is fast enough? A slot had been observed staying open for roughly thirty seconds before someone else grabbed it. That number became the target.

Getting there was a chain of small wins. A cold-launched headless browser was costing ten to thirty seconds on its own, so the booker learned to attach to an already-running instance. The cloud VM turned out to be throttling itself under sustained load, confirmed against its own CPU graph rather than assumed, which meant a machine upgrade. Switching CAPTCHA providers roughly halved solve times. And the biggest single change was realising CAPTCHA-solving didn't need to wait until the page reached that step, it could start the moment the process began, running in the background while the browser was still loading the first page. Combined, these took a real run from over a minute down to as little as thirty-one seconds.

The part I'd want another builder to take from this: every one of those changes was designed to fail safe. If the pre-solved CAPTCHA isn't ready, the code just waits, exactly like before. If a faster browser mode isn't reachable, it falls back automatically. None of them could make things worse than the baseline, only better or unchanged. That constraint is the only reason it was safe to keep stacking increasingly aggressive changes on top of each other.

And then I measured the whole thing properly, and discovered most of that work had been optimising the wrong layer entirely.

## The browser was never necessary

Before optimising further, I did something I should have done at the very start: I instrumented the browser to log every single HTTP request it made, and looked at what was actually going over the wire.

The entire booking flow turned out to be five ordinary form POSTs to a single URL with a rotating security token. No JavaScript execution required. No dynamic rendering. The date picker that had caused the nastiest bug in the project was, underneath, one POST with a date string in it. The browser wasn't doing anything except *producing those five requests* — and doing it slowly, since roughly twelve of the twenty-five seconds per run was browser overhead.

I rewrote the flow as plain HTTP calls. Total time for all the actual work: about two and a half seconds. The same booking, without a browser, was ten times faster on everything except the CAPTCHA.

The lesson is uncomfortable and general: I had spent weeks optimising a tool without checking whether the tool was needed. Warming the browser, upgrading the VM to handle the browser, working around the browser's date picker — all of it was real, careful work solving problems that only existed because of a choice nobody had revisited since day one. Instrumenting first would have skipped every one of those problems. "Measure before you optimise" is advice everyone nods along to; what it actually means in practice is *measure what your tools are doing, not just how long they take*.

## The measurement that was confidently wrong

Once the browser was gone, a new architecture became possible. Getting past the CAPTCHA takes twenty seconds, but the session it produces can then check for free slots instantly, for free. So instead of racing through the form each time, the system could pay that cost once and then just *sit* on the booking page, watching — what I started calling holding a seat.

The obvious question was how long a seat lasts. We measured it: exactly 1,440 seconds. Twenty-four minutes, dead on. And 1,440 happens to be PHP's default session lifetime, which made it feel not just measured but *explained*. We designed renewal timing around it and moved on.

It was wrong. Later runs at a faster polling rate died after 190 seconds, five times in a row, within three seconds of each other every time. Same code, same site, wildly different lifetime.

Counting requests instead of seconds made it obvious immediately. The first measurement polled every fifteen seconds and died on poll 94. The later ones polled every two seconds and died at around request 100. The session doesn't expire on a clock at all. It expires after roughly a hundred requests. The 1,440 figure was an artifact of the polling interval we happened to choose, and the PHP coincidence was exactly that — a coincidence that made a wrong conclusion look well-supported.

That one stung, and it's the most transferable lesson in the whole project. **One measurement plus a plausible mechanism feels like proof and isn't.** The mechanism is what made it dangerous: without the PHP detail we'd have held the number loosely, but a tidy explanation converted a single data point into a fact nobody re-examined. The fix wasn't just changing the threshold, it was making the system record its own request count on every poll, so the real ceiling gets measured continuously across many runs instead of inferred once and trusted forever.

## Almost making an expensive mistake, and then making it on purpose

Earlier in the project I'd rejected an idea: keeping a pre-solved CAPTCHA token in reserve at all times, refreshed continuously, so one would always be ready. The reasoning was that these services bill per solved CAPTCHA, so a standing reserve costs money on a clock whether or not a slot ever appears, while solving on demand only costs money when real work happens. Costs that scale with the clock are worse than costs that scale with usage. I still think that reasoning was correct.

Then I built exactly that reserve anyway, and it was the single biggest speed improvement in the project.

What changed wasn't the economics. It was where the bottleneck sat. When I rejected the reserve, CAPTCHA-solving was hidden behind browser startup and page loads, so pre-solving bought almost nothing. Once the browser was gone, the CAPTCHA *was* the critical path — twenty seconds out of a twenty-two second run. The same idea went from marginal to decisive because everything around it had changed.

One detail mattered more than it looks. The obvious version keeps one token warm. That makes getting the seat instant and then leaves the actual booking stranded, because the booking needs a *second* token two seconds later and single-use tokens can't be reused. You'd watch the slot arrive twenty seconds sooner and still lose it. The reserve had to hold two.

The lesson isn't "I was wrong to reject it." It's that a good architectural decision has a shelf life, and the thing that expires it is usually a change somewhere else entirely. Worth revisiting rejected ideas whenever the shape of the system changes — not to second-guess yourself, but because the premise may simply no longer hold.

## The guard that became the bug

There's a failure mode I hit three separate times in this project, in three different disguises, and I now think it's one of the most common ways careful engineering goes wrong.

The first was a ninety-second cooldown after each booking attempt, added early to stop the booker stampeding. Sensible — except availability windows turned out to last ninety to a hundred and ten seconds, so one failed attempt burned the entire remaining window. The logs show it refusing to run, three times in a row, while a slot was actively being advertised.

The second was a detector I designed to notice when the parked seat had gone stale. My condition was "the date list came back unchanged." In practice an unchanged date list is the *normal* state — the same date persists for an entire window. The detector would have fired on every trigger, thrown away a working seat each time, and spent the whole window rebuilding sessions instead of booking.

The third was a rate cap of sixty CAPTCHA solves per hour, added as a runaway-spend guard. Maintaining a two-token reserve costs about sixty solves an hour by itself. Left alone, the safety mechanism would have throttled the feature it was guarding, silently, and made everything slower than before.

Same mistake every time: **a threshold chosen for an event I assumed was rare, applied to one that turned out to be routine.** The tell is always the same — the guard fires constantly instead of occasionally. Worth asking, of every limit you add, "what happens if this fires on every single cycle?" If the answer isn't "it stops something genuinely bad," the threshold is wrong.

## The confirmation that couldn't confirm

One morning the system finally found a real slot, selected it, and clicked book. And then reported that it couldn't tell whether the booking had worked.

It hadn't. But the more interesting problem was that it *couldn't have known* either way. The success check looked for a date rendered as `2026.08.05` on a site that renders `05.08.2026` — a comparison that could never match, sitting quietly in the code the whole time. And there was no negative check at all, so "no success marker found" was indistinguishable from the page saying "Booking failed" in plain text.

The fix used the confirmation page's own structure — a cancel link that only exists when a booking exists — as the primary signal, and made the failure string an explicit override. But the real fix was philosophical: the check is now deliberately biased toward *pessimism*. A false "confirmed" would stop the system hunting for a booking that doesn't exist. A false "unclear" just sends me a message asking me to check. When the two error directions have wildly different costs, the check should lean hard toward the cheap one.

Two smaller habits came out of the same morning. The system now saves the page after every submit, success or failure — because when this happened there was no artifact of any kind to look at, and an hour went into reconstructing what might have occurred. And it stores the cancel link with the booking, which turns "did it work?" into something you can verify in one click.

## The tests that lied in the logs

A small one, but it cost real time. Midway through investigating a live availability window, the logs contained a booking attempt for a slot ID that didn't exist, a session death that hadn't happened, and a confirmation page saved to a file called `fake_dump.html`.

The unit tests were writing into the production log and dumps directory. They'd been doing it for days, harmlessly, right up until the moment I was reading those logs urgently and had to work out which entries were real.

Test output that's indistinguishable from production output isn't a cosmetic problem. It's a diagnostic problem, and it surfaces at exactly the worst time. Tests now write to a throwaway directory.

## Shipping is not the same as building

Here's the one I'd most want another PM to sit with.

Over one long session I designed a new architecture, wrote it, fixed five rounds of bugs in it, tested each round, and documented all of it. It was good work. Then two real slots appeared, and neither was handled by any of it — because I had never actually deployed it. The service file sat in the repo, uninstalled. Every test had been run by hand. The thing still handling real events was the old, slow version I'd spent the entire session replacing.

Nothing was broken. Nothing alerted. The gap between "the code is finished and tested" and "the system is doing this" was invisible from inside the work, because from inside the work it genuinely felt done.

Everything I'd been doing was legible as progress — commits, tests passing, changelog entries. Deployment was the one step that produced no artifact except a change in the world, and it was the only step that mattered. **The last mile has no dopamine, which is exactly why it gets skipped.** I now treat "is it actually running, in production, right now?" as a question with its own explicit check, not something implied by the work being complete.

## Where it landed

The end state, measured against a real booking on a live site: trigger to confirmed appointment in **five seconds**. Two of those are acquiring the session, one is finding the slot, one is submitting. It was twenty-five seconds this morning and over a minute a week ago.

Of that five seconds, almost none is now spent on anything we control. The remaining cost is network round trips to a government server. There's no obvious next order of magnitude here, which is a good place for an optimisation project to end.

And the honest ending, because the useful version of this story includes it: **I still don't have my appointment.**

Everything above works. It's deployed, it's fast, it's verified end to end against a real booking on a test embassy. But the specific embassy I need has, over three days of watching, advertised a bookable time exactly once. Every other time the calendar offers a date and then reports zero available times for it — confirmed three independent ways, including a session that was already sitting on the page and checking every two seconds.

So the remaining question isn't a speed question anymore. It's whether those slots exist at all in the window we can see them, and no amount of engineering answers that. What the system does give me now is visibility: a message showing exactly what the calendar offered, every time it changes, so I can watch the pattern instead of inferring it from failures.

That's a genuinely useful place to be, and it's not the place I expected to end up. The work made the system fast enough to be *sure* speed isn't the problem — which is a real result, just not the one I was aiming at.

## The framework, if I had to name it

**Separate what needs judgment from what doesn't, and only pay for judgment where it's needed.** The agent was right for designing the system. It was wrong for running it every fifteen minutes.

**Measure what your tools are doing, not just how long they take.** Weeks of browser optimisation solved problems that only existed because nobody had checked whether the browser was needed. It wasn't.

**One measurement plus a plausible mechanism is not proof.** A session lifetime we'd "confirmed" and explained turned out to be an artifact of how often we happened to be asking. A tidy explanation is what stops people re-examining a number.

**Ask what happens if your guard fires every cycle.** Three separate limits in this project were sized for rare events and applied to routine ones. Each would have broken the thing it was protecting.

**Bias your checks toward the cheaper error.** A false "booking confirmed" stops everything. A false "unclear" sends a message. Those are not symmetrical, and the check shouldn't treat them as if they are.

**Revisit rejected ideas when the system's shape changes.** A standing CAPTCHA reserve was genuinely the wrong call when I rejected it, and genuinely the right one two weeks later. Nothing about the economics changed — the bottleneck moved.

**Every optimisation should degrade to safe, never to broken.** That constraint is what made it possible to keep stacking aggressive changes without the whole thing turning fragile.

**A second independent opinion is worth getting when the stakes justify it.** One AI's confident diagnosis found half a bug. A second, working independently, found the other half.

**"Done and tested" is not "running."** Track deployment as its own explicit state. It's the only step that produces no artifact except a change in the world.

## What's in the repo

The full system, the setup steps, the technical rationale behind every infrastructure decision, and the guardrail logic are public here:

**https://github.com/abhijeetvyas/estonia-d-visa-appointment**

There are now three layers. `README.md` covers the original detection-and-alerting monitor, which is unchanged and still the safety net — if everything else fails, it still calls my phone. `BOOKER_README.md` covers the original Playwright booker plus the parked-session daemon that replaced it, including the request-budget findings and every environment variable. `AI_CONTEXT.md` is written for an AI picking this project up cold: exhaustive, explicitly including the wrong turns and superseded conclusions, because the failed hypotheses turned out to be more valuable to a future reader than the working code.

It's set up for Estonia's D-visa system, but the embassy and service are parameterised, so pointing it elsewhere is a couple of environment variables rather than a rewrite. If you're dealing with your own "I need to know the instant this becomes available, and act on it faster than a human can" problem — a visa slot, a restaurant reservation, a sneaker drop — the bones should transfer directly.

If you build on top of it, or find a cleaner way to solve any piece of this, I'd genuinely like to hear about it.
