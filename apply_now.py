"""Real apply to current listing on StwDO right now."""
import dorm_agent as d
from playwright.sync_api import sync_playwright

print(f"Applying with Email: {d.Config.EMAIL}")
print(f"Phone: {d.Config.PHONE} / Mobile: {d.Config.MOBILE or d.Config.PHONE}")

with sync_playwright() as p:
    browser, context = d.create_stealth_context(p)
    page = context.new_page()
    d._apply_stealth(page)

    applied_set = set()
    count = d.check_and_apply(page, applied_set)
    print(f"Listings found on main page: {count}")

    # Fallback to direct room link if main overview page is in between updates
    if count == 0:
        print("Checking known direct listing /freie-zimmer/6630...")
        ok = d.apply_stwdo_listing(page, "https://www.stwdo.de/freie-zimmer/6630", "Iserlohn Steubenstraße 14-18")
        if ok:
            applied_set.add("https://www.stwdo.de/freie-zimmer/6630")
            count = 1
            print("Successfully applied to /freie-zimmer/6630!")
            d.send_alert(f"✅ Application submitted for Iserlohn! Email: {d.Config.EMAIL}")

    print(f"Total listings processed: {count}")
    print(f"Applied URLs: {applied_set}")
    
    browser.close()
