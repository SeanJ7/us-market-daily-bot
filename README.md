# US Market Daily Bot

This repository generates a Chinese-language US market close report and sends it to Telegram.

## What it does

- pulls market data directly from Yahoo Finance and FRED
- generates a daily markdown report plus an HTML copy
- sends the report to Telegram
- sends a Telegram failure alert if generation fails
- supports GitHub Actions scheduling without relying on a local computer

## Required GitHub secrets

Add these repository secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Schedule behavior

GitHub Actions runs twice per UTC day to handle Melbourne daylight saving changes:

- `21:35 UTC`
- `22:35 UTC`

The script only proceeds when the local `Australia/Melbourne` time is near `08:35`, so only the correct run sends the report.

It is configured to run only on Melbourne local `Tuesday` through `Saturday` mornings, which maps to US market closes from Monday through Friday.

## Manual local run

```bash
cp .env.example .env
# fill in your Telegram values
python3 send_latest_report.py --env-file .env --force
```

## GitHub setup

1. Create an empty GitHub repository named `us-market-daily-bot`
2. Push this folder to that repository
3. Add the two repository secrets
4. Enable GitHub Actions
5. Optionally run the workflow once with `workflow_dispatch`

## Output

Each run writes:

- `reports/YYYY-MM-DD.md`
- `reports/YYYY-MM-DD.html`
- `reports/latest.md`
- `reports/latest.html`
