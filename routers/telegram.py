"""
Telegram bot command handlers.
Bot runs in polling mode (not webhook) as a background task inside FastAPI.
All commands listed in Section 9.3 of the master doc are implemented here.
"""
import asyncio
import logging
from datetime import datetime
import pytz
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from config import settings
from db import queries
from services.post_manager import approve_post, reject_post, reschedule_post

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Global bot instance for sending outbound messages from other services
_bot: Bot | None = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    return _bot


async def send_telegram_message(text: str, parse_mode: str = "HTML") -> None:
    """Send a message to Shiwang's Telegram. Called from any service."""
    bot = get_bot()
    await bot.send_message(
        chat_id=settings.TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=parse_mode,
    )


async def send_telegram_file(file_path: str, caption: str = "") -> None:
    """Send a file (image) to Shiwang's Telegram."""
    bot = get_bot()
    with open(file_path, "rb") as f:
        await bot.send_document(
            chat_id=settings.TELEGRAM_CHAT_ID,
            document=f,
            caption=caption,
        )


# ── Command: /status ─────────────────────────────────────────────────────────

async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    health = queries.get_all_session_health()
    queue = queries.get_posts_by_status(["draft", "approved", "scheduled", "pending_reschedule"])

    health_lines = []
    for h in health:
        icon = "🟢" if h["is_healthy"] else "🔴"
        health_lines.append(f"{icon} {h['platform'].capitalize()}")

    next_post = None
    for post in sorted(queue, key=lambda p: p.get("scheduled_time") or ""):
        if post.get("scheduled_time"):
            next_post = post
            break

    next_info = (
        f"📅 Next: {next_post['scheduled_time']} IST"
        if next_post else "📅 No post scheduled"
    )

    msg = (
        f"<b>Libero Status</b> — {now_ist}\n\n"
        f"{'  '.join(health_lines)}\n\n"
        f"{next_info}\n"
        f"📬 Queue: {len(queue)} post(s)"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ── Command: /queue ───────────────────────────────────────────────────────────

async def handle_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    posts = queries.get_posts_by_status(["draft", "approved", "scheduled", "pending_reschedule"])
    if not posts:
        await update.message.reply_text("Queue is empty. No posts pending.")
        return

    lines = ["<b>Upcoming Posts</b>\n"]
    for i, p in enumerate(posts[:5], 1):
        status_icon = {"draft": "📝", "approved": "✅", "scheduled": "🕐", "pending_reschedule": "⏳"}.get(p["status"], "❓")
        snippet = (p["content"] or "")[:60].replace("\n", " ")
        lines.append(f"{i}. {status_icon} {p.get('scheduled_time', 'unscheduled')}\n   {snippet}…\n   ID: <code>{p['id'][:8]}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── Command: /approve ─────────────────────────────────────────────────────────

async def handle_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /approve  (approves the latest draft)  or  /approve <post_id>"""
    post_id = context.args[0] if context.args else None

    if not post_id:
        # Approve the most recent draft
        drafts = queries.get_posts_by_status(["draft", "pending_reschedule"])
        if not drafts:
            await update.message.reply_text("No draft posts to approve.")
            return
        post_id = drafts[0]["id"]

    result = approve_post(post_id)
    if result.get("error"):
        await update.message.reply_text(f"❌ {result['error']}")
        return

    post = result["post"]
    await update.message.reply_text(
        f"✅ Post approved!\n"
        f"Scheduled: {post.get('scheduled_time', 'TBD')} IST\n"
        f"ID: <code>{post['id'][:8]}</code>",
        parse_mode="HTML",
    )


# ── Command: /reject ──────────────────────────────────────────────────────────

async def handle_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    post_id = context.args[0] if context.args else None
    if not post_id:
        drafts = queries.get_posts_by_status(["draft", "pending_reschedule"])
        if not drafts:
            await update.message.reply_text("No draft posts to reject.")
            return
        post_id = drafts[0]["id"]

    result = reject_post(post_id)
    if result.get("error"):
        await update.message.reply_text(f"❌ {result['error']}")
        return

    await update.message.reply_text("🗑 Post rejected. A new draft will be generated in the next content cycle.")


# ── Command: /reschedule ──────────────────────────────────────────────────────

async def handle_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /reschedule  or  /reschedule <post_id>"""
    post_id = context.args[0] if context.args else None
    if not post_id:
        scheduled = queries.get_posts_by_status(["approved", "scheduled", "pending_reschedule"])
        if not scheduled:
            await update.message.reply_text("No posts to reschedule.")
            return
        post_id = scheduled[0]["id"]

    result = reschedule_post(post_id)
    if result.get("error"):
        await update.message.reply_text(f"❌ {result['error']}")
        return
    if result.get("expired"):
        await update.message.reply_text(
            "⚠️ Post expired after 3 reschedules.\n"
            "Send /approve to post now or /reject to discard."
        )
        return

    await update.message.reply_text(
        f"📅 Rescheduled to {result['new_time']} IST"
    )


# ── Command: /generate_image ─────────────────────────────────────────────────

async def handle_generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Usage: /generate_image chatgpt  or  /generate_image gemini
    Fires the Playwright image generation job on Railway.
    Image returned to Telegram when done.
    Phase 4 feature — returns placeholder message during Phase 1.
    """
    platform = context.args[0].lower() if context.args else "chatgpt"
    if platform not in ("chatgpt", "gemini"):
        await update.message.reply_text("Usage: /generate_image chatgpt  or  /generate_image gemini")
        return

    # Get the latest approved/draft post
    posts = queries.get_posts_by_status(["draft", "approved", "scheduled"])
    if not posts:
        await update.message.reply_text("No posts in queue to generate an image for.")
        return

    post = posts[0]
    await update.message.reply_text(
        f"🎨 Generating image via {platform}...\n"
        f"Post: {post['content'][:60]}…\n\n"
        f"⚠️ Image generation (Phase 4) not yet deployed. "
        f"Use the dashboard to upload an image manually."
    )


# ── Plain text handler — "what's on my mind" input ───────────────────────────

async def handle_mind_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any non-command text is treated as a content signal from Shiwang."""
    if str(update.effective_chat.id) != settings.TELEGRAM_CHAT_ID:
        return  # Ignore messages from other chats

    text = update.message.text.strip()
    queries.create_telegram_input(message=text, source="telegram")
    await update.message.reply_text(
        f"💡 Got it! Saved as content signal.\n"
        f"This will be used as Priority 1 signal in the next content generation cycle.\n\n"
        f"<i>\"{text[:100]}\"</i>",
        parse_mode="HTML",
    )



# ── Command: /run_now ─────────────────────────────────────────────────────────

async def handle_run_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger the content generation pipeline."""
    if str(update.effective_chat.id) != settings.TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text(
        "⚙️ Triggering content generation pipeline...\n"
        "You will receive a draft notification in ~30 seconds.\n\n"
        "Or call POST /run_now on the Railway URL."
    )
    try:
        from services.content_pipeline import run_content_pipeline
        import asyncio
        asyncio.create_task(run_content_pipeline())
    except Exception as e:
        await update.message.reply_text(f"❌ Pipeline trigger failed: {e}")


# ── Application factory ───────────────────────────────────────────────────────

def build_telegram_app() -> Application:
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("approve", handle_approve))
    app.add_handler(CommandHandler("reject", handle_reject))
    app.add_handler(CommandHandler("reschedule", handle_reschedule))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("queue", handle_queue))
    app.add_handler(CommandHandler("generate_image", handle_generate_image))
    app.add_handler(CommandHandler("run_now", handle_run_now))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_mind_input))
    return app
