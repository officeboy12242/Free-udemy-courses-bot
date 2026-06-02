# Multi-Bot Parallel Downloads & Uploads

## 🚀 What This Does

With multiple bot tokens, your Telegram bot can:

1. **Download courses FASTER** - Split lectures across multiple bots downloading simultaneously
2. **Upload archives FASTER** - Each bot uploads a part of the ZIP in parallel

Example: With 4 bot tokens, a 120-lecture course is split into 4 batches of 30 lectures each, all downloading at the same time!

## 📋 Setup Instructions

### Step 1: Create Additional Bots

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` command
3. Follow prompts to create 3-5 additional bots
4. **Save each bot token** (format: `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`)

### Step 2: Add Bots as Channel Admins

For each new bot you created:

1. Go to your target Telegram channel
2. Click on channel name → Administrators → Add Administrator
3. Search for each bot by username
4. Grant these permissions:
   - ✅ Post Messages
   - ✅ Edit Messages
   - ✅ Delete Messages

### Step 3: Configure Environment Variable

In your `.env` file, add all bot tokens (comma-separated):

```env
# Main bot token (required)
BOT_TOKEN=1111111111:AAA-MainBotToken-Here

# Additional bots for parallel operations (optional)
UPLOAD_BOT_TOKENS=2222222222:BBB-Bot2-Token,3333333333:CCC-Bot3-Token,4444444444:DDD-Bot4-Token
```

**Important:**
- Don't include the main `BOT_TOKEN` in `UPLOAD_BOT_TOKENS`
- Separate multiple tokens with commas (no spaces needed)
- You can add 2-5 additional bots (more = faster)

### Step 4: Restart Bot

After adding tokens, restart your bot. You'll see:

```
✅ Bot pool initialized with 3 additional bot(s) for parallel operations
```

## 🎯 How It Works

### Example: 120-Lecture Course with 4 Bots

**Without Multi-Bot:**
```
Bot 1: Downloads all 120 lectures sequentially
Time: ~45 minutes
```

**With 4 Bots:**
```
Bot 1: Downloads lectures 1-30   (parallel)
Bot 2: Downloads lectures 31-60  (parallel)
Bot 3: Downloads lectures 61-90  (parallel)
Bot 4: Downloads lectures 91-120 (parallel)
Time: ~12 minutes  (4x faster!)
```

All downloads merge into one organized folder automatically!

## 📊 Performance Gains

| Bots | Speed Increase | Example: 100 Lectures |
|------|----------------|----------------------|
| 1 (default) | 1x | 30 minutes |
| 2 | ~2x | 15 minutes |
| 3 | ~3x | 10 minutes |
| 4 | ~4x | 7-8 minutes |
| 5 | ~4-5x | 6-7 minutes |

*Note: Actual speed depends on your server bandwidth and Udemy API limits*

## ⚙️ Configuration Tips

1. **Start with 3-4 bots** - More isn't always better due to API rate limits
2. **Monitor with `/downloads`** - View real-time progress of all parallel tasks
3. **Check disk space** - Multiple downloads need more temporary storage

## 🔧 Troubleshooting

### "Bot pool initialized with 0 bots"
- Check if `UPLOAD_BOT_TOKENS` is set in `.env`
- Verify tokens are valid (test with BotFather)
- Ensure no extra spaces in token list

### Downloads still slow
- Verify all bots are admins in target channel
- Check server bandwidth (`/downloads` shows stats)
- Try reducing number of bots if API rate-limited

### "Bot not found" errors
- Make sure each bot token is valid
- Add each bot to your channel as admin
- Restart the application after adding tokens

## 📝 Notes

- Main `BOT_TOKEN` is still used for user interactions
- Additional bots only handle downloads/uploads
- All bots must have same channel admin permissions
- Single-bot mode still works if no `UPLOAD_BOT_TOKENS` set
