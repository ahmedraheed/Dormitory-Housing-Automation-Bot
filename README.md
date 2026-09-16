# StwDO Dorm Monitor Bot

> **Open-source** Python bot that watches the Studierendenwerk Dortmund (StwDO)
> housing portal and auto-submits dormitory applications the instant new rooms appear —
> with Telegram push alerts, stealth anti-bot evasion, and free GitHub Actions scheduling.

---

## ⚡ Quick Start (Local)

### 1 — Clone & install dependencies

```bash
git clone https://github.com/YOUR_USERNAME/stwdo-dorm-bot.git
cd stwdo-dorm-bot

python -m pip install -r requirements.txt
playwright install chromium --with-deps
```

### 2 — Configure your `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill in **your** details:

| Variable | Description |
|---|---|
| `APPLICANT_FIRST_NAME` | Your first name (Vorname) |
| `APPLICANT_LAST_NAME` | Your last name (Nachname) |
| `APPLICANT_EMAIL` | Your email address |
| `APPLICANT_PHONE` | Your phone number (e.g. `+4917012345678`) |
| `APPLICANT_MATRIKEL` | Matriculation number *(optional)* |
| `TELEGRAM_BOT_TOKEN` | Bot token from `@BotFather` |
| `TELEGRAM_CHAT_ID` | Your personal chat ID (see below) |
| `POLLING_INTERVAL_SECONDS` | How often to check (default `30`) |
| `MAX_RUNTIME_MINUTES` | Stop after N minutes (default `90`) |
| `HEADLESS` | `true` = no browser window; `false` = visible |

### 3 — Run the bot

```bash
python dorm_agent.py
```

> **Pro tip:** Run it on a Monday or Wednesday morning starting at ~09:50 Berlin time — that is when StwDO typically opens its drop windows.

---

## 📱 How to Get Your Telegram Chat ID

1. Open Telegram and search for `@BotFather`.
2. Send `/newbot` and follow the prompts to create your bot. Copy the **token**.
3. Start a conversation with your new bot (send any message to it).
4. Visit in your browser:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
5. Look in the JSON response for `"chat":{"id": 123456789}` — that number is your `TELEGRAM_CHAT_ID`.

---

## 🤖 GitHub Actions (Free CI/CD Scheduling)

GitHub Actions provides **free** scheduled execution (2,000 min/month on free tier).

### Step 1 — Push to GitHub

```bash
git remote add origin https://github.com/YOUR_USERNAME/stwdo-dorm-bot.git
git push -u origin main
```

### Step 2 — Add GitHub Secrets

Go to your repository → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add all the secrets from your `.env` file:

| Secret Name | Value |
|---|---|
| `APPLICANT_FIRST_NAME` | Your first name |
| `APPLICANT_LAST_NAME` | Your last name |
| `APPLICANT_EMAIL` | Your email |
| `APPLICANT_PHONE` | Your phone |
| `APPLICANT_MATRIKEL` | Your Matrikelnummer *(optional)* |
| `TELEGRAM_BOT_TOKEN` | Token from `@BotFather` |
| `TELEGRAM_CHAT_ID` | Your chat ID |

### Step 3 — Trigger manually for testing

Go to **Actions** → **StwDO Dorm Monitor** → **Run workflow** (top-right dropdown).

The workflow is pre-configured to run automatically at **09:55 CET** every Monday and Wednesday.

---

## 📁 Project Structure

```
stwdo-dorm-bot/
├── dorm_agent.py               # Main bot — all logic lives here
├── requirements.txt            # Python dependencies
├── .env.example                # Environment variable template
├── test_smoke.py               # Lightweight sanity-check test
└── .github/
    └── workflows/
        └── dorm_monitor.yml    # GitHub Actions workflow
```

---

## 🔧 Configuration Reference

All settings are controlled via environment variables (`.env` locally, GitHub Secrets in CI):

| Variable | Default | Description |
|---|---|---|
| `LISTINGS_URL` | StwDO aktuelle Wohnangebote URL | Target page to monitor |
| `POLLING_INTERVAL_SECONDS` | `30` | Seconds between checks |
| `MAX_RUNTIME_MINUTES` | `90` | Session timeout |
| `HEADLESS` | `true` | Browser visibility |
| `MIN_ACTION_DELAY_MS` | `300` | Min random delay between actions |
| `MAX_ACTION_DELAY_MS` | `1200` | Max random delay between actions |

---

## 🛡️ Anti-Bot Measures

The bot implements several stealth techniques to avoid detection:

- **`playwright-stealth`** — patches dozens of browser fingerprinting vectors
- **Rotating user-agents** — randomly selects from 4 realistic desktop UA strings
- **Randomised viewport sizes** — 4 common desktop resolutions
- **Human-like typing** — character-by-character input with random delays
- **Random inter-action delays** — configurable min/max millisecond range
- **Berlin timezone + German locale** — browser fingerprint matches target region
- **`navigator.webdriver` patch** — overrides the automation detection flag
- **Resource blocking** — fonts and media blocked to reduce load time without triggering WAFs

---

## ⚠️ Legal & Ethical Notice

> This bot automates form submission on a public portal. **You are solely responsible**
> for ensuring your use complies with StwDO's terms of service, German law (including
> the *Bundesdatenschutzgesetz*), and the portal's `robots.txt`. Only use this tool
> to apply for rooms for yourself. Do not run at abusive frequency. The default
> `POLLING_INTERVAL_SECONDS=30` is intentionally conservative.

---

## 🐛 Troubleshooting

| Symptom | Fix |
|---|---|
| `EnvironmentError: Missing required env vars` | Copy `.env.example` → `.env` and fill all fields |
| `playwright install chromium` fails | Run `playwright install-deps chromium` first (Linux) |
| Telegram messages not received | Double-check `TELEGRAM_BOT_TOKEN` format and `TELEGRAM_CHAT_ID` |
| Bot finds no listings | Portal may be closed; try running during 10:00–10:30 Mon/Wed Berlin time |
| Form fields not found | StwDO may have changed their form HTML; open a GitHub issue with a screenshot |

---

## 📜 License

MIT — free to use, modify, and distribute. See `LICENSE` for details.
