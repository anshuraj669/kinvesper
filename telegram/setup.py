"""Connect the Kinvesper Telegram bot to the deployed server.

    make telegram                  # or: uv run python telegram/setup.py
    uv run python telegram/setup.py --local      # only the bot's profile and commands (no AWS)

Reads TELEGRAM_BOT_TOKEN from .env (or asks), then:
1. checks the token with Telegram (getMe),
2. gives the bot its description and commands from telegram/bot.json,
3. stores the token in AWS Secrets Manager (stack output TelegramSecret),
4. points the bot's webhook at <ServiceUrl>/api/telegram/webhook, with a secret header,
5. restarts the App Runner service so it reads the token.

The token is never printed or passed on a command line.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
BOT = json.loads((Path(__file__).parent / "bot.json").read_text())
TOKEN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def env_file() -> dict[str, str]:
    values = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if m := re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line):
                values[m[1]] = re.sub(r"\s+#.*$", "", m[2]).strip().strip("'\"")
    return values


def token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN") or env_file().get("TELEGRAM_BOT_TOKEN", "")
    if not t:
        t = getpass.getpass("Telegram bot token from @BotFather: ").strip()
    if not TOKEN.match(t):
        sys.exit("error: that doesn't look like a bot token (digits, a colon, then about 35 letters). Get one from @BotFather.")
    return t


def webhook_secret(t: str) -> str:
    """Must match kinvesper.telegram.webhook_secret."""
    return hmac.new(t.encode(), b"kinvesper-telegram-webhook", hashlib.sha256).hexdigest()


def bot(t: str, method: str, **params) -> dict:
    r = httpx.post(f"https://api.telegram.org/bot{t}/{method}", json=params, timeout=15)
    data = r.json()
    if not data.get("ok"):
        sys.exit(f"error: Telegram {method}: {data.get('description', r.status_code)}")
    return data["result"]


def profile(t: str) -> str:
    me = bot(t, "getMe")
    print(f"Bot: @{me['username']} ({me['first_name']})")
    bot(t, "setMyDescription", description=BOT["description"])
    bot(t, "setMyShortDescription", short_description=BOT["short_description"])
    bot(t, "setMyCommands", commands=BOT["commands"])
    if me["first_name"] != BOT["name"]:
        try:
            bot(t, "setMyName", name=BOT["name"])
        except SystemExit as e:  # rate-limited when changed often; the name is cosmetic
            print(f"  (name not changed: {e})")
    print("Gave the bot its description and commands (/status, /ok, /help, /stop).")
    return me["username"]


def aws(t: str, stack: str, region: str) -> str:
    import boto3

    cfn = boto3.client("cloudformation", region_name=region)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])}
    if "TelegramSecret" not in outputs:
        sys.exit("error: the stack has no TelegramSecret output yet: redeploy it first (cd infra && cdk deploy ...)")
    boto3.client("secretsmanager", region_name=region).put_secret_value(SecretId=outputs["TelegramSecret"], SecretString=t)
    print("Stored the token in Secrets Manager.")
    url = outputs["ServiceUrl"].rstrip("/") + "/api/telegram/webhook"
    bot(t, "setWebhook", url=url, secret_token=webhook_secret(t), allowed_updates=["message", "callback_query"])
    print(f"Webhook: {url}")
    boto3.client("apprunner", region_name=region).start_deployment(ServiceArn=outputs["ServiceArn"])
    print("Restarting the service so it reads the token (about 3 minutes)...")
    return outputs["ServiceUrl"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--local", action="store_true", help="only set the bot's profile; no AWS (use telegram/poll.py)")
    parser.add_argument("--stack", default=os.environ.get("STACK", "Kinvesper"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    args = parser.parse_args()
    t = token()
    name = profile(t)
    if args.local:
        print("Now run: make telegram-local   (relays the bot to your local server)")
        return
    service = aws(t, args.stack, args.region)
    print(f"\nDone. Once it's running, open {service}/dashboard/settings and press Connect Telegram.\n"
          f"The bot: https://t.me/{name}")


if __name__ == "__main__":
    main()
