"""
StwDO Dorm Monitor Bot
======================
Continuously monitors the Studierendenwerk Dortmund housing portal
(https://www.stwdo.de/wohnen/aktuelle-wohnangebote) for newly listed
dormitory rooms and auto-submits applications with sub-second speed.

Author  : Open-source community
License : MIT
Python  : 3.11+

Features
--------
- High-frequency polling with configurable interval
- playwright-stealth anti-bot evasion
- Realistic human-like interaction timing
- Exponential backoff on transient network errors
- Telegram push notifications (listing found / applied / error)
- Graceful shutdown on timeout or keyboard interrupt

Usage
-----
    pip install -r requirements.txt
    playwright install chromium --with-deps
    cp .env.example .env          # then fill in your credentials
    python dorm_agent.py
"""

from __future__ import annotations

import os
import sys
import time
import random
import logging
import traceback
from datetime import datetime, timedelta
from typing import Optional, List

import pytz
import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from playwright.sync_api import (
    sync_playwright,
    Page,
    Browser,
    BrowserContext,
    Playwright,
    TimeoutError as PWTimeoutError,
)

# playwright-stealth v1 uses stealth_sync; v2 uses Stealth class / stealth()
try:
    try:
        from playwright_stealth import stealth_sync as _stealth_fn  # v1
        def _apply_stealth(page):
            _stealth_fn(page)
    except ImportError:
        from playwright_stealth import Stealth  # v2
        def _apply_stealth(page):
            Stealth().apply_stealth_sync(page)
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False
    def _apply_stealth(page):
        pass  # no-op when library not installed

# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()

# Rich console for pretty terminal output
console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, markup=True)],
)
log = logging.getLogger("dorm_agent")

# Fix Windows console encoding so Unicode characters render correctly
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

BERLIN_TZ = pytz.timezone("Europe/Berlin")


# ─────────────────────────────────────────────────────────────────────────────
# Configuration — loaded from .env
# ─────────────────────────────────────────────────────────────────────────────

class Config:
    """All runtime configuration sourced from environment variables with safe defaults."""

    # Applicant data (reads environment, falling back to registered details)
    FIRST_NAME: str     = os.getenv("APPLICANT_FIRST_NAME", "").strip() or "Ahmed"
    LAST_NAME: str      = os.getenv("APPLICANT_LAST_NAME", "").strip() or "Rasheed"
    EMAIL: str          = os.getenv("APPLICANT_EMAIL", "").strip() or "ahmed.rasheed@tu-dortmund.de"
    PHONE: str          = os.getenv("APPLICANT_PHONE", "").strip() or "+3089876647"
    MATRIKEL: str       = os.getenv("APPLICANT_MATRIKEL", "").strip() or "285351"
    UNIVERSITY: str     = os.getenv("APPLICANT_UNIVERSITY", "").strip() or "TU Dortmund"

    # Telegram (optional)
    BOT_TOKEN: str      = os.getenv("TELEGRAM_BOT_TOKEN", "")
    CHAT_ID: str        = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Alternative alert channels (pick any one or more) ────────────────────
    # ntfy.sh  — easiest, no account needed (just install ntfy app)
    NTFY_TOPIC: str     = os.getenv("NTFY_TOPIC", "").strip() or "ahmed-dorm-285351"          # e.g. ahmed-dorm-2024
    NTFY_SERVER: str    = os.getenv("NTFY_SERVER", "https://ntfy.sh")  # or self-hosted

    # Discord webhook  — paste webhook URL from Discord channel settings
    DISCORD_WEBHOOK: str = os.getenv("DISCORD_WEBHOOK", "")

    # Gmail / SMTP email alert
    EMAIL_FROM: str     = os.getenv("ALERT_EMAIL_FROM", "")     # your gmail
    EMAIL_TO: str       = os.getenv("ALERT_EMAIL_TO", "")       # where to send
    EMAIL_PASS: str     = os.getenv("ALERT_EMAIL_PASS", "")     # gmail app password

    # URLs
    LISTINGS_URL: str   = os.getenv(
        "LISTINGS_URL",
        "https://www.stwdo.de/wohnen/aktuelle-wohnangebote",
    )

    # Polling
    POLL_INTERVAL: int  = int(os.getenv("POLLING_INTERVAL_SECONDS", "30"))
    MAX_RUNTIME_MIN: int = int(os.getenv("MAX_RUNTIME_MINUTES", "90"))

    # Browser
    HEADLESS: bool      = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")

    # Human-like delays (ms → seconds used internally)
    MIN_DELAY_MS: int   = int(os.getenv("MIN_ACTION_DELAY_MS", "300"))
    MAX_DELAY_MS: int   = int(os.getenv("MAX_ACTION_DELAY_MS", "1200"))

    @classmethod
    def validate(cls) -> None:
        """Raise if mandatory values are missing."""
        # Ensure core values are non-empty
        if not cls.FIRST_NAME: cls.FIRST_NAME = "Ahmed"
        if not cls.LAST_NAME:  cls.LAST_NAME  = "Rasheed"
        if not cls.EMAIL:      cls.EMAIL      = "ahmed.rasheed@tu-dortmund.de"
        if not cls.PHONE:      cls.PHONE      = "+3089876647"
        # Check that at least ONE alert channel is configured
        has_telegram = bool(cls.BOT_TOKEN and cls.CHAT_ID)
        has_ntfy     = bool(cls.NTFY_TOPIC)
        has_discord  = bool(cls.DISCORD_WEBHOOK)
        has_email    = bool(cls.EMAIL_FROM and cls.EMAIL_TO and cls.EMAIL_PASS)
        if not any([has_telegram, has_ntfy, has_discord, has_email]):
            log.warning(
                "No alert channel configured! Set at least one of: "
                "NTFY_TOPIC, DISCORD_WEBHOOK, "
                "ALERT_EMAIL_FROM/TO/PASS, or TELEGRAM_BOT_TOKEN/CHAT_ID"
            )
        else:
            active = []
            if has_ntfy:     active.append("ntfy.sh")
            if has_discord:  active.append("Discord")
            if has_email:    active.append("Email")
            if has_telegram: active.append("Telegram")
            log.info(f"Alert channels active: {', '.join(active)}")


# ─────────────────────────────────────────────────────────────────────────────
# Alert channels — all are optional, any combination works
# ─────────────────────────────────────────────────────────────────────────────

import smtplib
import email.mime.text as _mime_text


def _strip_html(text: str) -> str:
    """Remove HTML tags for plain-text channels."""
    import re
    return re.sub(r"<[^>]+>", "", text).strip()


def _send_ntfy(message: str) -> bool:
    """Push via ntfy.sh — free, no account, just install the app."""
    if not Config.NTFY_TOPIC:
        return False
    url = f"{Config.NTFY_SERVER.rstrip('/')}/{Config.NTFY_TOPIC}"
    try:
        resp = requests.post(
            url,
            data=_strip_html(message).encode("utf-8"),
            headers={
                "Title": "StwDO Dorm Alert",
                "Priority": "urgent",
                "Tags": "house,bell",
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.debug("ntfy.sh notification sent.")
        return True
    except requests.RequestException as exc:
        log.warning(f"ntfy send failed: {exc}")
        return False


def _send_discord(message: str) -> bool:
    """Push via Discord webhook."""
    if not Config.DISCORD_WEBHOOK:
        return False
    try:
        resp = requests.post(
            Config.DISCORD_WEBHOOK,
            json={"content": f"🏠 **StwDO Alert**\n{_strip_html(message)}"},
            timeout=10,
        )
        resp.raise_for_status()
        log.debug("Discord notification sent.")
        return True
    except requests.RequestException as exc:
        log.warning(f"Discord webhook failed: {exc}")
        return False


def _send_email(message: str) -> bool:
    """Send alert via Gmail SMTP (use an App Password, not your main password)."""
    if not (Config.EMAIL_FROM and Config.EMAIL_TO and Config.EMAIL_PASS):
        return False
    try:
        msg = _mime_text.MIMEText(_strip_html(message), "plain", "utf-8")
        msg["Subject"] = "🏠 StwDO Dorm Alert"
        msg["From"]    = Config.EMAIL_FROM
        msg["To"]      = Config.EMAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(Config.EMAIL_FROM, Config.EMAIL_PASS)
            smtp.sendmail(Config.EMAIL_FROM, Config.EMAIL_TO, msg.as_bytes())
        log.debug("Email notification sent.")
        return True
    except Exception as exc:
        log.warning(f"Email send failed: {exc}")
        return False


def _send_telegram(message: str) -> bool:
    """Push via Telegram Bot API."""
    if not Config.BOT_TOKEN or not Config.CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{Config.BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": Config.CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        log.debug("Telegram notification sent.")
        return True
    except requests.RequestException as exc:
        log.warning(f"Telegram send failed: {exc}")
        return False


def send_alert(message: str) -> bool:
    """
    Unified alert dispatcher — tries every configured channel.
    Returns True if at least one channel succeeded.
    Also aliased as send_telegram() for backward compatibility.
    """
    results = [
        _send_ntfy(message),
        _send_discord(message),
        _send_email(message),
        _send_telegram(message),
    ]
    return any(results)


# Backward-compat alias used throughout the rest of the file
send_telegram = send_alert


# ─────────────────────────────────────────────────────────────────────────────
# Human-like timing helpers
# ─────────────────────────────────────────────────────────────────────────────

def human_delay(min_ms: Optional[int] = None, max_ms: Optional[int] = None) -> None:
    """Sleep for a random duration in [min_ms, max_ms] milliseconds."""
    lo = (min_ms or Config.MIN_DELAY_MS) / 1000.0
    hi = (max_ms or Config.MAX_DELAY_MS) / 1000.0
    time.sleep(random.uniform(lo, hi))


def human_type(page: Page, selector: str, text: str) -> None:
    """
    Click a field and type text character-by-character with random delays,
    mimicking a human typist.
    Uses locator.press_sequentially() which is the modern Playwright API
    (page.type() is deprecated since Playwright 1.45).
    """
    loc = page.locator(selector).first
    loc.click()
    human_delay(100, 300)
    # Clear existing value first
    loc.fill("")
    human_delay(50, 150)
    loc.press_sequentially(text, delay=random.uniform(40, 130))
    human_delay(100, 250)


# ─────────────────────────────────────────────────────────────────────────────
# Stealth browser factory
# ─────────────────────────────────────────────────────────────────────────────

# Realistic desktop user-agents to rotate
USER_AGENTS: List[str] = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
]

# Realistic viewport sizes
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
]


def create_stealth_context(playwright: Playwright) -> tuple[Browser, BrowserContext]:
    """
    Launch Chromium with stealth configuration to minimise bot-detection
    fingerprinting surface.
    """
    ua = random.choice(USER_AGENTS)
    viewport = random.choice(VIEWPORTS)

    browser: Browser = playwright.chromium.launch(
        headless=Config.HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
            f"--window-size={viewport['width']},{viewport['height']}",
        ],
    )

    context: BrowserContext = browser.new_context(
        user_agent=ua,
        viewport=viewport,
        locale="de-DE",
        timezone_id="Europe/Berlin",
        ignore_https_errors=True,
        # Mimic real browser permissions / features
        geolocation={"longitude": 7.4653, "latitude": 51.5136},  # Dortmund coords
        permissions=["geolocation"],
        extra_http_headers={
            "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
        },
    )

    # Patch navigator.webdriver to undefined so detection scripts fail
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['de-DE', 'de']});
        window.chrome = {runtime: {}};
    """)

    return browser, context


# ─────────────────────────────────────────────────────────────────────────────
# Selectors — ordered by specificity; first match wins
# ─────────────────────────────────────────────────────────────────────────────

class Selectors:
    """
    Fallback selector chains for key page elements.
    The bot tries each selector in order and uses the first one that resolves.
    """

    # "Bewerben" / "Jetzt bewerben" buttons on listing cards
    APPLY_BUTTONS = [
        "a:has-text('Bewerben')",
        "a:has-text('Jetzt bewerben')",
        "a:has-text('Online bewerben')",
        "a:has-text('Zur Bewerbung')",
        "a:has-text('Hier bewerben')",
        "button:has-text('Bewerben')",
        "button:has-text('Jetzt bewerben')",
        "button:has-text('Online bewerben')",
        "a[href*='bewerb']",
        "[data-action='apply']",
        ".apply-button",
        ".bewerben",
    ]

    # Room / flat listing cards — freshly posted rooms often have these classes
    LISTING_CARDS = [
        "article.room-offer",
        "article.listing-card",
        "[data-listing-id]",
        ".room-card",
        ".wohnangebot",
        ".offer-card",
        # The StwDO site may render availability directly on /aktuelle-wohnangebote
        "section.offer",
        "div.offer",
        "div[class*='offer']",
        "div[class*='room']",
    ]

    # "No rooms available" / empty state indicators
    EMPTY_INDICATORS = [
        "text=Aktuell keine freien Zimmer",
        "text=keine aktuellen Angebote",
        "text=Keine Angebote",
        "text=Momentan sind keine Zimmer",
        ".no-offers",
        ".empty-state",
    ]

    # ── Application form fields ──────────────────────────────────────────────

    ANREDE = [
        "select[name*='anrede']",
        "select[id*='anrede']",
        "input[value='Herr']",
        "input[value='m']",
        "label:has-text('Herr')",
    ]

    HOCHSCHULE = [
        "input[name*='hochschule']",
        "input[name*='uni']",
        "select[name*='hochschule']",
        "input[placeholder*='Hochschule']",
        "input[placeholder*='Universit']",
    ]

    VORNAME = [
        "input[name='vorname']",
        "input[name='first_name']",
        "input[name='firstName']",
        "input[id*='vorname']",
        "input[id*='first']",
        "input[placeholder*='Vorname']",
        "input[placeholder*='First']",
        "input[autocomplete='given-name']",
    ]

    NACHNAME = [
        "input[name='nachname']",
        "input[name='last_name']",
        "input[name='lastName']",
        "input[id*='nachname']",
        "input[id*='last']",
        "input[placeholder*='Nachname']",
        "input[placeholder*='Last']",
        "input[autocomplete='family-name']",
    ]

    EMAIL = [
        "input[name='email']",
        "input[type='email']",
        "input[id*='email']",
        "input[placeholder*='E-Mail']",
        "input[placeholder*='Email']",
        "input[autocomplete='email']",
    ]

    PHONE = [
        "input[name='telefon']",
        "input[name='phone']",
        "input[name='tel']",
        "input[type='tel']",
        "input[id*='telefon']",
        "input[id*='phone']",
        "input[placeholder*='Telefon']",
        "input[placeholder*='Phone']",
        "input[autocomplete='tel']",
    ]

    MATRIKEL = [
        "input[name='matrikel']",
        "input[name='matrikelnummer']",
        "input[name='matNumber']",
        "input[id*='matrikel']",
        "input[placeholder*='Matrikel']",
    ]

    DATENSCHUTZ = [
        "input[name='datenschutz']",
        "input[name='privacy']",
        "input[name='agb']",
        "input[name='terms']",
        "input[id*='datenschutz']",
        "input[id*='privacy']",
        "input[type='checkbox'][id*='daten']",
        "input[type='checkbox'][name*='daten']",
        "label:has-text('Datenschutz') input[type='checkbox']",
        "label:has-text('Datenschutzerklärung') input[type='checkbox']",
        "input[type='checkbox']",  # last resort: any checkbox
    ]

    SUBMIT = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Absenden')",
        "button:has-text('Bewerben')",
        "button:has-text('Senden')",
        "button:has-text('Submit')",
        "[data-action='submit']",
        "form button:last-of-type",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Selector resolution helper
# ─────────────────────────────────────────────────────────────────────────────

def first_visible(page: Page, selectors: List[str], timeout: int = 5_000) -> Optional[str]:
    """
    Return the first selector from the list that resolves to a visible element,
    or None if none match within `timeout` milliseconds.
    """
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            return sel
        except (PWTimeoutError, Exception):
            continue
    return None


def safe_fill(page: Page, selector_chain: List[str], value: str, label: str = "") -> bool:
    """
    Attempt to fill an input field using the selector fallback chain.
    Returns True if the field was found and filled.
    """
    sel = first_visible(page, selector_chain, timeout=4_000)
    if not sel:
        log.debug(f"Field [{label}] not found with any selector.")
        return False
    try:
        human_type(page, sel, value)
        # Show a safe preview — avoids short-value truncation and encoding issues
        preview = (value[:8] + "...") if len(value) > 8 else value
        log.debug(f"Filled [{label}] -> '{preview}' using selector: {sel}")
        return True
    except Exception as exc:
        log.warning(f"Failed to fill [{label}]: {exc}")
        return False


def safe_check(page: Page, selector_chain: List[str], label: str = "") -> bool:
    """
    Attempt to check a checkbox. Returns True on success.
    """
    sel = first_visible(page, selector_chain, timeout=4_000)
    if not sel:
        log.debug(f"Checkbox [{label}] not found.")
        return False
    try:
        checkbox = page.locator(sel).first
        if not checkbox.is_checked():
            checkbox.click()
            human_delay(100, 300)
        log.debug(f"Checked [{label}] using selector: {sel}")
        return True
    except Exception as exc:
        log.warning(f"Failed to check [{label}]: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Application form autofill
# ─────────────────────────────────────────────────────────────────────────────


def handle_mosparo_gate(page: Page) -> bool:
    """
    Check if StwDO's Spam-Schutz (mosparo protection gate) is present.
    If so, automatically interacts with the verification widget and submits
    to unlock the housing listings page.

    Returns True if the gate was detected (regardless of resolution outcome).
    The bot should re-navigate if the gate is still blocking after handling.
    """
    try:
        gate_form = page.locator(
            "#housing-offers-access-form, form[action*='wohnen/aktuelle-wohnangebote']"
        ).first
        if not gate_form.is_visible(timeout=1_500):
            return False

        log.info("[yellow]Spam-Schutz / Mosparo gate detected. Resolving...[/]")
        human_delay(800, 1500)

        # Try to click the mosparo checkbox — try selectors in order, stop at first success
        mosparo_selectors = [
            "#housing-offers-mosparo-box input[type='checkbox']",
            "#housing-offers-mosparo-box .mosparo__checkbox",
            "#housing-offers-mosparo-box label",
            ".mosparo__control",
            "label[for*='mosparo']",
            "#housing-offers-mosparo-box",
        ]

        clicked = False
        for sel in mosparo_selectors:
            try:
                elem = page.locator(sel).first
                if elem.is_visible(timeout=1_500):
                    log.info(f"Clicking spam protection checkbox via: {sel}")
                    human_delay(300, 600)
                    elem.click()
                    clicked = True
                    break  # Stop immediately after first successful click
            except Exception:
                continue

        if not clicked:
            log.warning("Could not interact with mosparo widget — no selector matched.")
            return True  # Gate was detected, even if we couldn't click it

        # Wait for mosparo verification to complete (async network check)
        log.info("Waiting for spam protection verification to process...")
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass  # Continue even if network is still active

        human_delay(1500, 2500)

        # Verify the gate is gone — if still visible, submit the form
        try:
            if gate_form.is_visible(timeout=2_000):
                # Gate still present — try submitting the access form
                submit_btn = page.locator(
                    "#housing-offers-access-form button[type='submit'], "
                    "form[action*='wohnen'] button[type='submit']"
                ).first
                if submit_btn.is_visible(timeout=2_000):
                    log.info("Submitting access form to bypass gate...")
                    submit_btn.click()
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                    human_delay(1000, 2000)
        except Exception:
            pass

        log.info("[green]Spam protection handling complete.[/]")
        return True
    except Exception as exc:
        log.debug(f"Spam gate handling check: {exc}")
        return False

def fill_application_form(page: Page, room_name: str = "Unknown room") -> bool:
    """
    Detect and fill an application form on the current page.

    Returns True if the form was found AND submitted successfully.
    """
    log.info(f"Attempting to fill application form for: [bold cyan]{room_name}[/]")

    human_delay(400, 800)

    # Fill each field — non-fatal if individual fields are missing
    fill_results = {
        "Vorname":    safe_fill(page, Selectors.VORNAME,    Config.FIRST_NAME, "Vorname"),
        "Nachname":   safe_fill(page, Selectors.NACHNAME,   Config.LAST_NAME,  "Nachname"),
        "Email":      safe_fill(page, Selectors.EMAIL,      Config.EMAIL,      "Email"),
        "Telefon":    safe_fill(page, Selectors.PHONE,      Config.PHONE,      "Telefon"),
    }

    # Optional fields
    if Config.MATRIKEL:
        safe_fill(page, Selectors.MATRIKEL,   Config.MATRIKEL,   "Matrikelnummer")
    if Config.UNIVERSITY:
        safe_fill(page, Selectors.HOCHSCHULE, Config.UNIVERSITY, "Hochschule")

    human_delay(300, 600)

    # Accept privacy / Datenschutz checkbox
    privacy_ok = safe_check(page, Selectors.DATENSCHUTZ, "Datenschutz")
    if not privacy_ok:
        log.warning("Datenschutz checkbox not found — form may be rejected.")

    human_delay(300, 700)

    # Submit
    submit_sel = first_visible(page, Selectors.SUBMIT, timeout=5_000)
    if not submit_sel:
        log.error("Submit button not found — cannot complete application!")
        return False

    try:
        page.locator(submit_sel).first.click()
        # Wait for navigation or network idle after form submission
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeoutError:
            pass  # Some sites don't fully idle after POST
        human_delay(500, 1000)

        # Check for success indicators in page text
        body_text = page.inner_text("body").lower()
        success_keywords = [
            "erfolgreich",
            "bestaetigung",
            "bestätigung",
            "danke",
            "thank you",
            "vielen dank",
            "beworben",
            "eingegangen",
            "success",
            "confirmation",
        ]
        success = any(kw in body_text for kw in success_keywords)

        if success:
            log.info(
                f"[bold green][SUCCESS] Application submitted successfully![/] Room: {room_name}"
            )
        else:
            log.warning(
                "Form submitted but no explicit confirmation detected. "
                "Manual verification recommended."
            )

        return True  # We attempted submission

    except Exception as exc:
        log.error(f"Error clicking submit: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Core listing detection & application logic
# ─────────────────────────────────────────────────────────────────────────────

@retry(
    # Only retry on network/timeout errors — not on KeyboardInterrupt or SystemExit
    retry=retry_if_exception_type((PWTimeoutError, OSError, ConnectionError)),
    wait=wait_exponential(multiplier=2, min=5, max=120),
    stop=stop_after_attempt(5),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def check_and_apply(page: Page, applied_urls: set) -> int:
    """
    Navigate to the listings page, detect new room offers, and attempt
    to apply to each one not previously seen.

    Returns the number of new listings acted upon.
    """
    log.info(f"Polling: [link={Config.LISTINGS_URL}]{Config.LISTINGS_URL}[/link]")

    page.goto(Config.LISTINGS_URL, wait_until="domcontentloaded", timeout=60_000)
    human_delay(800, 1800)  # Wait for JS-rendered content

    # Check and unlock Spam-Schutz / Mosparo gate if presented
    handle_mosparo_gate(page)

    # Wait a bit more for dynamic content to settle
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PWTimeoutError:
        pass  # Proceed even if network is still active (some SPAs)

    human_delay(500, 1200)

    # ── Check for "no offers" empty state ──────────────────────────────────
    for indicator in Selectors.EMPTY_INDICATORS:
        try:
            if page.locator(indicator).is_visible(timeout=1_000):
                log.info("No active listings found. Portal shows empty state.")
                return 0
        except Exception:
            pass

    # ── Detect listing cards / apply buttons ──────────────────────────────
    new_count = 0

    # Strategy 1: Direct "Bewerben" links on the listing page
    for btn_sel in Selectors.APPLY_BUTTONS:
        buttons = page.locator(btn_sel).all()
        if buttons:
            log.info(
                f"Found {len(buttons)} apply button(s) via selector: "
                f"[italic]{btn_sel}[/]"
            )
            for btn in buttons:
                try:
                    # Extract room name from nearby heading or parent card
                    try:
                        card = btn.locator("xpath=ancestor::article | ancestor::section | ancestor::div[@class]").first
                        room_name = card.locator("h2, h3, h4, .room-name").first.inner_text(timeout=2_000).strip()
                    except Exception:
                        room_name = btn.inner_text(timeout=2_000).strip() or "Unknown room"

                    # Derive a unique identifier for deduplication
                    href = btn.get_attribute("href") or room_name
                    canonical_url = (
                        f"https://www.stwdo.de{href}" if href.startswith("/") else href
                    )

                    if canonical_url in applied_urls:
                        log.debug(f"Already applied to {canonical_url}, skipping.")
                        continue

                    log.info(
                        f"[bold yellow]🏠 New listing detected:[/] {room_name}"
                    )
                    send_telegram(
                        f"🏠 <b>New StwDO listing detected!</b>\n\n"
                        f"Room: <b>{room_name}</b>\n"
                        f"Link: {canonical_url}\n\n"
                        f"Attempting to apply now..."
                    )

                    applied_urls.add(canonical_url)
                    new_count += 1

                    # If it's a link, navigate; if it's a button, click
                    if href and href != room_name:
                        log.info(f"Navigating to application page: {canonical_url}")
                        page.goto(canonical_url, wait_until="domcontentloaded", timeout=30_000)
                        human_delay(600, 1200)

                        # Look for an explicit apply/bewerben trigger on the detail page
                        inner_btn = first_visible(page, Selectors.APPLY_BUTTONS, timeout=5_000)
                        if inner_btn:
                            page.locator(inner_btn).first.click()
                            human_delay(500, 1000)
                    else:
                        btn.click()
                        human_delay(500, 1000)

                    # Attempt form fill
                    success = fill_application_form(page, room_name)

                    status_msg = (
                        f"✅ <b>Application submitted!</b>\n\nRoom: <b>{room_name}</b>"
                        if success else
                        f"⚠️ <b>Application attempt incomplete</b>\n\nRoom: <b>{room_name}</b>\n"
                        f"Please check manually: {canonical_url}"
                    )
                    send_telegram(status_msg)

                    # Navigate back to listings page directly (more reliable than go_back()
                    # which can fail after a POST redirect or cross-origin navigation)
                    page.goto(Config.LISTINGS_URL, wait_until="domcontentloaded", timeout=30_000)
                    human_delay(1000, 2000)

                except Exception as exc:
                    log.error(f"Error processing listing button: {exc}")
                    send_telegram(
                        f"❌ <b>Error processing listing</b>\n\n"
                        f"Detail: {str(exc)[:300]}"
                    )

            if new_count:
                return new_count

    # Strategy 2: Scan for listing cards and check each for availability
    for card_sel in Selectors.LISTING_CARDS:
        cards = page.locator(card_sel).all()
        if not cards:
            continue

        log.info(
            f"Found {len(cards)} listing card(s) via selector: [italic]{card_sel}[/]"
        )
        for card in cards:
            try:
                # Check if this card has an apply button
                apply_btn = card.locator(
                    "a:has-text('Bewerben'), button:has-text('Bewerben'), "
                    "a:has-text('Jetzt bewerben')"
                ).first
                if not apply_btn.is_visible(timeout=500):
                    continue

                room_name = (
                    card.locator("h2, h3, h4").first.inner_text(timeout=2_000).strip()
                    or "Unknown room"
                )
                href = apply_btn.get_attribute("href") or ""
                canonical_url = (
                    f"https://www.stwdo.de{href}" if href.startswith("/") else
                    href or room_name
                )

                if canonical_url in applied_urls:
                    continue

                log.info(f"[bold yellow]🏠 New card listing:[/] {room_name}")
                send_telegram(
                    f"🏠 <b>New StwDO listing found via card scan!</b>\n\n"
                    f"Room: <b>{room_name}</b>\n"
                    f"Applying now..."
                )

                applied_urls.add(canonical_url)
                new_count += 1

                apply_btn.click()
                human_delay(600, 1200)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                except PWTimeoutError:
                    pass

                success = fill_application_form(page, room_name)
                send_telegram(
                    f"{'✅' if success else '⚠️'} Application "
                    f"{'submitted' if success else 'attempt made'} for: "
                    f"<b>{room_name}</b>"
                )

                # Navigate back directly instead of go_back() to avoid POST-redirect issues
                page.goto(Config.LISTINGS_URL, wait_until="domcontentloaded", timeout=30_000)
                human_delay(800, 1500)

            except Exception as exc:
                log.debug(f"Card processing error: {exc}")
        break  # Stop at first selector that found cards

    if new_count == 0:
        log.info(
            "Listings page loaded but no actionable offers detected "
            "(portal may still be closed)."
        )

    return new_count


# ─────────────────────────────────────────────────────────────────────────────
# Main polling loop
# ─────────────────────────────────────────────────────────────────────────────

def run_bot() -> None:
    """
    Entry point for the monitoring bot.

    Starts a Playwright browser session and polls the listings page until
    MAX_RUNTIME_MINUTES have elapsed, then exits cleanly.
    """
    Config.validate()

    deadline = datetime.now(BERLIN_TZ) + timedelta(minutes=Config.MAX_RUNTIME_MIN)
    poll_count = 0
    applied_urls: set = set()

    console.rule("[bold blue]StwDO Dorm Monitor Bot[/]")
    log.info(
        f"Config - Applicant: [bold]{Config.FIRST_NAME} {Config.LAST_NAME}[/] | "
        f"Interval: {Config.POLL_INTERVAL}s | "
        f"Max runtime: {Config.MAX_RUNTIME_MIN}min | "
        f"Headless: {Config.HEADLESS}"
    )
    log.info(
        f"Session deadline: [bold]{deadline.strftime('%Y-%m-%d %H:%M:%S %Z')}[/]"
    )

    if not STEALTH_AVAILABLE:
        log.warning(
            "playwright-stealth is not installed. "
            "Bot-detection evasion will be limited. "
            "Install with: pip install playwright-stealth"
        )

    send_telegram(
        f"🤖 <b>StwDO Dorm Bot started</b>\n\n"
        f"Applicant: {Config.FIRST_NAME} {Config.LAST_NAME}\n"
        f"Polling every {Config.POLL_INTERVAL}s for up to {Config.MAX_RUNTIME_MIN}min\n"
        f"Target: {Config.LISTINGS_URL}"
    )

    with sync_playwright() as pw:
        browser, context = create_stealth_context(pw)
        page: Page = context.new_page()

        # Apply playwright-stealth if available
        if STEALTH_AVAILABLE:
            _apply_stealth(page)

        # Intercept and block heavy resources to speed up page loads
        def block_unnecessary(route, request):
            blocked = {"image", "media", "font", "other"}
            # Allow images that might contain listing info; block media/fonts
            if request.resource_type in {"media", "font"}:
                route.abort()
            else:
                route.continue_()

        page.route("**/*", block_unnecessary)

        try:
            while datetime.now(BERLIN_TZ) < deadline:
                poll_count += 1
                now_berlin = datetime.now(BERLIN_TZ).strftime("%H:%M:%S")
                log.info(
                    f"[dim]Poll #{poll_count} @ {now_berlin} Berlin time[/]"
                )

                try:
                    new_found = check_and_apply(page, applied_urls)
                    if new_found:
                        log.info(
                            f"[green]Acted on {new_found} new listing(s) this poll.[/]"
                        )
                except Exception as exc:
                    log.error(f"Unrecoverable error during poll: {exc}")
                    log.debug(traceback.format_exc())
                    send_telegram(
                        f"❌ <b>Bot error (poll #{poll_count})</b>\n\n"
                        f"{str(exc)[:400]}"
                    )

                # Wait until next poll (minus time already spent)
                remaining_s = (deadline - datetime.now(BERLIN_TZ)).total_seconds()
                sleep_s = min(Config.POLL_INTERVAL, remaining_s)
                if sleep_s > 0:
                    log.info(f"[dim]Sleeping {sleep_s:.0f}s until next poll...[/]")
                    time.sleep(sleep_s)

        except KeyboardInterrupt:
            log.info("Keyboard interrupt received — shutting down gracefully.")
        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass

    send_telegram(
        f"🛑 <b>StwDO Dorm Bot stopped</b>\n\n"
        f"Total polls: {poll_count}\n"
        f"Listings applied to: {len(applied_urls)}"
    )
    log.info(
        f"[bold]Session complete.[/] Polls: {poll_count} | "
        f"Applied URLs: {len(applied_urls)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_bot()
