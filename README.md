# Kors Tire Bot — Deployment Guide

## Railway.app (Free Hosting)

### Step 1: Create account
Go to railway.app → Sign up with GitHub

### Step 2: Upload files
New Project → Deploy from GitHub repo
(OR: use Railway CLI)

### Step 3: Set environment variables
In Railway dashboard → Variables → Add:
- TELEGRAM_TOKEN = (from BotFather)
- KOMMO_TOKEN = (your Kommo long-lived token)
- ANTHROPIC_API_KEY = (from console.anthropic.com)

### Step 4: Deploy
Railway auto-deploys. Bot will be live in ~2 minutes.

## How employees use it
1. Open Telegram → find the bot
2. Send photo of sticker OR screenshot from Marketplace
3. Bot reads it and creates lead in Kommo automatically
4. Bot replies with lead summary + link to Kommo
