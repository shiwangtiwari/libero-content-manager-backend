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

    # Trigger immediate regeneration if there's still time before the slot
    rejected_post = queries.get_posts_by_status(["draft", "pending_reschedule"])
    # The post we just rejected is now "rejected" — find its slot from the result
    try:
        from datetime import datetime
        import pytz
        IST_tz = pytz.timezone("Asia/Kolkata")
        rejected_row = queries.get_post_by_id(post_id)
        slot = rejected_row.get("scheduled_time") if rejected_row else None
        if slot:
            slot_dt = IST_tz.localize(datetime.strptime(slot, "%Y-%m-%d %H:%M"))
            hours_left = (slot_dt - datetime.now(IST_tz)).total_seconds() / 3600
            if hours_left > 1:
                from services.content_pipeline import run_content_pipeline
                asyncio.create_task(run_content_pipeline())
                await update.message.reply_text(
                    f"🗑 Post rejected.\n\n"
                    f"⚙️ Generating a replacement for {slot} IST...\n"
                    f"New draft arrives in ~30 seconds."
                )
                return
    except Exception as e:
        logger.warning("Post-reject regen trigger failed: %s", e)

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

# Stores the post_id waiting for an image, so when you send a photo
# the bot knows which post to attach it to.
_pending_image_post_id: str | None = None


def _build_image_prompt(post: dict) -> str:
    """
    Build a ready-to-copy image generation prompt from the post content.
    Tailored for ChatGPT / Gemini / Grok image generation.
    """
    content = post.get("content", "")
    signal_card = post.get("signal_card") or {}

    # Extract topic
    topic = (
        signal_card.get("selected_topic")
        or signal_card.get("trigger", "")
        or "Product Management"
    )[:80]

    # Extract hook (first non-empty line)
    hook = next((l.strip() for l in content.split("\n") if l.strip()), content[:80])

    # Extract hashtags
    tags = [l.strip() for l in content.split("\n") if l.strip().startswith("#")]
    tag_str = " ".join(tags[:3]) if tags else "#ProductManagement #DevToPM"

    prompt = f"""Create a professional LinkedIn post image with these exact specs:

TOPIC: {topic}
KEY MESSAGE: {hook[:100]}

STYLE REQUIREMENTS:
- Realistic or semi-realistic illustration style (NOT a text card, NOT a quote graphic)
- A visual scene that represents the theme metaphorically
- Examples of good visuals for this topic:
  * A person at a desk with their LinkedIn profile glowing on screen while the office is empty around them
  * A split scene: busy creative work on one side, empty social media on the other
  * Abstract visual of a city skyline with one window lit up (representing the ghost town theme)
- Warm, professional color palette — navy, amber, or teal tones
- Cinematic lighting, depth of field
- NO text overlaid on the image whatsoever
- NO stock photo feel — make it feel illustrated or editorial
- Format: 1200x627 pixels (LinkedIn landscape ratio 1.91:1)
- Mood: thought-provoking, slightly dramatic, professional

WHAT TO AVOID:
- No generic handshakes or business people in suits
- No text, words, or typography on the image
- No corporate clipart
- No obvious AI-generated uncanny valley faces

This image will accompany a LinkedIn post about: {topic}"""

    return prompt


async def handle_generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Usage:
      /generate_image           → if 1 draft, gives prompt immediately
                                  if multiple drafts, lists them to choose from
      /generate_image <id>      → gives prompt for that specific post ID (first 8 chars)

    After generating the image externally (ChatGPT/Gemini/Grok),
    send it back here as a photo — bot attaches it to the selected post.
    """
    global _pending_image_post_id

    posts = queries.get_posts_by_status(["approved", "draft", "scheduled"])
    if not posts:
        await update.message.reply_text(
            "No posts in queue.\nRun /run_now to generate a draft first."
        )
        return

    # If a specific post ID prefix was passed, find it
    arg = context.args[0].lower().strip() if context.args else None
    if arg:
        matched = next(
            (p for p in posts if p["id"].lower().startswith(arg)),
            None
        )
        if not matched:
            # Show available IDs
            lines = ["\n".join(
                f"• <code>{p['id'][:8]}</code> — {next((l.strip() for l in p['content'].split(chr(10)) if l.strip()), '')[:50]}…"
                for p in posts
            )]
            await update.message.reply_text(
                f"❌ No post found with ID starting with '<code>{arg}</code>'\n\n"
                f"Available posts:\n{''.join(lines)}\n\n"
                f"Usage: /generate_image &lt;first 8 chars of post ID&gt;",
                parse_mode="HTML",
            )
            return
        post = matched

    # No arg — if only 1 post, auto-select. If multiple, show picker.
    elif len(posts) == 1:
        post = posts[0]

    else:
        # Multiple posts — show list so user can pick
        lines = []
        for p in posts:
            hook = next((l.strip() for l in p["content"].split("\n") if l.strip()), "")[:60]
            has_image = "🖼" if p.get("image_url") else "📝"
            status = p.get("status", "draft")
            scheduled = p.get("scheduled_time", "")[:10] if p.get("scheduled_time") else "unscheduled"
            lines.append(
                f"{has_image} <code>{p['id'][:8]}</code> — {hook}…\n"
                f"   Status: {status} · Slot: {scheduled}"
            )

        await update.message.reply_text(
            f"📋 <b>You have {len(posts)} posts in queue.</b>\n\n"
            f"Reply with the post ID you want to generate an image for:\n\n"
            + "\n\n".join(lines)
            + "\n\n<b>Example:</b> /generate_image f6645445",
            parse_mode="HTML",
        )
        return

    # We have a specific post — generate and send the prompt
    _pending_image_post_id = post["id"]
    prompt = _build_image_prompt(post)
    hook = next((l.strip() for l in post["content"].split("\n") if l.strip()), "")
    has_image_already = bool(post.get("image_url"))

    await update.message.reply_text(
        f"🎨 <b>Image prompt for Post <code>{post['id'][:8]}</code></b>\n"
        + (f"⚠️ This post already has an image — sending a new one will replace it.\n" if has_image_already else "")
        + f"\n<b>Hook:</b> {hook[:80]}\n\n"
        f"<b>Steps:</b>\n"
        f"1️⃣ Copy the prompt below\n"
        f"2️⃣ Paste into ChatGPT / Gemini / Grok → generate\n"
        f"3️⃣ Download the image → send it here as a photo\n"
        f"4️⃣ Bot attaches it automatically\n\n"
        f"<b>━━━ COPY THIS PROMPT ━━━</b>\n\n"
        f"<code>{prompt}</code>\n\n"
        f"<i>Waiting for your image... (send it as a photo in this chat)</i>",
        parse_mode="HTML",
    )


# ── Photo handler — receives the image you send back after generating ─────────

async def handle_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    When you send a photo to the bot after /generate_image,
    this handler:
    1. Downloads the image from Telegram
    2. Uploads it to Supabase Storage (public bucket: post-images)
    3. Saves the public URL to the post row in Supabase
    4. Dashboard can then display the image via the public URL
    """
    global _pending_image_post_id

    if str(update.effective_chat.id) != settings.TELEGRAM_CHAT_ID:
        return

    if not _pending_image_post_id:
        await update.message.reply_text(
            "No pending post waiting for an image.\n"
            "Send /generate_image first, then send the image here."
        )
        return

    post_id = _pending_image_post_id

    try:
        import os, time
        from db.supabase_client import get_supabase

        # 1. Download from Telegram
        photo = update.message.photo[-1]  # highest resolution
        file = await context.bot.get_file(photo.file_id)
        save_path = f"/tmp/libero_img_{int(time.time())}.jpg"
        await file.download_to_drive(save_path)

        file_size = os.path.getsize(save_path)
        logger.info(f"[handle_photo] Downloaded: {save_path} ({file_size} bytes) for post {post_id[:8]}")

        # 2. Upload to Supabase Storage
        storage_path = f"posts/{post_id[:8]}_{int(time.time())}.jpg"
        public_url = None

        try:
            import httpx as _httpx
            from config import settings as _settings

            with open(save_path, "rb") as f:
                image_bytes = f.read()

            # Upload directly via Supabase Storage REST API using httpx
            # Avoids supabase-py client timeout issues
            upload_url = (
                f"{_settings.SUPABASE_URL}/storage/v1/object/post-images/{storage_path}"
            )
            async with _httpx.AsyncClient(timeout=30) as _client:
                upload_resp = await _client.post(
                    upload_url,
                    content=image_bytes,
                    headers={
                        "Authorization": f"Bearer {_settings.SUPABASE_SERVICE_KEY}",
                        "Content-Type": "image/jpeg",
                        "x-upsert": "true",
                    },
                )

            if upload_resp.status_code in (200, 201):
                public_url = (
                    f"{_settings.SUPABASE_URL}/storage/v1/object/public/post-images/{storage_path}"
                )
                logger.info(f"[handle_photo] Uploaded to Supabase Storage: {public_url}")
            else:
                raise Exception(f"Storage upload returned {upload_resp.status_code}: {upload_resp.text[:200]}")

        except Exception as storage_err:
            logger.warning(f"[handle_photo] Supabase Storage upload failed: {storage_err}")
            public_url = f"telegram://user_upload/{os.path.basename(save_path)}"

        # 3. Save URL to Supabase posts table
        queries.update_post_image(
            post_id=post_id,
            image_url=public_url,
            image_generator="user_upload",
        )

        # 4. Cleanup temp file
        try:
            os.remove(save_path)
        except Exception:
            pass

        _pending_image_post_id = None

        post = queries.get_post_by_id(post_id)
        status = post.get("status", "draft") if post else "draft"
        stored_ok = not public_url.startswith("telegram://")

        await update.message.reply_text(
            f"✅ <b>Image attached to post {post_id[:8]}</b>\n\n"
            f"Post status: <b>{status}</b>\n"
            f"Image stored: <b>{'Supabase Storage ✓' if stored_ok else 'local ref (Storage upload failed)'}</b>\n\n"
            + (
                "The post is approved and will go live at the scheduled time with this image."
                if status == "approved" else
                "Send /approve to approve the post — it will publish with this image."
            ),
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(f"[handle_photo] Failed: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Failed to save image: {str(e)[:200]}\n\nTry sending the image again."
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


# ── Application factory ───────────────────────────────────────────────────────

async def handle_run_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger the content generation pipeline."""
    if str(update.effective_chat.id) != settings.TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text(
        "⚙️ Triggering content generation pipeline...\n"
        "You will receive a draft notification in ~30 seconds."
    )
    try:
        from services.content_pipeline import run_content_pipeline
        asyncio.create_task(run_content_pipeline())
    except Exception as e:
        await update.message.reply_text(f"❌ Pipeline trigger failed: {e}")




# ── Command: /edit ────────────────────────────────────────────────────────────

async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Usage:
      /edit <new content here>         — replaces the latest draft's content
      /edit <post_id> <new content>    — replaces a specific post's content

    The new content is everything after the command (and optional post ID).
    Example: /edit Most PMs I know are solving hard problems daily but their
             LinkedIn looks like a ghost town.\n\nHere's what I learned...
    """
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/edit <new content>\n"
            "or\n"
            "/edit <post_id_first_8_chars> <new content>\n\n"
            "Replaces the draft content with your text.",
            parse_mode="HTML",
        )
        return

    # Check if first arg looks like a post ID (8 hex chars)
    import re
    first_arg = context.args[0]
    if re.match(r'^[0-9a-f-]{8,36}$', first_arg, re.I) and len(context.args) > 1:
        # First arg is a post ID
        post_id_fragment = first_arg
        new_content = " ".join(context.args[1:]).strip()

        # Find the post by ID fragment
        drafts = queries.get_posts_by_status(["draft", "approved", "pending_reschedule"])
        post = next((p for p in drafts if p["id"].startswith(post_id_fragment) or p["id"][:8].upper() == post_id_fragment.upper()), None)
        if not post:
            await update.message.reply_text(f"❌ No draft found with ID starting with {post_id_fragment}")
            return
    else:
        # No post ID — use most recent draft
        drafts = queries.get_posts_by_status(["draft", "approved", "pending_reschedule"])
        if not drafts:
            await update.message.reply_text("No draft posts to edit.")
            return
        post = drafts[0]
        new_content = " ".join(context.args).strip()

    if not new_content:
        await update.message.reply_text("❌ New content cannot be empty.")
        return

    # Strip any markdown bold that slipped in
    new_content = new_content.replace("**", "").replace("__", "")

    # Save to Supabase
    queries.update_post_content(post["id"], new_content)

    await update.message.reply_text(
        f"✅ Draft updated!\n\n"
        f"<i>{new_content[:200]}{'...' if len(new_content) > 200 else ''}</i>\n\n"
        f"Send /approve to approve it.",
        parse_mode="HTML",
    )


# ── Command: /strip ───────────────────────────────────────────────────────────

async def handle_strip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /strip — removes **bold** markdown from the latest draft.
    Quick fix before posting if asterisks are showing in the content.
    """
    drafts = queries.get_posts_by_status(["draft", "approved", "pending_reschedule"])
    if not drafts:
        await update.message.reply_text("No draft posts found.")
        return

    post = drafts[0]
    original = post.get("content", "")
    cleaned = original.replace("**", "").replace("__", "")

    if cleaned == original:
        await update.message.reply_text("✓ No markdown formatting found — content is already clean.")
        return

    queries.update_post_content(post["id"], cleaned)
    await update.message.reply_text(
        f"✅ Markdown stripped!\n\n"
        f"<i>{cleaned[:200]}{'...' if len(cleaned) > 200 else ''}</i>\n\n"
        f"Send /approve to approve it.",
        parse_mode="HTML",
    )

def build_telegram_app() -> Application:
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("approve", handle_approve))
    app.add_handler(CommandHandler("edit", handle_edit))
    app.add_handler(CommandHandler("strip", handle_strip))
    app.add_handler(CommandHandler("reject", handle_reject))
    app.add_handler(CommandHandler("reschedule", handle_reschedule))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("queue", handle_queue))
    app.add_handler(CommandHandler("generate_image", handle_generate_image))
    app.add_handler(CommandHandler("run_now", handle_run_now))
    # Photo handler — receives image after /generate_image prompt flow
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_input))
    # Text handler must be LAST
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_mind_input))
    return app
