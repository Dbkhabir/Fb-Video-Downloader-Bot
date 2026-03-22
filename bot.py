import os
import re
import time
import uuid
import logging
import asyncio
import sqlite3
import datetime
import traceback
from collections import defaultdict
import aiohttp
import aiofiles
from dotenv import load_dotenv

# Telegram imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError, Forbidden, TimedOut, NetworkError
from pyrogram import Client

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ZYLA_API_KEY = os.getenv("ZYLA_API_KEY")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH")
ADMIN_IDS =[int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL")
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", 20))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 20))
BOT_USERNAME = os.getenv("BOT_USERNAME", "NewSocialDLBot")

# Regex for Facebook URLs
FB_REGEX = r"(?:https?:\/\/)?(?:www\.|m\.|web\.)?(?:facebook\.com|fb\.watch|fb\.com)\/(?:video\.php\?v=\d+|watch\/?\?v=\d+|share\/v\/[a-zA-Z0-9_]+|share\/r\/[a-zA-Z0-9_]+|reels?\/[a-zA-Z0-9_]+|.+?\/videos\/\d+)"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global variables
VIDEO_CACHE = {}
USER_MESSAGE_TIMES = defaultdict(list)
MAINTENANCE_MODE = False
pyro_client = None

# ----------------- DATABASE MODULE -----------------

async def db_query(query: str, params: tuple = (), fetchone=False, fetchall=False, commit=False):
    def _execute():
        with sqlite3.connect("bot.db", check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(query, params)
            if commit:
                conn.commit()
            if fetchone:
                row = cur.fetchone()
                return dict(row) if row else None
            if fetchall:
                return[dict(row) for row in cur.fetchall()]
            return cur.lastrowid
    return await asyncio.to_thread(_execute)

async def setup_db():
    queries =[
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
            join_date TEXT, last_active TEXT, is_banned INTEGER DEFAULT 0, ban_reason TEXT,
            is_premium INTEGER DEFAULT 0, total_downloads INTEGER DEFAULT 0,
            daily_downloads INTEGER DEFAULT 0, last_dl_date TEXT,
            referral_code TEXT UNIQUE, referred_by INTEGER, referral_count INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, video_url TEXT,
            video_title TEXT, quality TEXT, file_size INTEGER, timestamp TEXT, status TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY, total_users INTEGER DEFAULT 0, new_users INTEGER DEFAULT 0,
            active_users INTEGER DEFAULT 0, total_downloads INTEGER DEFAULT 0,
            successful_dl INTEGER DEFAULT 0, failed_dl INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT,
            message TEXT, timestamp TEXT
        )"""
    ]
    for q in queries:
        await db_query(q, commit=True)

# ----------------- UTILITY FUNCTIONS -----------------

def get_current_date():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

def format_size(bytes_size):
    if bytes_size == 0:
        return "Unknown"
    for unit in['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0

async def generate_progress_bar(current, total, prefix="Progress"):
    if total <= 0:
        return f"⏳ {prefix}...\n📦 {format_size(current)} downloaded."
    percent = current / total * 100
    filled = int(percent / 5)
    bar = "█" * filled + "░" * (20 - filled)
    return f"⏳ {prefix}...\n{bar} {percent:.1f}%\n📦 {format_size(current)} / {format_size(total)}"

async def update_progress_msg(message, current, total, prefix, last_update_dict):
    now = time.time()
    if now - last_update_dict.get('time', 0) > 3 or current == total:
        last_update_dict['time'] = now
        text = await generate_progress_bar(current, total, prefix)
        try:
            await message.edit_text(text)
        except Exception:
            pass

async def pyro_progress_callback(current, total, message, last_update_dict):
    await update_progress_msg(message, current, total, "Uploading (Enhanced) 🚀", last_update_dict)

async def download_file(url, dest_path, message):
    last_update_dict = {'time': time.time()}
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise Exception(f"HTTP Error {response.status}")
            total_size = int(response.headers.get('Content-Length', 0))
            downloaded = 0
            async with aiofiles.open(dest_path, 'wb') as f:
                async for chunk in response.content.iter_chunked(2 * 1024 * 1024):
                    await f.write(chunk)
                    downloaded += len(chunk)
                    await update_progress_msg(message, downloaded, total_size, "Downloading 📥", last_update_dict)
            return total_size

async def get_video_info(url: str):
    api_url = f"https://zylalabs.com/api/2013/facebook+video+downloader+api/1888/get+video?url={url}"
    headers = {"Authorization": f"Bearer {ZYLA_API_KEY}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(api_url, headers=headers) as response:
            if response.status != 200:
                return None
            data = await response.json()
            return data

async def get_file_size(url: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(url, allow_redirects=True) as response:
                return int(response.headers.get('Content-Length', 0))
    except Exception:
        return 0

# ----------------- SECURITY & CHECKS -----------------

async def is_flood(user_id):
    now = time.time()
    USER_MESSAGE_TIMES[user_id] = [t for t in USER_MESSAGE_TIMES[user_id] if now - t < 10]
    if len(USER_MESSAGE_TIMES[user_id]) >= 6:
        return True
    USER_MESSAGE_TIMES[user_id].append(now)
    return False

async def get_user(user_id):
    return await db_query("SELECT * FROM users WHERE user_id=?", (user_id,), fetchone=True)

async def register_user(user, referred_by=None):
    existing = await get_user(user.id)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if not existing:
        ref_code = str(uuid.uuid4())[:8]
        await db_query(
            """INSERT INTO users (user_id, username, first_name, last_name, join_date, last_active, referral_code, referred_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user.id, user.username, user.first_name, user.last_name, now, now, ref_code, referred_by),
            commit=True
        )
        await db_query(
            "INSERT OR IGNORE INTO daily_stats (date, new_users) VALUES (?, 0)",
            (get_current_date(),), commit=True
        )
        await db_query(
            "UPDATE daily_stats SET new_users = new_users + 1, total_users = total_users + 1 WHERE date=?",
            (get_current_date(),), commit=True
        )
        if referred_by:
            await db_query("UPDATE users SET referral_count = referral_count + 1 WHERE user_id=?", (referred_by,), commit=True)
    else:
        await db_query("UPDATE users SET last_active=?, username=?, first_name=?, last_name=? WHERE user_id=?",
                       (now, user.username, user.first_name, user.last_name, user.id), commit=True)
    return await get_user(user.id)

async def check_force_sub(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if not FORCE_SUB_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(FORCE_SUB_CHANNEL, user_id)
        if member.status in ['left', 'kicked']:
            return False
        return True
    except TelegramError:
        return False

# ----------------- BOT HANDLERS -----------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_flood(update.effective_user.id):
        return
    
    args = context.args
    referred_by = None
    if args and args[0].startswith("ref_"):
        ref_code = args[0].replace("ref_", "")
        referrer = await db_query("SELECT user_id FROM users WHERE referral_code=?", (ref_code,), fetchone=True)
        if referrer and referrer['user_id'] != update.effective_user.id:
            referred_by = referrer['user_id']
            
    await register_user(update.effective_user, referred_by)
    
    welcome_text = (
        f"👋 Welcome to **SocialDL Bot | FB Downloader**, {update.effective_user.first_name}!\n\n"
        "🎬 Send me any Facebook Video, Reel, or Watch link, and I will download it for you in the best quality!\n\n"
        "⚡ Fast & Smart Downloads\n"
        "🔗 Just paste your link below to get started!"
    )
    keyboard =[[InlineKeyboardButton("📖 Help", callback_data="cmd_help"), InlineKeyboardButton("📊 My Stats", callback_data="cmd_stats")],[InlineKeyboardButton("🔗 Referral", callback_data="cmd_referral"), InlineKeyboardButton("ℹ️ About", callback_data="cmd_about")]
    ]
    await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 **How to use this bot:**\n\n"
        "1️⃣ Copy a video link from Facebook (Reel, Watch, Post).\n"
        "2️⃣ Paste it in this chat.\n"
        "3️⃣ Choose your preferred quality (HD/SD).\n"
        "4️⃣ Wait a moment, and the video is yours!\n\n"
        "📊 **File Support:**\n"
        "🔸 Under 50MB: Instant upload ⚡\n"
        "🔸 50MB - 2GB: Enhanced upload 🚀\n"
        "🔸 Above 2GB: Direct Link 🔗\n\n"
        "Commands:\n"
        "/mystats - Check your limits\n"
        "/history - Download history\n"
        "/referral - Earn limits by referring\n"
        "/feedback <msg> - Send feedback"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(help_text, parse_mode="Markdown")
    else:
        await update.message.reply_text(help_text, parse_mode="Markdown")

async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)
    if not user:
        return
    
    if user['last_dl_date'] != get_current_date():
        await db_query("UPDATE users SET daily_downloads=0, last_dl_date=? WHERE user_id=?", (get_current_date(), user_id), commit=True)
        user['daily_downloads'] = 0

    stats_text = (
        "📊 **Your Statistics:**\n\n"
        f"👑 **Premium:** {'Yes ✅' if user['is_premium'] else 'No ❌'}\n"
        f"📥 **Total Downloads:** {user['total_downloads']}\n"
        f"📈 **Today's Downloads:** {user['daily_downloads']} / {DAILY_LIMIT if not user['is_premium'] else 'Unlimited'}\n"
        f"👥 **People Referred:** {user['referral_count']}"
    )
    
    if update.callback_query:
        await update.callback_query.edit_message_text(stats_text, parse_mode="Markdown")
    else:
        await update.message.reply_text(stats_text, parse_mode="Markdown")

async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if not user:
        return
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user['referral_code']}"
    text = (
        "🔗 **Referral Program**\n\n"
        "Invite your friends and increase your daily limits!\n\n"
        f"**Your Link:** `{ref_link}`\n"
        f"**Total Invites:** {user['referral_count']}"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: `/feedback your message here`", parse_mode="Markdown")
        return
    msg = " ".join(context.args)
    await db_query("INSERT INTO feedback (user_id, username, message, timestamp) VALUES (?, ?, ?, ?)",
                   (update.effective_user.id, update.effective_user.username, msg, datetime.datetime.now().isoformat()), commit=True)
    await update.message.reply_text("✅ Thank you! Your feedback has been sent to the admins.")
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_message(admin, f"📩 **New Feedback from {update.effective_user.first_name}:**\n\n{msg}", parse_mode="Markdown")
        except:
            pass

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    user_id = update.effective_user.id
    limit = 5
    offset = page * limit
    dls = await db_query("SELECT * FROM downloads WHERE user_id=? AND status='Success' ORDER BY timestamp DESC LIMIT ? OFFSET ?", (user_id, limit, offset + 1), fetchall=True)
    
    if not dls and page == 0:
        msg = "📭 You haven't downloaded anything yet."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return

    text = f"📜 **Your Download History (Page {page + 1})**\n\n"
    for d in dls[:limit]:
        text += f"🎬 {d['video_title'][:30]}...\n📥 Quality: {d['quality']} | 📦 Size: {format_size(d['file_size'])}\n📅 {d['timestamp'][:10]}\n\n"

    buttons =[]
    if page > 0:
        buttons.append(InlineKeyboardButton("◀️ Prev", callback_data=f"hist_{page-1}"))
    if len(dls) > limit:
        buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"hist_{page+1}"))
    
    keyboard = [buttons] if buttons else[]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# ----------------- ADMIN HANDLERS -----------------

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    keyboard = [[InlineKeyboardButton("📊 Dashboard", callback_data="adm_dash"), InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast")],[InlineKeyboardButton("⚙️ Settings", callback_data="adm_settings"), InlineKeyboardButton("📤 Export", callback_data="adm_export")]
    ]
    await update.message.reply_text("👑 **Admin Panel**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("❌ Usage: `/broadcast message`")
        return
    
    msg = update.message.text.split(" ", 1)[1]
    users = await db_query("SELECT user_id FROM users", fetchall=True)
    
    status_msg = await update.message.reply_text("📢 Broadcasting...")
    sent, failed = 0, 0
    
    for u in users:
        try:
            await context.bot.send_message(u['user_id'], msg)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
        
    await status_msg.edit_text(f"✅ **Broadcast Complete**\n📤 Sent: {sent}\n❌ Failed/Blocked: {failed}", parse_mode="Markdown")

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: `/ban user_id reason`")
        return
    uid = int(context.args[0])
    reason = " ".join(context.args[1:])
    await db_query("UPDATE users SET is_banned=1, ban_reason=? WHERE user_id=?", (reason, uid), commit=True)
    await update.message.reply_text(f"✅ User {uid} banned.")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 1:
        await update.message.reply_text("❌ Usage: `/unban user_id`")
        return
    uid = int(context.args[0])
    await db_query("UPDATE users SET is_banned=0 WHERE user_id=?", (uid,), commit=True)
    await update.message.reply_text(f"✅ User {uid} unbanned.")

async def user_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 1:
        await update.message.reply_text("❌ Usage: `/user user_id`")
        return
    uid = int(context.args[0])
    user = await get_user(uid)
    if not user:
        await update.message.reply_text("❌ User not found.")
        return
    text = (
        f"👤 **User Info:**\n"
        f"ID: `{user['user_id']}`\n"
        f"Name: {user['first_name']} {user['last_name']}\n"
        f"Banned: {user['is_banned']} ({user['ban_reason']})\n"
        f"Total DLs: {user['total_downloads']}\n"
        f"Premium: {user['is_premium']}"
    )
    buttons = [[InlineKeyboardButton("🚫 Ban", callback_data=f"adm_ban_{uid}"), InlineKeyboardButton("✅ Unban", callback_data=f"adm_unban_{uid}")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    users = await db_query("SELECT * FROM users", fetchall=True)
    async with aiofiles.open("users.txt", "w") as f:
        await f.write(str(users))
        
    await context.bot.send_document(update.effective_user.id, open("users.txt", "rb"))
    os.remove("users.txt")

# ----------------- VIDEO DOWNLOAD FLOW -----------------

async def handle_fb_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if await is_flood(user_id):
        return
        
    url = update.message.text
    if not re.search(FB_REGEX, url):
        return 
    
    if MAINTENANCE_MODE and user_id not in ADMIN_IDS:
        await update.message.reply_text("🛠 Bot is under maintenance. Please try again later.")
        return

    user = await register_user(update.effective_user)
    
    if user['is_banned']:
        await update.message.reply_text(f"🚫 You are banned. Reason: {user['ban_reason']}")
        return

    if not await check_force_sub(context, user_id):
        btn = [[InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCE_SUB_CHANNEL.replace('@','')}"),
                InlineKeyboardButton("✅ I've Joined", callback_data="check_sub")]]
        await update.message.reply_text("🛑 You must join our channel to use this bot!", reply_markup=InlineKeyboardMarkup(btn))
        return

    if user['last_dl_date'] != get_current_date():
        await db_query("UPDATE users SET daily_downloads=0, last_dl_date=? WHERE user_id=?", (get_current_date(), user_id), commit=True)
        user['daily_downloads'] = 0

    if not user['is_premium'] and user['daily_downloads'] >= DAILY_LIMIT:
        await update.message.reply_text("❌ Daily limit reached! Wait until tomorrow or refer friends for more.")
        return

    msg = await update.message.reply_text("⏳ Processing your link...")
    
    video_data = await get_video_info(url)
    if not video_data or 'sd_url' not in video_data:
        await msg.edit_text("❌ Failed to fetch video. Make sure the link is public.")
        return

    vid_id = str(uuid.uuid4())[:8]
    
    hd_size = await get_file_size(video_data.get('hd_url')) if video_data.get('hd_url') else 0
    sd_size = await get_file_size(video_data.get('sd_url')) if video_data.get('sd_url') else 0
    
    VIDEO_CACHE[vid_id] = {
        'url': url,
        'title': video_data.get('title', 'Facebook Video'),
        'hd_url': video_data.get('hd_url'),
        'sd_url': video_data.get('sd_url'),
        'timestamp': time.time()
    }

    buttons =[]
    if video_data.get('hd_url'):
        buttons.append([InlineKeyboardButton(f"📥 HD ({format_size(hd_size)})", callback_data=f"dl|{vid_id}|HD")])
    if video_data.get('sd_url'):
        buttons.append([InlineKeyboardButton(f"📥 SD ({format_size(sd_size)})", callback_data=f"dl|{vid_id}|SD")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="dl_cancel")])

    title = video_data.get('title', 'Facebook Video')
    await msg.edit_text(f"🎬 **{title[:50]}**...\n\nSelect Quality:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "check_sub":
        if await check_force_sub(context, user_id):
            await query.message.edit_text("✅ Thank you for joining! Send your link again.")
        else:
            await query.message.reply_text("❌ You haven't joined the channel yet!")
    
    elif data.startswith("cmd_"):
        cmd = data.split("_")[1]
        if cmd == "help": await help_command(update, context)
        elif cmd == "stats": await mystats_command(update, context)
        elif cmd == "referral": await referral_command(update, context)
        elif cmd == "about": await query.edit_message_text("🤖 **SocialDL Bot | FB Downloader**\nBuilt for downloading FB videos efficiently.", parse_mode="Markdown")
            
    elif data.startswith("hist_"):
        page = int(data.split("_")[1])
        await history_command(update, context, page)
        
    elif data.startswith("adm_"):
        if user_id not in ADMIN_IDS: return
        action = data.replace("adm_", "")
        if action == "dash":
            stats = await db_query("SELECT * FROM daily_stats WHERE date=?", (get_current_date(),), fetchone=True)
            u_count = await db_query("SELECT COUNT(*) as c FROM users", fetchone=True)
            text = f"📊 **Dashboard**\n\nTotal Users: {u_count['c']}\nNew Today: {stats['new_users'] if stats else 0}\nDownloads Today: {stats['successful_dl'] if stats else 0}"
            await query.edit_message_text(text, parse_mode="Markdown")
        elif action.startswith("ban_"):
            uid = int(action.split("_")[1])
            await db_query("UPDATE users SET is_banned=1 WHERE user_id=?", (uid,), commit=True)
            await query.edit_message_text(f"✅ User {uid} banned.")
        elif action.startswith("unban_"):
            uid = int(action.split("_")[1])
            await db_query("UPDATE users SET is_banned=0 WHERE user_id=?", (uid,), commit=True)
            await query.edit_message_text(f"✅ User {uid} unbanned.")

    elif data == "dl_cancel":
        await query.message.edit_text("❌ Download Cancelled.")

    elif data.startswith("dl|"):
        _, vid_id, quality = data.split("|")
        vdata = VIDEO_CACHE.get(vid_id)
        if not vdata:
            await query.message.edit_text("❌ Link expired. Please send again.")
            return

        dl_url = vdata['hd_url'] if quality == "HD" else vdata['sd_url']
        if not dl_url:
            await query.message.edit_text("❌ Quality not available.")
            return

        os.makedirs("downloads", exist_ok=True)
        file_path = f"downloads/{uuid.uuid4()}.mp4"
        
        try:
            file_size = await download_file(dl_url, file_path, query.message)
            
            dl_id = await db_query(
                "INSERT INTO downloads (user_id, video_url, video_title, quality, file_size, timestamp, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, vdata['url'], vdata['title'], quality, file_size, datetime.datetime.now().isoformat(), 'Pending'),
                commit=True
            )

            user = await get_user(user_id)
            caption = f"🎬 {vdata['title'][:40]}...\n📥 Quality: {quality}\n📦 Size: {format_size(file_size)}\n📈 Daily count: {user['daily_downloads']+1}/{DAILY_LIMIT if not user['is_premium'] else '∞'}\n🤖 @{BOT_USERNAME}"

            if file_size > 2 * 1024 * 1024 * 1024:
                await query.message.edit_text(f"🔗 File too large (>2GB). Direct Link: [Click Here]({dl_url})", parse_mode="Markdown")
            
            elif file_size > 50 * 1024 * 1024:
                await query.message.edit_text("🚀 Uploading via Enhanced Server (Pyrogram)...")
                last_update_dict = {'time': time.time()}
                await pyro_client.send_video(
                    chat_id=user_id,
                    video=file_path,
                    caption=caption,
                    progress=pyro_progress_callback,
                    progress_args=(query.message, last_update_dict)
                )
                await query.message.delete()
                
            else:
                await query.message.edit_text("⚡ Uploading via Fast Server...")
                with open(file_path, 'rb') as video_file:
                    await context.bot.send_video(
                        chat_id=user_id,
                        video=video_file,
                        caption=caption,
                        read_timeout=120,
                        write_timeout=120
                    )
                await query.message.delete()

            await db_query("UPDATE downloads SET status='Success' WHERE id=?", (dl_id,), commit=True)
            await db_query("UPDATE users SET total_downloads = total_downloads + 1, daily_downloads = daily_downloads + 1 WHERE user_id=?", (user_id,), commit=True)
            await db_query("UPDATE daily_stats SET successful_dl = successful_dl + 1, total_downloads = total_downloads + 1 WHERE date=?", (get_current_date(),), commit=True)

        except Exception as e:
            logger.error(f"Download error: {traceback.format_exc()}")
            await query.message.edit_text("❌ An error occurred during download/upload.")
            await db_query("UPDATE daily_stats SET failed_dl = failed_dl + 1 WHERE date=?", (get_current_date(),), commit=True)
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

# ----------------- ERROR HANDLER -----------------

async def global_error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    try:
        if update and update.effective_user:
            await context.bot.send_message(update.effective_user.id, "❌ An internal error occurred. Admins have been notified.")
        for admin in ADMIN_IDS:
            await context.bot.send_message(admin, f"⚠️ Error:\n`{context.error}`", parse_mode="Markdown")
    except:
        pass

# ----------------- SCHEDULED JOBS -----------------

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    stats = await db_query("SELECT * FROM daily_stats WHERE date=?", (date,), fetchone=True)
    if not stats: return
    text = f"📊 **Daily Report ({date})**\n\nNew Users: {stats['new_users']}\nSuccessful DLs: {stats['successful_dl']}\nFailed DLs: {stats['failed_dl']}"
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_message(admin, text, parse_mode="Markdown")
        except: pass

async def cleanup_cache(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    keys_to_del =[k for k, v in VIDEO_CACHE.items() if now - v['timestamp'] > 1800]
    for k in keys_to_del:
        del VIDEO_CACHE[k]

# ----------------- POST INIT & SHUTDOWN -----------------

async def post_init(app: Application):
    await setup_db()
    await pyro_client.start()
    logger.info("Bot & Database Initialized. Pyrogram Started.")

async def post_shutdown(app: Application):
    await pyro_client.stop()
    logger.info("Pyrogram Stopped.")

# ----------------- MAIN EXECUTION -----------------

def main():
    global pyro_client
    
    pyro_client = Client(
        "fb_dl_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        no_updates=True,
        in_memory=True
    )

    app = Application.builder() \
        .token(BOT_TOKEN) \
        .post_init(post_init) \
        .post_shutdown(post_shutdown) \
        .build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("mystats", mystats_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("referral", referral_command))
    app.add_handler(CommandHandler("feedback", feedback_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("user", user_info_command))
    app.add_handler(CommandHandler("export", export_command))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_fb_message))
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    app.add_error_handler(global_error_handler)

    jq = app.job_queue
    jq.run_daily(daily_report, time=datetime.time(hour=0, minute=0, tzinfo=datetime.timezone.utc))
    jq.run_repeating(cleanup_cache, interval=1800)

    logger.info("Starting Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()