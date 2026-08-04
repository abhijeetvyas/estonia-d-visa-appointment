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

The last stretch was about urgency, not reliability. A Telegram message is easy to miss if a slot opens and closes within minutes. So the alerting escalates: a message every single check, uncapped, to multiple people, and a phone call the moment a slot is actually found, because a ringing phone survives silent mode and Do Not Disturb in a way a push notification doesn't. But the call is throttled, capped, and eventually paused for hours at a stretch, because if I haven't picked up after a few tries, calling again immediately isn't help, it's noise.

That throttling logic is a small thing, but it's the part I'm most proud of from a product-thinking angle. It would have been easy to just "add more alerting" until the problem felt solved. The actual work was matching each channel's urgency to how badly I'd want to know and how likely I was to actually notice it, and then explicitly deciding when *more* alerting stops adding value and starts adding noise.

## When detection stopped being enough

The monitor worked. It told me the instant a slot opened. And then, the first time it actually fired, I still lost the slot, because by the time I'd opened my laptop, clicked through the form, and started filling it in, someone else had already taken it. Detection was never actually the hard part. Booking fast enough was.

So the project grew a second stage: a Playwright-driven booker that the monitor triggers the moment it finds a slot, running through the whole flow, embassy, service, applicant details, date and time selection, and submission, without a human in the loop at all. This is a meaningfully different kind of automation than the original script. The original monitor is a single HTTP request and a string match. The booker drives an actual browser through a multi-step form, which meant a whole new category of things could go wrong that a plain `requests` call never has to deal with.

Predictably, the site noticed. Partway through this, a CAPTCHA appeared at exactly the step automation needed to clear, which is its own small lesson: the moment you build something to reliably beat a race condition, you should expect the other side to start defending it.

## The bug that looked like a data problem but wasn't

The first real bug in the booker was a good one, in the sense that it taught me something about *how* to debug with AI rather than just fixing itself. The symptom was simple: the booker would reach the calendar, correctly detect an available date, and then report zero time slots for it, every single time, across three different embassies. The obvious suspect was the parsing logic, maybe the code was reading the wrong HTML attribute for the time labels. That diagnosis was plausible. It was also incomplete.

The actual root cause was two layers deeper: the code was trying to click a calendar day by matching against a locale-formatted string ("August 18, 2026") using an ISO-formatted string ("2026-08-18"), a comparison that could never once succeed, which meant the click silently never fired at all. No exception, no timeout, just a no-op. Layered on top of that, a second piece of code was manually shoving a malformed date value into a hidden form field, which was actively resetting the page and wiping out whatever slots a correct click would have loaded anyway. Neither of those things looks anything like a parsing bug. Both had to be found and fixed before the parsing logic even got a chance to matter.

The part worth calling out: I ran this same debugging problem past a second AI agent independently, and it found the second bug, the malformed hidden-field reset, that the first pass had missed entirely, while missing the first bug that the first pass had caught. Neither diagnosis was wrong. Neither was complete on its own. The lesson wasn't "trust AI less," it was the opposite of what people usually assume: treat one AI's confident diagnosis the same way you'd treat one engineer's, useful, often right, but worth a second independent pass when the stakes justify it, exactly like you'd want a second reviewer on a PR that actually matters.

## Chasing milliseconds without losing sight of the point

Once the booker actually worked end to end, the next question was obvious: how fast is fast enough? A slot has been observed staying open for roughly thirty seconds before someone else grabs it. That number became the real target, not "faster," but "faster than thirty seconds, reliably."

Getting there was a chain of individually small wins that added up to something large. A cold-launched headless browser was costing ten to thirty seconds on its own, so the booker learned to attach to an already-running browser instance instead of launching a fresh one every time. The cloud VM turned out to be quietly throttling itself under sustained load, confirmed directly against its own CPU utilization graph rather than assumed, which meant an upgrade to a machine type without that ceiling. Switching CAPTCHA-solving providers cut typical solve times roughly in half. And the biggest single change was realizing that CAPTCHA-solving didn't need to happen only when the page reached that step, it could start the moment the process began, running in the background while the browser was still loading the very first page. Combined, these took a real end-to-end run from well over a minute down to as fast as thirty-one seconds, with both CAPTCHAs fully solved and hidden behind ordinary page-navigation time.

The part I'd actually want another builder to take from this: every one of those speed changes was designed to fail safe. If the pre-solved CAPTCHA isn't ready yet when needed, the code just waits for it, exactly like before. If a faster browser mode isn't reachable, it falls back to the slower one automatically. None of the optimizations could make things worse than the original baseline, only better or unchanged. That's a very deliberate constraint, and it's the only reason it was safe to keep stacking increasingly aggressive changes on top of each other without the whole thing becoming fragile.

## Almost making an expensive mistake

Not every idea that sounds clever survives contact with how the underlying system actually bills you. One proposal partway through this was to keep a fresh, pre-solved CAPTCHA token sitting in reserve at all times, refreshed every ninety seconds or so, so one would always be instantly available the moment it was actually needed. It sounded like a natural extension of the background-solving idea that had already worked so well.

It wasn't, once actually checked against how these solving services bill. They charge per successfully solved CAPTCHA, not per use, which means a standing reserve refreshed continuously, whether or not a real slot ever appeared that day, would have been a real, ongoing cost for a benefit the system already had for free: the existing approach only ever solves a CAPTCHA when a real detection event actually triggers one. A superficially similar idea, running two different CAPTCHA-solving services at once and keeping whichever finishes first, turned out to be worth doing for the opposite reason: it only costs anything at the moment of a real, rare booking attempt, not continuously, so the extra expense is trivial even though it's real.

The distinction between those two ideas is the whole lesson: "this could make things faster" isn't sufficient justification on its own. The question that actually matters is whether the cost of chasing that speed scales with how often the system does real work, or scales with the clock. One of those is worth paying for. The other one isn't, no matter how clever it sounds in the moment.

## The framework, if I had to name it

Looking back, the actual product thinking underneath all of this was pretty consistent, and it's the part I'd want another PM to take away even if they never touch this specific repo:

**Separate what needs judgment from what doesn't, and only pay for judgment where it's needed.** The agent was right for designing the system. It was wrong for running it every 15 minutes.

**Treat every fix as a hypothesis, not a guess.** Every infrastructure problem in this project got solved by testing a theory against independent evidence rather than iterating blindly on the code.

**Build the failure path before you trust the happy path.** The guardrails, cooldowns, and permanent alert log all came from imagining, and then actually hitting, ways this system could quietly lie to me.

**Escalate urgency deliberately, don't just add more channels.** More alerting isn't automatically better alerting. The goal is matching the channel to the attention it deserves, and knowing when to stop escalating.

**Cost-consciousness is a design constraint, not an afterthought.** The whole project started because token costs on the agentic version were unsustainable for a task that runs constantly. That constraint shaped every architectural decision that followed, right down to rejecting a continuously-refreshed CAPTCHA reserve in favor of one that only spends money when real work is actually happening.

**Every optimization should degrade to safe, never to broken.** Background CAPTCHA-solving, a faster browser attach mode, a real-Chrome experiment, every one of these was built to fall back to the original, known-good behavior the instant it couldn't do the faster thing. That's what made it safe to keep stacking them.

**A second independent opinion is worth getting when the stakes justify it.** One AI's confident diagnosis found half of a real bug. A second one, working independently, found the other half. Treat that the way you'd treat a second reviewer on a change that actually matters, not as redundant effort.

## What's in the repo

The full script, the setup steps, the technical rationale behind every infrastructure decision (why a VM over a cloud scheduler, why this specific free tier over the alternatives, the exact certificate fix, the full command reference for running and debugging it), and the guardrail logic are all public here:

**https://github.com/abhijeetvyas/estonia-d-visa-appointment**

The repo has grown a second layer since the version this article originally described: a Playwright-driven booker that runs on top of the original monitor, complete with CAPTCHA solving, date and time selection, and the speed work described above. `README.md` covers the original detection-and-alerting system; `BOOKER_README.md` covers everything about the automated booking layer, including the machine-sizing notes, the background CAPTCHA-solving setup, and the environment variables that control all of it.

It's set up for Estonia's D-visa booking system specifically, but the embassy and service target are parameterized, so pointing it at a different embassy is a couple of environment variables, not a rewrite. If you're dealing with your own "I need to know the instant this becomes available, and then act on it faster than a human can" problem, whether that's a visa slot, a restaurant reservation, or a sneaker drop, the bones of this should transfer directly.

If you build on top of it, or find a cleaner way to solve any piece of this, I'd genuinely like to hear about it.
