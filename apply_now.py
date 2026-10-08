"""REAL apply (submits!) to the live listing - as explicitly requested by the user."""
import dorm_agent as d
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(locale="de-DE", viewport={"width": 1280, "height": 900}, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    pg = ctx.new_page()
    d._apply_stealth(pg)
    pg.goto(d.Config.LISTINGS_URL, timeout=60000)
    d.handle_mosparo_gate(pg)
    pg.wait_for_timeout(3000)
    ok = d.apply_stwdo_listing(pg, "https://www.stwdo.de/freie-zimmer/6630", "Iserlohn Steubenstrasse 14-18")
    print("RESULT_OK =", ok)
    if ok:
        d.send_alert(f"Application submitted for Iserlohn listing. Check email {d.Config.EMAIL}")
    b.close()
