# SocialDL Bot | FB Downloader (@NewSocialDLBot) 🚀

An advanced, asynchronous Telegram Bot to download videos from Facebook. Equipped with smart file uploading based on size, SQLite DB, Admin dashboards, Referral systems, and ZylaLabs API integration.

## Features ✨
- **Smart Upload:** 
  - <=50MB handles via standard Telegram Bot API (Super Fast).
  - 50MB - 2GB handles via Pyrogram MTProto with Progress bar updates.
  - >2GB sends Direct Download Links.
- **Progress Tracking:** Updates UI every 3 seconds to avoid rate-limits while showing exact `MB` & `%`.
- **Admin Panel:** Check DB stats, ban/unban users, broadcast messages, export databases.
- **Force Subscription:** Ensure users join a specified channel before using.
- **Daily Limits & Referrals:** Generate custom referral URLs for users to invite friends and lift daily limits.

## How to Deploy on Railway via GitHub 🚂

1. Create a GitHub repository and push all these files.
2. Sign up or log into [Railway.app](https://railway.app/).
3. Click **New Project** -> **Deploy from GitHub repo**.
4. Select your repository.
5. Once added, go to **Variables** section in Railway and add the environment variables matching the `.env.example` file:
   - `BOT_TOKEN`
   - `ZYLA_API_KEY`
   - `API_ID` (Get from my.telegram.org)
   - `API_HASH` (Get from my.telegram.org)
   - `ADMIN_IDS` (comma separated Telegram User IDs)
   - `FORCE_SUB_CHANNEL` (e.g. `@mychannel`)
   - `DAILY_LIMIT` (e.g. 20)
   - `BOT_USERNAME` (NewSocialDLBot)
6. Wait for the deployment to finish! Railway will automatically detect `Procfile` and use Python 3.11.6 as defined in `runtime.txt`.