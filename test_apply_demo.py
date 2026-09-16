"""
Test Simulation of StwDO Dorm Bot Application Flow
===================================================
This script creates a local mock StwDO listing page and runs the exact
bot logic to:
1. Detect the room listing
2. Click "Bewerben"
3. Autofill Ahmed Rasheed's details
4. Check Datenschutz (privacy policy)
5. Submit the form
6. Send the real ntfy alert to Ahmed's phone!
"""

import time
import os
from playwright.sync_api import sync_playwright
from dorm_agent import Config, fill_application_form, send_alert

# Ensure config is loaded
Config.validate()

MOCK_HTML = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <title>Aktuelle Wohnangebote - Studierendenwerk Dortmund</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #f4f6f8; margin: 40px; }
        .card { background: white; border-radius: 12px; padding: 24px; max-width: 600px; margin: 0 auto; box-shadow: 0 4px 20px rgba(0,0,0,0.08); border-top: 5px solid #004b93; }
        h2 { color: #004b93; margin-top: 0; }
        .details { margin: 15px 0; color: #555; line-height: 1.6; }
        .form-group { margin-bottom: 14px; text-align: left; }
        label { display: block; font-weight: 600; margin-bottom: 5px; color: #333; font-size: 14px; }
        input[type="text"], input[type="email"], input[type="tel"] { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-size: 15px; }
        .checkbox-group { display: flex; align-items: center; gap: 8px; margin: 15px 0; }
        button { background: #00875a; color: white; border: none; padding: 12px 24px; font-size: 16px; font-weight: bold; border-radius: 6px; cursor: pointer; width: 100%; transition: background 0.2s; }
        button:hover { background: #006644; }
        .badge { background: #e3fcef; color: #006644; padding: 4px 10px; border-radius: 20px; font-weight: 600; font-size: 13px; display: inline-block; margin-bottom: 10px; }
        #success-msg { display: none; background: #d4edda; color: #155724; padding: 16px; border-radius: 8px; margin-top: 15px; text-align: center; font-weight: bold; }
    </style>
</head>
<body>
    <div class="card" id="listing-card">
        <span class="badge">Sofort frei</span>
        <h2>Einzelzimmer - Baroper Straße 233 (TU Dortmund Campus)</h2>
        <div class="details">
            <p><strong>Miete:</strong> 315,00 € warm / Monat<br>
            <strong>Wohnfläche:</strong> ca. 18 m²<br>
            <strong>Einzugstermin:</strong> Ab sofort</p>
        </div>

        <form id="application-form" onsubmit="event.preventDefault(); document.getElementById('success-msg').style.display='block'; document.getElementById('application-form').style.display='none';">
            <h3>Bewerbungsformular</h3>
            <div class="form-group">
                <label>Vorname *</label>
                <input type="text" name="vorname" id="vorname" required>
            </div>
            <div class="form-group">
                <label>Nachname *</label>
                <input type="text" name="nachname" id="nachname" required>
            </div>
            <div class="form-group">
                <label>E-Mail-Adresse *</label>
                <input type="email" name="email" id="email" required>
            </div>
            <div class="form-group">
                <label>Telefonnummer *</label>
                <input type="tel" name="telefon" id="telefon" required>
            </div>
            <div class="form-group">
                <label>Matrikelnummer (TU Dortmund)</label>
                <input type="text" name="matrikelnummer" id="matrikelnummer">
            </div>
            <div class="checkbox-group">
                <input type="checkbox" name="datenschutz" id="datenschutz" required>
                <label for="datenschutz" style="margin-bottom:0; font-weight:normal; font-size:13px;">
                    Ich habe die Datenschutzerklärung zur Kenntnis genommen und stimme zu.
                </label>
            </div>
            <button type="submit" id="submit-btn">Jetzt verbindlich bewerben</button>
        </form>

        <div id="success-msg">
            🎉 Vielen Dank! Ihre Bewerbung ist erfolgreich eingegangen.<br>
            Bestätigung wurde versendet.
        </div>
    </div>
</body>
</html>
"""

def run_simulation():
    mock_file = os.path.abspath("mock_room.html")
    with open(mock_file, "w", encoding="utf-8") as f:
        f.write(MOCK_HTML)

    room_name = "Einzelzimmer - Baroper Straße 233 (TU Dortmund Campus)"
    
    print("\n" + "="*60)
    print(">>> STARTING SIMULATION TEST")
    print(f"Room: {room_name}")
    print(f"Applicant: {Config.FIRST_NAME} {Config.LAST_NAME}")
    print(f"Email: {Config.EMAIL}")
    print(f"Matrikel: {Config.MATRIKEL}")
    print("="*60 + "\n")

    # Send room detected alert to ntfy
    print("[*] Sending 'Room Detected' alert to your phone...")
    send_alert(
        f"🏠 <b>[TEST] New StwDO listing detected!</b>\n\n"
        f"Room: <b>{room_name}</b>\n"
        f"Link: https://www.stwdo.de/wohnen/aktuelle-wohnangebote\n\n"
        f"Attempting to apply now..."
    )

    with sync_playwright() as pw:
        # Launch browser visibly so user can see it!
        browser = pw.chromium.launch(headless=False, slow_mo=350)
        context = browser.new_context()
        page = context.new_page()

        print("[*] Opening listing page...")
        page.goto(f"file:///{mock_file.replace(os.sep, '/')}")
        time.sleep(1.5)

        print("[*] Bot is autofilling form fields with your details...")
        success = fill_application_form(page, room_name)

        if success:
            print("\n[+] Application submitted successfully!")
            print("[*] Sending 'Application Submitted' alert to your phone...")
            send_alert(
                f"✅ <b>[TEST] Application submitted!</b>\n\n"
                f"Room: <b>{room_name}</b>\n"
                f"Applicant: <b>{Config.FIRST_NAME} {Config.LAST_NAME}</b>\n"
                f"Matrikel: <b>{Config.MATRIKEL}</b>"
            )
        else:
            print("[-] Application simulation encountered an issue.")

        time.sleep(3.0)
        browser.close()

    if os.path.exists(mock_file):
        os.remove(mock_file)

    print("\n*** SIMULATION COMPLETE! Check your phone for notifications! ***\n")

if __name__ == "__main__":
    run_simulation()
