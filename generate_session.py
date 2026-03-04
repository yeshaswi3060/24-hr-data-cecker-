"""
Generate a Telegram Session String for server deployment.

Run this LOCALLY once, then copy the output string to your Render environment
variables as TELEGRAM_SESSION_STRING.

Usage:
    python generate_session.py
"""
import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')


async def main():
    print("=" * 55)
    print("  Telegram Session String Generator")
    print("=" * 55)
    print()

    if not API_ID or not API_HASH:
        print("ERROR: Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env")
        return

    print("You will be asked for your phone number and OTP code.")
    print()

    client = TelegramClient(StringSession(), int(API_ID), API_HASH)
    await client.start()

    session_string = client.session.save()

    print()
    print("=" * 55)
    print("  YOUR SESSION STRING (copy everything below):")
    print("=" * 55)
    print()
    print(session_string)
    print()
    print("=" * 55)
    print()
    print("NEXT STEPS:")
    print("  1. Copy the string above")
    print("  2. Go to Render > your service > Environment")
    print("  3. Add: TELEGRAM_SESSION_STRING = <paste string>")
    print()
    print("⚠ KEEP THIS SECRET — it gives full Telegram access!")

    await client.disconnect()


asyncio.run(main())
