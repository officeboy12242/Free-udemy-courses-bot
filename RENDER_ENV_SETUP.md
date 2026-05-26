# Render Environment Variables Setup

After deploying to Render, you need to manually add these environment variables in the Render dashboard.

## How to Add Environment Variables on Render:

1. Go to your service on Render dashboard
2. Click on **Environment** tab
3. Click **Add Environment Variable**
4. Add each variable below

## Required Environment Variables:

### Bot Configuration (Required)
```
BOT_TOKEN=<your_telegram_bot_token>
CHANNEL_ID=<your_channel_id>
MARKET_ALERT_CHAT_ID=<your_market_chat_id>
MOVIES_CHANNEL_ID=<your_movies_channel_id>
SCRAPER_API_KEY=<your_scraperapi_key_if_any>
```

### Movie Site Base URLs (Required)
**These can be updated directly in Render dashboard without code push:**

```
HDHUB_BASE_URL=https://new1.hdhub4u.limo
HDH_BASE_URL=https://4khdhub.link
MD_BASE_URL=https://new2.moviesdrives.my
M4U_BASE_URL=https://movies4u.gr
VEGA_BASE_URL=https://vegamovies.global
SDMP_BASE_URL=https://sd1.sdmoviespoint.trade
BOLLYFLIX_BASE_URL=https://new.bollyflix.gd
MOVIESMOD_BASE_URL=https://moviesmod.farm
ZEEFLIZ_BASE_URL=https://zeefliz.beer
```

### System Configuration (Auto-set by render.yaml)
These are already defined in render.yaml with default values:
```
DIP_THRESHOLD_PERCENT=1
MIN_DEEPER_STEP=0.5
MARKET_POLL_INTERVAL=120
MARKET_FEATURES_ENABLED=1
PORT=10000
```

## When a Domain Changes:

**OLD WAY (Required code push):**
1. Edit render.yaml
2. Commit and push to GitHub
3. Wait for Render to redeploy

**NEW WAY (No code push needed):**
1. Go to Render dashboard → Your service → Environment
2. Find the base URL variable (e.g., `M4U_BASE_URL`)
3. Click **Edit** → Update the value → Save
4. Render will automatically restart with new URL

## Known Issues on Render Free Tier:

### Movies4U Requires Playwright

**Movies4U** (`movies4u.gr`) uses aggressive Sucuri JavaScript challenge that requires Playwright (headless browser) to bypass. 

**Status on Render:**
- Playwright is configured to install during build
- May fail on free tier due to disk space limits (~500MB browser binaries)
- If installation fails, Movies4U will return empty results with warning logs
- **Other sites (ZeeFliz, HDHub4u, etc.) will continue working normally**

**If Movies4U is critical:**
- Upgrade to Render paid plan (more disk space)
- Or use ScraperAPI (but Movies4U also blocks ScraperAPI sometimes)
- Or rely on alternative sources (ZeeFliz provides similar content)

## Current Base URLs (as of May 26, 2026):

| Variable | Current URL |
|----------|-------------|
| HDHUB_BASE_URL | https://new1.hdhub4u.limo |
| HDH_BASE_URL | https://4khdhub.link |
| MD_BASE_URL | https://new2.moviesdrives.my |
| M4U_BASE_URL | https://movies4u.gr |
| VEGA_BASE_URL | https://vegamovies.global |
| SDMP_BASE_URL | https://sd1.sdmoviespoint.trade |
| BOLLYFLIX_BASE_URL | https://new.bollyflix.gd |
| MOVIESMOD_BASE_URL | https://moviesmod.farm |
| ZEEFLIZ_BASE_URL | https://zeefliz.beer |
