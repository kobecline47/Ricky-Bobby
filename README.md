# Ricky Bobby - Disboard Bump Bot

A Discord bot that automatically bumps your server to Disboard every 2 hours (configurable).

## Features

- 🚀 **Automatic Bumping** - Bumps server every 2 hours to Disboard automatically
- 📊 **Statistics Tracking** - Track total bumps and failed attempts
- 💬 **Manual Bump** - `/bump` command to manually trigger a bump anytime
- 📋 **Scheduled Info** - `/nextbump` command to see when next bump is scheduled
- 📈 **Stats Command** - `/bumpstats` to view detailed statistics
- 📝 **Logging** - Optional logging channel to track all bump activity

## Setup

### Prerequisites
- Python 3.9+
- Discord Bot Token
- Disboard Server Listing (free at disboard.org)

### Installation

1. **Clone/Copy the project:**
   ```
   Copy RickyBobby folder to C:\Users\kobec\RickyBobby
   ```

2. **Install dependencies:**
   ```
   pip install -r requirements.txt
   ```

3. **Set up environment variables:**
   - Copy `.env.example` to `.env`
   - Add your Discord bot token
   - Add your Guild ID
   - Add your Disboard webhook URL

4. **Get Disboard Webhook:**
   - Go to https://disboard.org
   - Create/login to your server
   - In server settings, find the webhook URL under "Webhook"
   - Paste it in `.env` as `DISBOARD_WEBHOOK_URL`

### Running Locally

```
python RickyBobby.py
```

## Commands

### `/bump`
Manually bump the server to Disboard right now.

### `/bumpstats`
View statistics:
- Total bumps executed
- Failed bumps
- Last bump time
- Bump interval setting

### `/nextbump`
See exactly when the next automatic bump is scheduled.

## Deployment

### Docker (Recommended)

```
docker build -t rickyb Bobby .
docker run -d --env-file .env rickyb Bobby
```

### Railway

```
railway link
railway variables add DISCORD_TOKEN=<your_token> GUILD_ID=<your_guild> DISBOARD_WEBHOOK_URL=<webhook>
railway up
```

## Configuration

Edit `.env` to customize:

- `BUMP_INTERVAL_HOURS` - How often to bump (default: 2 hours)
- `LOG_CHANNEL_ID` - Channel for bump logs (optional)

## Troubleshooting

**Bot not bumping?**
- Check DISBOARD_WEBHOOK_URL is correct
- Verify bot is online: check console for "✅ Ricky Bobby#XXXX is online!"
- Check bump_data.json for error tracking

**"Bump failed with status 401"?**
- Disboard webhook URL might be wrong
- Regenerate webhook from Disboard settings

## Data Persistence

Bump statistics are saved in `bump_data.json`:
- Last bump timestamp
- Total bump count
- Failed bump count

This allows the bot to track bumps even after restarts.

## Notes

- Keep your bot token secret! Never commit `.env` to git
- The bot requires Disboard listing to be set up (free)
- Bumps are tracked locally and persisted to JSON file
- Works with any Discord server

---
Made with ❤️ - Ricky Bobby Bump Bot
