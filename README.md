# 📢 Telegram Course Bot (SQLite)

Posts free/discounted courses to your Telegram channel on a timer.
Uses a local **SQLite** file — zero database setup required.

**Render / full stack:** run `bot_with_healthcheck.py` — HTTP server with `/health`, web **dashboard**, market **JSON APIs**, and optional **Indian index dip alerts** to Telegram (default [@Its_Mirror_Here](https://t.me/Its_Mirror_Here)).

---

## 📁 Files

```
tgbot2/
├── bot.py                      ← Courses only (no HTTP)
├── bot_with_healthcheck.py    ← Courses + HTTP + market (use on Render)
├── market_service.py           ← Nifty / Sensex / Nifty BeES, dip alerts
├── market_backtest.py          ← Dip-buy vs monthly SIP simulation
├── requirements.txt
├── .env.example
├── .env                        ← Your secrets (create locally; gitignored)
└── posted_courses.db           ← Auto-created
```

---

## 🚀 Setup (3 steps)

### 1. Install Python packages
```bash
pip install -r requirements.txt
```

### 2. Create your `.env` file
```bash
cp .env.example .env
```
Edit `.env`:
```env
BOT_TOKEN=123456:ABCDefgh...        # from @BotFather
CHANNEL_ID=@your_channel_name       # your channel username
```

> **Bot must be Admin of the channel** with "Post Messages" permission.

### 3. Run

```bash
# Local: courses only
python bot.py

# Render / courses + dashboard + market alerts
python bot_with_healthcheck.py
```

---

## 📉 Market tracking & dip alerts (`bot_with_healthcheck.py`)

- **Data:** [Yahoo Finance](https://finance.yahoo.com/) via `yfinance` (delayed / unofficial — not NSE’s official feed).
- **Tracked by default:** Nifty 50 (`^NSEI`), Sensex (`^BSESN`), Nifty BeES (`NIFTYBEES.NS`). Override with `MARKET_SYMBOLS` (see `.env.example`).
- **Dip rule:** when last price vs **previous session close** is down by at least `DIP_THRESHOLD_PERCENT` (e.g. `1` = 1%), the bot sends **one Telegram message per symbol per calendar day** (IST), with a short “SIP the dip” style note.
- **Recipient:** `MARKET_ALERT_CHAT_ID` defaults to `@Its_Mirror_Here`. For a **private** chat the user must **start your bot** (`/start`) first; if `@username` fails, use their numeric `chat_id`.
- **SMS / WhatsApp:** not wired in code (would need Twilio, WhatsApp Business API, etc.). Alerts are **Telegram** for now.

### HTTP endpoints (same `PORT` as Render)

| Path | Purpose |
|------|--------|
| `/health` | Plain-text liveness (Render health check) |
| `/` or `/dashboard` | Minimal UI: today’s % vs prev close |
| `/api/market` | JSON snapshots |
| `/api/dip-status` | Same data + **would a dip alert fire now?** (optional `threshold=1` to simulate another %) |
| `/api/backtest` | Query: `ticker`, `start`, `amount`, `dip` — compares “buy ₹X on each ≥dip% down day” vs “monthly SIP same ₹X” |

Example:

`/api/backtest?ticker=^NSEI&start=2015-01-01&amount=5000&dip=1`

### See “real-time” checks (best effort)

Data is **Yahoo via yfinance**, often **delayed** vs exchange ticks (not official NSE).

The bot compares **last vs previous session close** and alerts when that change is **≤ −DIP_THRESHOLD_PERCENT**.

- **Telegram:** **`/market`** — pulls a **fresh** snapshot and shows, per symbol, Δ% vs prev close and whether an alert **would** fire on the next monitor tick (and if it was **already sent today**).
- **Browser / curl:** **`GET /api/dip-status`** (optional **`?threshold=1`**) — same logic as JSON.

### Try a dip alert without waiting for the market

- **Telegram:** send **`/testdip`** to your bot (private chat). You get the **same template** as a real dip alert, with a **TEST** banner and fake numbers (e.g. Nifty down 1.25%).
- **HTTP (optional):** set **`TEST_ALERT_SECRET`** in the environment, then open or `curl`:

  `http://localhost:PORT/api/test-alert?secret=YOUR_SECRET`

  That sends the sample text to **`MARKET_ALERT_CHAT_ID`** (same destination as live dip alerts).

---

## 🗄️ How SQLite Works Here

- On first run → creates `posted_courses.db` in the same folder
- Every posted course ID is saved with `INSERT OR IGNORE`
- On every check → skips any ID already in the DB
- **Survives restarts** — the `.db` file persists on disk
- No server, no account, no configuration needed

### View posted courses anytime:
```bash
# Install sqlite3 CLI (usually pre-installed on Mac/Linux)
sqlite3 posted_courses.db "SELECT * FROM posted_courses ORDER BY posted_at DESC LIMIT 20;"
```

---

## ⚙️ Settings

| Location | Description |
|----------|-------------|
| `CHECK_EVERY` in `bot.py` / `bot_with_healthcheck.py` | Seconds between course API polls (default `180`) |
| `.env` | See `.env.example` for `MARKET_*`, `DIP_THRESHOLD_PERCENT`, `MARKET_ALERT_CHAT_ID` |

---

## 🪟 Keep Running on Windows

```bash
# Simple — just keep terminal open
python bot.py

# Or run hidden in background
pythonw bot.py
```

## 🐧 Keep Running on Linux/Mac

```bash
nohup python bot.py &
```
