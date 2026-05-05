# Telegram Bot Commands Design

## Goal
Allow controlling the spa via Telegram bot commands, restricted to authorized chat IDs.

## Architecture
Add a webhook route `POST /telegram/<token>` to Flask. Telegram sends incoming messages to this URL. The app parses commands and replies via `sendMessage`. On startup, register the webhook with Telegram's `setWebhook` API.

## Authorization
`TELEGRAM_CHAT_ID` becomes a comma-separated list. All incoming messages are checked against the allowed list.

## Commands
- `/status` — current temp, desired temp, override status, heating estimate
- `/override` — toggle manual override on/off
- `/heat` — set temp to TEMP_HIGH - 0.5 with 12h override
- `/schedule` — show porssari schedule as text grid

## Env Vars
- `TELEGRAM_WEBHOOK_URL` — base URL (e.g. `https://poreallas.aukia.com`)

## Webhook URL
`{TELEGRAM_WEBHOOK_URL}/telegram/{TELEGRAM_BOT_TOKEN}`

## Changes to send_telegram()
Add optional `chat_id` parameter. When omitted, sends to all configured IDs (for alerts). When provided, sends to that specific chat (for command replies).
