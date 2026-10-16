"""Relay the Telegram bot to a local Kinvesper server (no public https address needed).

    make telegram-local            # or: uv run python telegram/poll.py [--server http://127.0.0.1:8000]

Run the server with the same TELEGRAM_BOT_TOKEN in .env. This fetches the bot's updates from Telegram
(long polling) and posts each one to the local /api/telegram/webhook, just as Telegram would.

A bot has one delivery route at a time, so while this runs the deployed server gets nothing. The
webhook it found is put back when you stop it (Ctrl+C).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from setup import bot, env_file, token, webhook_secret


def main() -> None:
    port = os.environ.get("PORT") or env_file().get("PORT", "8000")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--server", default=f"http://127.0.0.1:{port}")
    args = parser.parse_args()
    t = token()
    secret = webhook_secret(t)
    me = bot(t, "getMe")
    previous = bot(t, "getWebhookInfo").get("url", "")
    if previous:
        bot(t, "deleteWebhook")
        print(f"Paused the webhook ({previous}); it comes back when you stop this.")
    hook = f"{args.server.rstrip('/')}/api/telegram/webhook"
    print(f"Relaying @{me['username']} to {hook}. Ctrl+C to stop.")
    offset = 0
    try:
        with httpx.Client(timeout=40) as http:
            while True:
                try:
                    r = http.post(f"https://api.telegram.org/bot{t}/getUpdates",
                                  json={"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]})
                    updates = r.json().get("result", [])
                except (httpx.HTTPError, ValueError):
                    continue
                for u in updates:
                    offset = u["update_id"] + 1
                    kind = "button" if "callback_query" in u else "message"
                    try:
                        status = http.post(hook, json=u, headers={"X-Telegram-Bot-Api-Secret-Token": secret}).status_code
                    except httpx.HTTPError as e:
                        status = f"failed ({type(e).__name__}: is the server running?)"
                    print(f"  {kind} -> {status}")
    except KeyboardInterrupt:
        pass
    finally:
        if previous:
            bot(t, "setWebhook", url=previous, secret_token=secret, allowed_updates=["message", "callback_query"])
            print(f"\nWebhook restored: {previous}")


if __name__ == "__main__":
    main()
