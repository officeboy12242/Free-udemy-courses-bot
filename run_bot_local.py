#!/usr/bin/env python3
"""
Local bot testing script - starts the bot with all features enabled
"""

import subprocess
import sys
import os

print("\n" + "="*60)
print("TELEGRAM BOT STARTUP - LOCAL TESTING")
print("="*60 + "\n")

print("✅ Checking environment...")
required_vars = ["BOT_TOKEN", "CHANNEL_ID", "MARKET_ALERT_CHAT_ID"]
missing = [v for v in required_vars if not os.getenv(v)]

if missing:
    print(f"❌ Missing environment variables: {', '.join(missing)}")
    print("   Please ensure .env file exists and has these variables")
    sys.exit(1)

print(f"✅ BOT_TOKEN: {os.getenv('BOT_TOKEN')[:20]}...")
print(f"✅ CHANNEL_ID: {os.getenv('CHANNEL_ID')}")
print(f"✅ MARKET_ALERT_CHAT_ID: {os.getenv('MARKET_ALERT_CHAT_ID')}")

print("\n" + "="*60)
print("STARTING BOT...")
print("="*60 + "\n")

print("Available commands:")
print("  /start              - Start message")
print("  /myid               - Your Telegram ID")
print("  /movies             - Get movie buttons")
print("  /search             - Search movies")
print("  /news               - Get latest news")
print("  /market             - Market dip status")
print("  /enroll             - 🆕 Udemy Auto-Enroller")
print("  /enroll_status      - 🆕 View scraped courses")
print("\nPress Ctrl+C to stop the bot\n")

try:
    subprocess.run([sys.executable, "bot_with_healthcheck.py"], check=True)
except KeyboardInterrupt:
    print("\n\n❌ Bot stopped by user")
    sys.exit(0)
except Exception as e:
    print(f"\n❌ Error: {e}")
    sys.exit(1)
