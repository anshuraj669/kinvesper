# Kinvesper Telegram bot

Free alerts for the family in Telegram, with buttons to answer them. One bot serves every family. A
caregiver connects their own chat from the dashboard with a single tap. Nobody registers a phone number,
and there's no message template or 24-hour window as there is with WhatsApp.

| In the chat | What happens |
|---|---|
| An alert arrives | 🚨 urgent, ⚠️ warning or ℹ️ info, with **✅ I'm on it** and **📋 Status** buttons |
| Tap **✅ I'm on it** (or send `OK` / `/ok`) | The alert is marked handled on the dashboard, and the button changes to "✅ Handled (Priya)" |
| Tap **📋 Status** (or send `/status`) | Today's doses taken and missed, the last check-in mood, and open alerts |
| Type anything else | It's saved as a family message and read out on the Echo the next time she talks to Alexa |
| Ask her something ("Ask her if she took her pills") | The Echo asks her ("Did you take your pills today?"), and her spoken answer comes back here as a reply to your question |
| `/help`, `/stop` | What the bot can do; stop alerts in this chat |

The backup contact can connect too. They get the urgent alerts nobody answered in time, and the
weekly report goes to the caregiver's chat.

## Files

| File | What it is |
|---|---|
| `bot.json` | The bot's name, descriptions and command menu |
| `setup.py` | Checks the token, applies `bot.json`, stores the token in AWS, sets the webhook and restarts the service (`make telegram`) |
| `poll.py` | For local development: relays the bot to a server on your machine (`make telegram-local`) |

The server side is `src/kinvesper/telegram.py` (Bot API calls and message formatting) and the
Telegram section of `src/kinvesper/service.py` (linking, buttons, replies). Updates arrive at
`POST /api/telegram/webhook`. Telegram sends a secret header with each one, derived from the bot
token, and the server turns away anything without it.

## Set it up (about 2 minutes)

1. In Telegram, open **@BotFather**, send `/newbot`, and choose a name (e.g. "Kinvesper Alerts") and a
   username ending in `bot` (e.g. `kinvesper_alerts_bot`).
2. Put the token it gives you in `.env`, and don't share it anywhere else:
   ```
   TELEGRAM_BOT_TOKEN=1234567890:AA...
   ```
3. Deploy the stack once, so it has the `TelegramSecret`, then run:
   ```
   make telegram
   ```
4. After the restart (about 3 minutes), open the dashboard's **Settings**, press **Connect Telegram**
   under Caregiver, and press **Start** in the chat that opens. Settings shows "Connected"
   by itself. **Send a test alert** to see one arrive.

## Locally

```
uv run python telegram/setup.py --local   # once: the bot's profile and commands
make dev                                  # the server, with TELEGRAM_BOT_TOKEN in .env
make telegram-local                       # in another terminal: relays the bot to it
```

While `poll.py` runs, the deployed server doesn't get the bot's updates, because a bot has only one
delivery route. Stopping it with Ctrl+C restores the webhook.

## How linking works

**Connect Telegram** asks the server for a one-time code (it works for 30 minutes) and opens
`https://t.me/<bot>?start=<code>`. Pressing Start sends `/start <code>` to the bot. The server
then saves that chat as the caregiver's (or the backup contact's) and replies "Connected". Only linked
chats can acknowledge alerts, read the status or send messages to the Echo. Anyone else who finds
the bot is told to connect from Settings.
