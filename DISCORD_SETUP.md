# 🤖 Discord Bot Setup Guide

## Step 1 — Create a Discord Application

1. Go to https://discord.com/developers/applications
2. Click **New Application** → name it "Lazada Pokémon Bot"
3. Go to the **Bot** tab → click **Add Bot**
4. Under **Token** → click **Reset Token** → copy it (you'll need this)
5. Scroll down → enable **Message Content Intent** (toggle ON)

## Step 2 — Invite the Bot to Your Server

1. Go to **OAuth2 → URL Generator**
2. Under Scopes, check: `bot` and `applications.commands`
3. Under Bot Permissions, check:
   - Send Messages
   - Embed Links
   - Use Slash Commands
4. Copy the generated URL → open it → invite to your server

## Step 3 — Set Railway Environment Variables

In Railway → your service → **Variables**, add:

| Variable | Value |
|----------|-------|
| `DISCORD_BOT_TOKEN` | Your bot token from Step 1 |
| `API_BASE` | `http://localhost:8000` (both run in same process) |
| `ALERT_CHANNEL_ID` | (optional) Channel ID for auto-alerts — or use `/setalerts` |

**To get a Channel ID:** Right-click a channel in Discord → Copy Channel ID (enable Developer Mode in Discord settings first)

## Step 4 — Deploy to Railway

```bash
git add .
git commit -m "add discord bot"
git push
```

Railway redeploys automatically. Within ~30 seconds your bot will be online.

## Step 5 — Use the Bot

| Command | What it does |
|---------|-------------|
| `/track <url> <price>` | Start tracking a Lazada product |
| `/list` | See all tracked products |
| `/check <id>` | Refresh price & stock right now |
| `/checkout <id>` | Get a direct buy link |
| `/remove <id>` | Stop tracking |
| `/setalerts #channel` | Set auto-alert channel (admin only) |
| `/help` | Show all commands |

## How alerts work

- The bot polls for new alerts every **30 seconds**
- The backend scrapes Lazada every **15 minutes**
- When a price hits your target, drops 5%+, or comes back in stock → Discord embed fires automatically in your alert channel
- Use `/checkout <id>` to jump straight to the Lazada checkout page
