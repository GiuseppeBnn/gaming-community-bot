#!/usr/bin/env python3
"""
One-time Telethon login → prints a StringSession for the /backup chat archive.

The Bot API cannot read chat history, so the archive uses an MTProto *user*
session. Run this ONCE, interactively, then paste the printed string into the
bot's .env as TELEGRAM_SESSION (alongside TELEGRAM_API_ID / TELEGRAM_API_HASH
from https://my.telegram.org → API development tools).

    python scripts/login_telethon.py

SECURITY: the printed session string grants FULL access to that Telegram
account. Use a dedicated admin account, never commit it, and treat it like a
password. The bot only ever reads message history with it.
"""

from __future__ import annotations

import os
import sys

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    sys.exit("Telethon non installato. Esegui: pip install telethon")


def _ask(prompt: str, env_key: str) -> str:
    val = os.environ.get(env_key)
    if val:
        return val
    return input(prompt).strip()


def main() -> None:
    api_id = int(_ask("api_id: ", "TELEGRAM_API_ID"))
    api_hash = _ask("api_hash: ", "TELEGRAM_API_HASH")

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_string = client.session.save()
        me = client.get_me()
        print("\n✅ Login riuscito come:", getattr(me, "username", None) or me.first_name)
        print("\n--- Copia questa riga nella .env (NON committarla) ---")
        print(f"TELEGRAM_SESSION={session_string}")
        print("------------------------------------------------------")


if __name__ == "__main__":
    main()
