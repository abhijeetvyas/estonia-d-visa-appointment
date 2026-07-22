# Estonia D-Visa Slot Monitor — non-agentic version

This replaces the Gumloop agent with a plain scheduled script. No LLM runs on
any check — GitHub's cron scheduler just executes the Python script directly
and only pings Telegram when a slot actually opens. Zero tokens per run.

## Why this saves your credits

Your Gumloop setup wraps the script in an agent that re-reasons about the
output every 30 minutes ("reply concisely with the result"). That LLM step is
where the tokens go — the script itself was never the expensive part. Running
it as a GitHub Actions cron job removes the agent wrapper entirely.

## Setup (10 minutes, one-time)

1. **Create a private GitHub repo** (e.g. `estonia-visa-monitor`) and push this
   folder's contents to it (including the `.github/workflows` folder).
2. **Get your Telegram bot token and chat ID** (skip if you already have these
   from the Gumloop setup — reuse the same values):
   - Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`,
     copy the token it gives you.
   - Message your new bot once (anything), then visit
     `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser to find
     your `chat.id`.
3. **Add secrets to the repo**: Settings → Secrets and variables → Actions →
   New repository secret.
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Done. The workflow runs automatically every 15 minutes
   (`.github/workflows/estonia-visa-monitor.yml`). You can also trigger it
   manually anytime from the repo's **Actions** tab → "Estonia D-Visa Slot
   Monitor" → **Run workflow**.

## Checking on it

- **Actions tab** in the repo shows every run's status and logs (prints
  `AVAILABLE: ...` or `NOT available: ...`).
- You'll only get a Telegram message when the "Long-stay visa (D-visa)
  application" option actually appears in the booking form's service
  dropdown — not on every check.

## Cost

GitHub Actions free tier includes 2,000 minutes/month for private repos.
Each run takes ~5–10 seconds, so running every 15 minutes (~2,880 runs/month)
costs well under 500 minutes — comfortably free.

## Notes / limitations

- The site's CSRF token is fetched fresh each run (can't be avoided — Estonia's
  booking form requires a valid session token per submission).
- If the site adds a reCAPTCHA challenge on this step later, this script
  (and the Gumloop agent) would both stop working and need a browser-based
  approach (e.g. Playwright) instead of raw `requests`.
- To change frequency, edit the cron expression in the workflow file
  (e.g. `*/30 * * * *` for every 30 minutes).
