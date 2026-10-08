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

# listing URL -> number of apply attempts made this session
_ATTEMPTS: dict = {}


# ─────────────────────────────────────────────────────────────────────────────
# Configuration — loaded from .env
# ─────────────────────────────────────────────────────────────────────────────

class Config:
    """All runtime configuration sourced from environment variables with safe defaults."""

    # Applicant data (reads environment, falling back to registered details)
    FIRST_NAME: str     = os.getenv("APPLICANT_FIRST_NAME", "").strip() or "Ahmed"
    LAST_NAME: str      = os.getenv("APPLICANT_LAST_NAME", "").strip() or "Rasheed"
    EMAIL: str          = os.getenv("APPLICANT_EMAIL", "").strip() or "Ahmedrasheed112255@gmail.com"
    PHONE: str          = os.getenv("APPLICANT_PHONE", "").strip() or "+3089876647"
    MATRIKEL: str       = os.getenv("APPLICANT_MATRIKEL", "").strip() or "285351"
    UNIVERSITY: str     = os.getenv("APPLICANT_UNIVERSITY", "").strip() or "TU Dortmund"

    # Extra mandatory fields of the real Wohnungshelden application form
    SALUTATION: str     = os.getenv("APPLICANT_SALUTATION", "").strip() or "Herr"
    DOB: str            = os.getenv("APPLICANT_DOB", "").strip() or "30.08.2002"
    NATIONALITY: str    = os.getenv("APPLICANT_NATIONALITY", "").strip() or "Pakistan"
    SEMESTER_TYPE: str  = os.getenv("APPLICANT_SEMESTER_TYPE", "").strip() or "Winter"
    YEAR: str           = os.getenv("APPLICANT_YEAR", "").strip() or "2026"
    NUM_SEMESTERS: str  = os.getenv("APPLICANT_NUM_SEMESTERS", "").strip() or "6"
    MAX_RENT: str       = os.getenv("APPLICANT_MAX_RENT", "").strip() or "400"
    # DRY_RUN=true -> fill the form but do NOT submit (for testing)
    DRY_RUN: bool       = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")
    # Mobile number for the Wohnungshelden form (spaced format, e.g. +92 308 9876647)
    MOBILE: str         = os.getenv("APPLICANT_MOBILE", "").strip()

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
        if not cls.EMAIL:      cls.EMAIL      = "Ahmedrasheed112255@gmail.com"
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

def _wh_pick_option(frame, field_id: str, keywords: List[str], label: str) -> bool:
    """Open a mat-select inside the Wohnungshelden iframe and pick an option."""
    try:
        sel = frame.locator(f"[id='{field_id}']").first
        sel.evaluate("e => { e.scrollIntoView({block:'center'}); e.click(); }")
        frame.locator("mat-option").first.wait_for(state="attached", timeout=5_000)
        human_delay(250, 450)
        opts = frame.locator("mat-option").all()
        texts = [o.inner_text().strip() for o in opts]
        for kw in keywords:
            for o, t in zip(opts, texts):
                if kw and kw.lower() in t.lower():
                    o.evaluate("e => e.click()")
                    human_delay(200, 400)
                    return True
        log.warning(f"[{label}] no option matched {keywords}. Options: {texts}")
        frame.page.keyboard.press("Escape")
    except Exception as exc:
        log.warning(f"[{label}] select failed: {str(exc)[:200]}")
    return False


def _wh_toggle(frame, field_id: str, label: str) -> bool:
    """Tick a Material checkbox/radio and verify it is really set."""
    inp = frame.locator(f"[id='{field_id}']").first
    attempts = (
        lambda: frame.locator(f"label[for='{field_id}']").first.click(timeout=3_000, force=True),
        lambda: inp.evaluate("e => e.click()"),
        lambda: inp.locator(
            "xpath=ancestor::*[self::mat-checkbox or self::mat-radio-button][1]"
        ).evaluate("e => e.click()"),
    )
    for act in attempts:
        try:
            if inp.is_checked():
                return True
            act()
            human_delay(150, 300)
            if inp.is_checked():
                return True
        except Exception:
            continue
    log.warning(f"[{label}] could not be ticked")
    return False


def _wh_fill(frame, field_id: str, value: str, label: str) -> bool:
    try:
        loc = frame.locator(f"[id='{field_id}']").first
        loc.wait_for(state="attached", timeout=5_000)
        loc.evaluate("e => { e.scrollIntoView({block:'center'}); e.focus(); }")
        loc.fill(value, force=True, timeout=5_000)
        loc.dispatch_event("blur")
        got = loc.input_value()
        if got != value:
            log.warning(f"[{label}] value mismatch: wanted '{value}', got '{got}'")
        return True
    except Exception as exc:
        log.warning(f"[{label}] fill failed: {str(exc)[:200]}")
        return False


def apply_stwdo_listing(page: Page, url: str, room_name: str) -> bool:
    """
    Full apply flow for a https://www.stwdo.de/freie-zimmer/<id> listing:
    listing page -> consent + mosparo -> 'Bewerbungsformular laden' ->
    Wohnungshelden iframe form -> submit.
    Returns True only if the form was submitted AND accepted.
    """
    listing_id = url.rstrip("/").split("/")[-1]
    log.info(f"Opening listing page: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    human_delay(800, 1500)
    handle_mosparo_gate(page)

    # 1) consent checkbox (Wohnungshelden data processing)
    try:
        page.locator("#application-consent-cb").check(force=True, timeout=8_000)
    except Exception as exc:
        log.error(f"Consent checkbox not found: {exc}")
        return False

    # 2) mosparo for the application form
    try:
        box = f"#room-application-mosparo-box-{listing_id}"
        page.locator(f"{box} .mosparo__checkbox").first.click(force=True, timeout=8_000)
        page.locator(f"{box} .mosparo__icon-checkmark").first.wait_for(
            state="visible", timeout=20_000
        )
    except Exception as exc:
        log.warning(f"Mosparo verification not confirmed ({exc}); continuing anyway.")
        human_delay(2000, 3000)

    # 3) load the application form (iframe). Click ONCE and wait patiently:
    # re-clicking reloads the iframe and invalidates frame handles.
    human_delay(5000, 6000)

    def _find_form_frame():
        cands = [f for f in page.frames if "wohnungshelden" in f.url]
        for f in reversed(cands):
            try:
                if f.locator("[id='mat-input-1']").count():
                    return f
            except Exception:
                continue
        return None

    frame = None
    for attempt in range(3):
        try:
            page.locator("#application-load-btn").click(timeout=8_000)
        except Exception as exc:
            log.warning(f"'Bewerbungsformular laden' click failed ({attempt + 1}/3): {exc}")
        for _ in range(90):  # up to ~45 s
            frame = _find_form_frame()
            if frame:
                break
            time.sleep(0.5)
        if frame:
            break
        log.warning(f"Iframe not ready after attempt {attempt + 1}/3 - retrying.")
        human_delay(1500, 2500)
    if frame:
        time.sleep(1.5)  # let Angular settle
        frame = _find_form_frame() or frame
    if not frame:
        log.error("Wohnungshelden form iframe did not load.")
        try:
            page.screenshot(path="iframe_fail.png", full_page=True)
        except Exception:
            pass
        return False
    human_delay(800, 1500)

    # The sticky site header overlaps the iframe and intercepts clicks
    try:
        page.add_style_tag(content="#header__js{display:none !important}")
    except Exception:
        pass

    # 4) fill the form
    _wh_pick_option(frame, "mat-select-1", [Config.SALUTATION], "Anrede")
    _wh_fill(frame, "mat-input-1", Config.FIRST_NAME, "Vorname")
    _wh_fill(frame, "mat-input-2", Config.LAST_NAME, "Nachname")
    _wh_fill(frame, "mat-input-0", Config.EMAIL, "E-Mail")
    _wh_fill(frame, "mat-input-3", Config.PHONE, "Telefon")
    mobile = Config.MOBILE
    if not mobile:
        digits = Config.PHONE.replace(" ", "")
        if digits.startswith("+92") and len(digits) == 13:
            mobile = f"+92 {digits[3:6]} {digits[6:]}"
        else:
            mobile = Config.PHONE
    _wh_fill(frame, "formly_10_input_$$_mobile_number_$$_0", mobile, "Mobil")
    _wh_fill(frame, "formly_10_input_$$_date_of_birth_$$_1", Config.DOB, "Geburtsdatum")
    _wh_pick_option(frame, "formly_10_select_nationality_2",
                    [Config.NATIONALITY, "pakistan"], "Nationality")
    _wh_pick_option(frame, "formly_13_select_startOfSemester_0",
                    [Config.SEMESTER_TYPE, "winter"], "Semester")
    _wh_fill(frame, "formly_13_input_year_1", Config.YEAR, "Jahr")
    _wh_fill(frame, "formly_13_input_numberOfSemester_2", Config.NUM_SEMESTERS, "Anzahl Semester")
    _wh_fill(frame, "formly_14_input_stwdo_gesamtmiete_max_0", Config.MAX_RENT, "Max. Miete")
    _wh_pick_option(frame, "formly_15_select_stwdo_university_0",
                    [Config.UNIVERSITY, "dortmund"], "Hochschule")
    # radio: _0_0 = Ja, _0_1 = Nein
    _wh_toggle(frame, "formly_16_radio_stwdo_angewiesen_auf_rollstuhlgerechte_wohnung_0_1-input", "Rollstuhl: Nein")
    _wh_toggle(frame, "formly_17_checkbox_stwdo_immatrikulation_0-input", "Immatrikulation")
    _wh_toggle(frame, "formly_18_checkbox_stwdo_datenschutzhinweis_bestaetigt_0-input", "Datenschutz")

    # Read back what the form now contains (visible in logs for debugging)
    try:
        vals = frame.evaluate(
            """() => [...document.querySelectorAll('mat-form-field')].map(f => {
                const l = (f.querySelector('mat-label,label')||{}).innerText || '?';
                const i = f.querySelector('input,textarea');
                const s = f.querySelector('.mat-mdc-select-value');
                return l.trim().slice(0,25) + ' = ' + ((i && i.value) || (s && s.innerText) || '')
            })"""
        )
        log.info("Form read-back: " + " | ".join(vals))
    except Exception as exc:
        log.debug(f"read-back failed: {exc}")
    try:
        errs = [e.inner_text().strip() for e in frame.locator("mat-error").all()]
        if errs:
            log.warning(f"Validation errors before submit: {errs}")
    except Exception:
        pass

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        page.screenshot(path=f"before_submit_{stamp}.png", full_page=True)
    except Exception:
        pass

    if Config.DRY_RUN:
        log.warning("DRY_RUN enabled - form filled but NOT submitted.")
        return False

    # 5) submit
    try:
        frame.locator("button[type='submit']").first.click(timeout=8_000)
    except Exception as exc:
        log.error(f"Submit click failed: {exc}")
        return False
    human_delay(4000, 6000)

    try:
        text = frame.locator("body").inner_text(timeout=5_000).lower()
    except Exception:
        text = ""
    try:
        page.screenshot(path=f"after_submit_{stamp}.png", full_page=True)
        with open(f"after_submit_{stamp}.txt", "w", encoding="utf-8") as fh:
            fh.write(text[:5000])
    except Exception:
        pass

    errors = frame.locator("mat-error").count()
    still_form = "anfrage versenden" in text
    if errors or still_form:
        log.error(f"Submit rejected: {errors} validation error(s), form still shown.")
        return False
    log.info("[bold green][SUCCESS] Wohnungshelden accepted the application.[/]")
    return True


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

    missing = [k for k, ok in fill_results.items() if not ok]
    if missing:
        log.warning(f"Fields NOT filled: {', '.join(missing)}")

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

        # Always save evidence of what the page looked like after submit
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            page.screenshot(path=f"after_submit_{stamp}.png", full_page=True)
            with open(f"after_submit_{stamp}.txt", "w", encoding="utf-8") as fh:
                fh.write(f"URL: {page.url}\n\n{body_text[:5000]}")
        except Exception as exc:
            log.debug(f"Could not save post-submit evidence: {exc}")

        if success:
            log.info(
                f"[bold green][SUCCESS] Application submitted successfully![/] Room: {room_name}"
            )
            return True

        log.warning(
            "Form submitted but NO confirmation detected - application is "
            "probably NOT registered. Apply manually!"
        )
        return False

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

    # ── Detect listing cards / apply buttons ──────────────────────────────
    new_count = 0

    # Strategy 0: real StwDO listings (/freie-zimmer/<id>, button "Mehr Informationen")
    room_links = []
    for a in page.locator("a[href*='/freie-zimmer/']").all():
        try:
            h = a.get_attribute("href") or ""
            full = f"https://www.stwdo.de{h}" if h.startswith("/") else h
            if full and full not in [u for u, _ in room_links]:
                try:
                    nm = a.locator(
                        "xpath=ancestor::*[self::article or self::li or self::div][.//h2 or .//h3 or .//h4][1]"
                    ).locator("h2, h3, h4").first.inner_text(timeout=1_500).strip()
                except Exception:
                    nm = "StwDO room"
                room_links.append((full, nm))
        except Exception:
            continue

    for url, nm in room_links:
        if url in applied_urls or _ATTEMPTS.get(url, 0) >= 3:
            continue
        _ATTEMPTS[url] = _ATTEMPTS.get(url, 0) + 1
        new_count += 1
        log.info(f"[bold yellow]New listing:[/] {nm} ({url}) attempt {_ATTEMPTS[url]}/3")
        send_telegram(
            f"🏠 <b>New StwDO listing detected!</b>\n\nRoom: <b>{nm}</b>\n"
            f"Link: {url}\n\nApplying now (attempt {_ATTEMPTS[url]}/3)..."
        )
        try:
            ok = apply_stwdo_listing(page, url, nm)
        except Exception as exc:
            log.error(f"apply_stwdo_listing crashed: {exc}")
            ok = False
        if ok:
            applied_urls.add(url)
            send_telegram(
                f"✅ <b>Application submitted & accepted!</b>\n\nRoom: <b>{nm}</b>\n"
                f"Check your email ({Config.EMAIL}) for the confirmation."
            )
        else:
            send_telegram(
                f"⚠️ <b>Application NOT confirmed</b>\n\nRoom: <b>{nm}</b>\n"
                f"APPLY MANUALLY NOW: {url}"
            )
        page.goto(Config.LISTINGS_URL, wait_until="domcontentloaded", timeout=30_000)
        handle_mosparo_gate(page)
        human_delay(1000, 2000)
    if new_count:
        return new_count

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
