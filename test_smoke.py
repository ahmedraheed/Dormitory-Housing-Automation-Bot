"""
Sanity-check script for dorm_agent.py.
Imports all modules, validates Config env-var loading, and verifies
the Telegram helper wiring without actually calling the live API.
Run: python test_smoke.py
"""
import os
import sys

# NOTE: We must set os.environ BEFORE importing dorm_agent because the Config
# class attributes are evaluated at class-definition time during import.
# However, load_dotenv() inside dorm_agent will still override these with .env values.
# To truly isolate, we override the Config class attributes AFTER import.
os.environ.setdefault("NTFY_TOPIC", "")
os.environ.setdefault("DISCORD_WEBHOOK", "")
os.environ.setdefault("ALERT_EMAIL_FROM", "")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

from dorm_agent import (
    Config,
    send_telegram,
    _send_telegram,
    human_delay,
    Selectors,
    first_visible,
    STEALTH_AVAILABLE,
    _apply_stealth,
    BERLIN_TZ,
)

# Disable all alert channels for testing (override class attrs directly)
Config.BOT_TOKEN = ""
Config.CHAT_ID = ""
Config.NTFY_TOPIC = ""
Config.DISCORD_WEBHOOK = ""
Config.EMAIL_FROM = ""
Config.EMAIL_TO = ""
Config.EMAIL_PASS = ""

print("=" * 60)
print("StwDO Dorm Agent - Smoke Test")
print("=" * 60)

# 1. Config validation
try:
    Config.validate()
    print("[PASS] Config.validate() - all required fields present")
except Exception as e:
    print(f"[FAIL] Config.validate(): {e}")
    sys.exit(1)

# 2. Telegram (no token set -> should return False gracefully)
result = _send_telegram("Test message")
assert result is False, "send_telegram should return False when no token"
print("[PASS] _send_telegram() returns False gracefully with no token")

# 3. human_delay
import time
t0 = time.monotonic()
human_delay(50, 100)
elapsed = (time.monotonic() - t0) * 1000
assert 45 <= elapsed <= 500, f"human_delay out of expected range: {elapsed:.0f}ms"
print(f"[PASS] human_delay() slept {elapsed:.0f}ms")

# 4. Selectors are non-empty lists
for attr in ("APPLY_BUTTONS", "VORNAME", "NACHNAME", "EMAIL", "PHONE", "DATENSCHUTZ", "SUBMIT"):
    val = getattr(Selectors, attr)
    assert isinstance(val, list) and len(val) > 0, f"Selectors.{attr} is empty"
print("[PASS] All Selector lists are non-empty")

# 5. BERLIN_TZ
from datetime import datetime
now = datetime.now(BERLIN_TZ)
print(f"[PASS] Berlin timezone resolved: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")

# 6. playwright-stealth availability info
print(f"[INFO] playwright-stealth available: {STEALTH_AVAILABLE}")

print("=" * 60)
print("All smoke tests passed!")