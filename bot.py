import os
import logging
import requests
import json
import time
import tempfile
import asyncio
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from flask import Flask
from threading import Thread

# ==================== Logging ====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ==================== Config ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ZYLA_API_KEY = os.environ.get("ZYLA_API_KEY")
PORT = int(os.environ.get("PORT", 8080))
BOT_USERNAME = "@NewSocialDLBot"
BOT_VERSION = "4.0" # Updated Version
DEVELOPER = "@peranabik"
ZYLA_API_URL = "https://zylalabs.com/api/4146/facebook+download+api/7134/downloader"

# Limits
MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024  # 50MB
DOWNLOAD_TIMEOUT = 120
UPLOAD_TIMEOUT = 120

# ==================== Global Stats System ====================
STATS_FILE = "bot_stats.json"

def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"users":[], "total_downloads": 0}

def save_stats(stats):
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f)
    except Exception as e:
        logger.error(f"Failed to save stats: {e}")

bot_stats = load_stats()

def track_user(user_id):
    if user_id not in bot_stats["users"]:
        bot_stats["users"].append(user_id)
        save_stats(bot_stats)

def add_global_download():
    bot_stats["total_downloads"] += 1
    save_stats(bot_stats)

# ==================== Flask (Railway Health Check) ====================
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return f"✅ Bot is alive on Railway! Users: {len(bot_stats['users'])}", 200

@app_flask.route("/health")
def health():
    return "OK", 200

def run_flask():
    app_flask.run(host="0.0.0.0", port=PORT)

# ==================== Helpers ====================

def is_facebook_url(url):
    domains =["facebook.com", "fb.com", "fb.watch", "m.facebook.com", "web.facebook.com"]
    return any(d in url.lower() for d in domains)

def fetch_video_data(fb_url):
    headers = {"Authorization": f"Bearer {ZYLA_API_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(ZYLA_API_URL, headers=headers, data=json.dumps({"url": fb_url}), timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"API: {e}")
        return None

def fmt_dur(ms):
    if not ms: return "N/A"
    s = ms // 1000
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")

def q_icon(q):
    return {"HD": "🔵", "SD": "🟢", "Audio": "🟣"}.get(q, "⚪")

def get_size(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=10)
        return int(r.headers.get("content-length", 0))
    except:
        return 0

def fmt_size(b):
    if b <= 0: return "Unknown"
    for u in ["B", "KB", "MB", "GB"]:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

def cleanup(p):
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except:
        pass

def create_progress_bar(percent):
    filled = int(percent / 10)
    bar = "█" * filled + "░" * (10 - filled)
    return bar

# ==================== Async Download with Progress ====================
async def async_download_with_limit(url, ext="mp4", max_size=MAX_DOWNLOAD_SIZE, timeout=DOWNLOAD_TIMEOUT, progress_cb=None):
    try:
        start_time = time.time()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}", dir=tempfile.gettempdir())
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=timeout) as response:
                response.raise_for_status()
                total_size = int(response.headers.get('Content-Length', 0))
                
                if total_size > max_size:
                    tmp.close()
                    cleanup(tmp.name)
                    return None, total_size, "too_large"

                downloaded = 0
                chunk_size = 512 * 1024 # 512 KB chunks
                
                async for chunk in response.content.iter_chunked(chunk_size):
                    tmp.write(chunk)
                    downloaded += len(chunk)
                    
                    if time.time() - start_time > timeout:
                        tmp.close()
                        cleanup(tmp.name)
                        return None, downloaded, "timeout"

                    if downloaded > max_size:
                        tmp.close()
                        cleanup(tmp.name)
                        return None, downloaded, "too_large"
                    
                    if progress_cb and total_size > 0:
                        await progress_cb(downloaded, total_size, start_time)

        tmp.close()
        return tmp.name, downloaded, "ok"
    except Exception as e:
        logger.error(f"Async Download error: {e}")
        return None, 0, "error"

# ==================== Upload System ====================

async def smart_send(ctx, chat_id, url, mtype, qual, vdata, ext, file_size, status_cb=None):
    icon = q_icon(qual) if mtype == "video" else "🎵"
    size_label = fmt_size(file_size)

    caption = (
        f"✅ **Download Complete!**\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {vdata['title']}\n"
        f"👤 {vdata['author']}\n"
        f"{icon} Quality: **{qual}**\n"
        f"📦 Size: **{size_label}**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ {BOT_USERNAME}"
    )

    # ===== Check size limit =====
    if file_size > MAX_DOWNLOAD_SIZE:
        return False, "too_large"

    # ===== TIER 2: Download with Real-Time Progress =====
    last_edit_time = 0
    async def download_progress(current, total, start_time):
        nonlocal last_edit_time
        now = time.time()
        if now - last_edit_time > 2.0 or current == total: # Update every 2 seconds
            percent = (current / total) * 100
            bar = create_progress_bar(percent)
            speed = current / (now - start_time) if (now - start_time) > 0 else 0
            
            text = (
                f"📥 **Downloading to Server...**\n\n"
                f"📊 {bar} **{percent:.1f}%**\n"
                f"📦 {fmt_size(current)} / {fmt_size(total)}\n"
                f"🚀 Speed: {fmt_size(speed)}/s"
            )
            if status_cb: await status_cb(text)
            last_edit_time = now

    if status_cb: await status_cb(f"📥 **Starting Download...**\n📦 {size_label}")
    
    path, actual_size, dl_status = await async_download_with_limit(url, ext, progress_cb=download_progress)

    if dl_status != "ok" or not path:
        return False, dl_status

    actual_size_label = fmt_size(actual_size)
    
    # Uploading to Telegram
    if status_cb:
        await status_cb(f"📤 **Uploading to Telegram...**\n📦 {actual_size_label}\n⏳ This depends on Telegram's server speed, please wait...")

    try:
        with open(path, "rb") as f:
            if mtype == "video":
                await asyncio.wait_for(
                    ctx.bot.send_video(
                        chat_id=chat_id, video=f, caption=caption,
                        parse_mode="Markdown", supports_streaming=True,
                        filename=f"FB_{qual}_{int(time.time())}.{ext}",
                        read_timeout=UPLOAD_TIMEOUT, write_timeout=UPLOAD_TIMEOUT,
                    ),
                    timeout=UPLOAD_TIMEOUT + 30
                )
            else:
                await asyncio.wait_for(
                    ctx.bot.send_audio(
                        chat_id=chat_id, audio=f, caption=caption,
                        parse_mode="Markdown",
                        filename=f"FB_Audio_{int(time.time())}.{ext}",
                        read_timeout=UPLOAD_TIMEOUT, write_timeout=UPLOAD_TIMEOUT,
                    ),
                    timeout=UPLOAD_TIMEOUT + 30
                )
        cleanup(path)
        return True, "upload"
    except Exception as e:
        logger.warning(f"Media upload fail, trying document: {e}")

    # Try as document if media fails
    try:
        if status_cb: await status_cb(f"📄 **Sending as document...**\n📦 {actual_size_label}")
        with open(path, "rb") as f:
            await asyncio.wait_for(
                ctx.bot.send_document(
                    chat_id=chat_id, document=f, caption=caption,
                    parse_mode="Markdown",
                    filename=f"Facebook_{qual}_{int(time.time())}.{ext}",
                    read_timeout=UPLOAD_TIMEOUT, write_timeout=UPLOAD_TIMEOUT,
                ),
                timeout=UPLOAD_TIMEOUT + 30
            )
        cleanup(path)
        return True, "document"
    except Exception as e:
        logger.error(f"Doc upload fail: {e}")

    cleanup(path)
    return False, "upload_fail"

# ==================== Commands ====================

async def set_cmds(app):
    await app.bot.set_my_commands([
        BotCommand("start", "🚀 Start the bot"),
        BotCommand("help", "📖 How to use this bot"),
        BotCommand("stats", "📊 Bot & User Stats"),
        BotCommand("ping", "🏓 Check bot status"),
        BotCommand("about", "ℹ️ About this bot"),
    ])

async def start_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    track_user(u.id) # Track new user
    
    if "downloads" not in ctx.user_data:
        ctx.user_data["downloads"] = 0
        ctx.user_data["joined"] = time.strftime("%Y-%m-%d")

    txt = (
        f"Hey **{u.first_name}**! 👋\n\n"
        f"🎬 **Facebook Video Downloader**\n\n"
        f"Download videos, reels & audio from\n"
        f"Facebook — fast, free & easy!\n\n"
        f"💡 Just send me a Facebook link to get started!\n\n"
        f"🤖 {BOT_USERNAME} • v{BOT_VERSION}"
    )
    kb =[[InlineKeyboardButton("📖 Help", callback_data="cb_help"),
         InlineKeyboardButton("📊 Stats", callback_data="cb_stats")],[InlineKeyboardButton("👨‍💻 Developer", callback_data="cb_dev")]
    ]
    await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = "📖 **How to Use:**\n1. Copy FB video/reel link.\n2. Paste it here.\n3. Choose Quality.\n4. Enjoy! 🎉"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def stats_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user_dl = ctx.user_data.get("downloads", 0)
    
    global_users = len(bot_stats["users"])
    global_dl = bot_stats["total_downloads"]

    txt = (
        "📊 **Bot Statistics**\n\n"
        "👤 **Your Stats:**\n"
        f"├ 🆔 ID: `{u.id}`\n"
        f"└ 📥 Your Downloads: {user_dl}\n\n"
        "🌍 **Global Bot Stats:**\n"
        f"├ 👥 Total Users: {global_users}\n"
        f"└ 🚀 Total Downloads: {global_dl}\n\n"
        f"🤖 {BOT_USERNAME}"
    )
    await update.message.reply_text(txt, parse_mode="Markdown")

async def ping_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t1 = time.time()
    msg = await update.message.reply_text("🏓 Pinging...")
    ms = round((time.time() - t1) * 1000)
    await msg.edit_text(f"🏓 **Pong!**\n⚡ Latency: `{ms}ms`\n📌 v{BOT_VERSION} ✅", parse_mode="Markdown")

async def about_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🤖 {BOT_USERNAME}\n👨‍💻 Developer: {DEVELOPER}\n⚙️ Python 3.11 + aiohttp + PTB v20")

# ==================== Message Handler ====================

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    u = update.effective_user
    track_user(u.id)

    if not is_facebook_url(url):
        await update.message.reply_text("🚫 **Invalid Link!** Send a valid Facebook video/reel link.", parse_mode="Markdown")
        return

    msg = await update.message.reply_text("🔍 **Processing link...**\n⏳ Fetching details from Facebook...", parse_mode="Markdown")
    data = fetch_video_data(url)

    if not data or data.get("error", True):
        await msg.edit_text("❌ **Video Not Found!**\nMight be private, deleted, or invalid link.", parse_mode="Markdown")
        return

    vids =[m for m in data.get("medias", []) if m.get("type") == "video"]
    auds = [m for m in data.get("medias", []) if m.get("type") == "audio"]

    if not vids and not auds:
        await msg.edit_text("❌ No downloadable media found!", parse_mode="Markdown")
        return

    await msg.edit_text("📦 **Checking file sizes & qualities...**", parse_mode="Markdown")

    for m in vids + auds:
        s = get_size(m["url"])
        m["size"] = s
        m["size_label"] = fmt_size(s)
        m["is_large"] = s > MAX_DOWNLOAD_SIZE

    ctx.user_data["video_data"] = {
        "title": data.get("title", "Untitled"), "author": data.get("author", "Unknown"),
        "videos": vids, "audios": auds, "thumbnail": data.get("thumbnail", ""), "url": url,
    }

    kb =[]
    for i, v in enumerate(vids):
        q = v.get("quality", "?")
        sl = v.get("size_label", "")
        large_tag = " 🔗 (Direct)" if v.get("is_large") else ""
        kb.append([InlineKeyboardButton(f"{q_icon(q)} {q} ({sl}){large_tag}", callback_data=f"v_{i}")])

    for i, a in enumerate(auds):
        sl = a.get("size_label", "")
        large_tag = " 🔗 (Direct)" if a.get("is_large") else ""
        kb.append([InlineKeyboardButton(f"🎵 Audio ({sl}){large_tag}", callback_data=f"a_{i}")])

    info = (
        f"✅ **Video Found!**\n\n"
        f"📌 **Title:** {data.get('title', 'Untitled')}\n"
        f"👤 **Author:** {data.get('author', 'Unknown')}\n"
        f"⏱️ **Duration:** {fmt_dur(data.get('duration', 0))}\n\n"
        f"👇 **Select download quality:**"
    )

    await msg.delete()
    thumb = data.get("thumbnail", "")
    if thumb:
        try:
            await update.message.reply_photo(photo=thumb, caption=info, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
            return
        except: pass
    await update.message.reply_text(info, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

# ==================== Callback ====================

async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data
    
    if d.startswith("cb_"):
        if d == "cb_help": await q.edit_message_text("📖 Just send a link!", parse_mode="Markdown")
        elif d == "cb_dev": await q.edit_message_text(f"👨‍💻 Developer: {DEVELOPER}", parse_mode="Markdown")
        elif d == "cb_stats":
            await q.edit_message_text(f"👥 Users: {len(bot_stats['users'])}\n🚀 Total Downloads: {bot_stats['total_downloads']}", parse_mode="Markdown")
        return

    # === Download Execution ===
    vd = ctx.user_data.get("video_data")
    if not vd:
        await q.answer("⚠️ Session expired! Send link again.", show_alert=True)
        return

    dl_url, qual, ext, mtype, fsize, is_large = None, None, "mp4", None, 0, False

    if d.startswith("v_"):
        v = vd.get("videos", [])[int(d.split("_")[1])]
        dl_url, qual, ext, mtype, fsize, is_large = v["url"], v.get("quality", "?"), v.get("extension", "mp4"), "video", v.get("size", 0), v.get("is_large", False)
    elif d.startswith("a_"):
        a = vd.get("audios", [])[int(d.split("_")[1])]
        dl_url, qual, ext, mtype, fsize, is_large = a["url"], "Audio", a.get("extension", "mp3"), "audio", a.get("size", 0), a.get("is_large", False)

    if is_large and fsize > 0:
        add_global_download()
        ctx.user_data["downloads"] = ctx.user_data.get("downloads", 0) + 1
        direct_kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"⬇️ Download {qual} ({fmt_size(fsize)})", url=dl_url)]])
        await q.edit_message_caption(caption=f"⚠️ **File too large for Telegram!**\n\n📥 You can download it directly below:", reply_markup=direct_kb, parse_mode="Markdown")
        return

    # Status updater for smart_send
    async def status_updater(txt):
        try:
            await q.edit_message_caption(caption=f"{txt}\n\n📌 {vd['title']}", parse_mode="Markdown")
        except: pass

    ok, method = await smart_send(ctx, q.message.chat_id, dl_url, mtype, qual, vd, ext, fsize, status_updater)

    if ok:
        add_global_download()
        ctx.user_data["downloads"] = ctx.user_data.get("downloads", 0) + 1
        await q.edit_message_caption(caption=f"✅ **Sent Successfully!**\n📌 {vd['title']}\n🚀 Powered by {BOT_USERNAME}", parse_mode="Markdown")

# ==================== Main ====================

async def post_init(app):
    await set_cmds(app)
    logger.info("✅ Bot Started & Commands Set!")

def main():
    Thread(target=run_flask, daemon=True).start()
    
    app = (Application.builder().token(TELEGRAM_BOT_TOKEN)
        .read_timeout(300).write_timeout(300).connect_timeout(120)
        .post_init(post_init).build())

    app.add_handler(CommandHandler(["start", "help", "stats", "ping", "about"], lambda u, c: None)) # Handled individually
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("about", about_command))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    app.run_polling()

if __name__ == "__main__":
    main()
