"""Dry-run of the REAL apply flow: fills the live form but does NOT submit."""
import os
os.environ["DRY_RUN"] = "true"
import dorm_agent as d
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(locale="de-DE", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    pg = ctx.new_page()
    d._apply_stealth(pg)
    pg.goto(d.Config.LISTINGS_URL, timeout=60000)
    d.handle_mosparo_gate(pg)
    pg.wait_for_timeout(3000)
    print("links:", [a.get_attribute("href") for a in pg.locator("a[href*='/freie-zimmer/']").all()])
    d.apply_stwdo_listing(pg, "https://www.stwdo.de/freie-zimmer/6630", "test")
    b.close()
