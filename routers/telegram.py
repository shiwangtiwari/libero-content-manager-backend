"""
Telegram bot command handlers.
Bot runs in polling mode (not webhook) as a background task inside FastAPI.

Aesthetic: system terminal style. Bracketed markers instead of emojis.
[CONFIRMED] [REJECTED] [RESCHEDULED] [EXPIRED] [UPDATED] [SIGNAL SAVED]
[IMAGE PROMPT] [IMAGE SAVED] [RUNNING] [STRIPPED] [POSTED] [POST FAILED]

LIBERO is the system name — not "Claude". Session health shows LIBERO, not CLAUDE.

_pending_image_post_id is stored in Supabase user_profile table (survives restarts).
"""
import asyncio
import logging
import re
from datetime import datetime
import pytz
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from config import settings
from db import queries
from services.post_manager import approve_post, reject_post, reschedule_post

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Display names for session health — maps DB platform name → human label
_PLATFORM_DISPLAY = {
    "claude":  "LIBERO",      # The content generation engine is Libero, not Claude
    "chatgpt": "CHATGPT",
    "gemini":  "GEMINI",
}

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
    """Send a file to Shiwang's Telegram."""
    bot = get_bot()
    with open(file_path, "rb") as f:
        await bot.send_document(
            chat_id=settings.TELEGRAM_CHAT_ID,
            document=f,
            caption=caption,
        )


# ── Command: /status ──────────────────────────────────────────────────────────

async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    health = queries.get_all_session_health()
    queue = queries.get_posts_by_status(["draft", "approved", "scheduled", "pending_reschedule"])

    # Only show health rows that are actually relevant (skip chatgpt/gemini)
    relevant_platforms = {"claude", "libero"}
    health_lines = ""
    for h in health:
        if h["platform"] not in ("claude", "libero"):
            continue
        name = _PLATFORM_DISPLAY.get(h["platform"], h["platform"].upper())
        status = "ONLINE" if h["is_healthy"] else "FAULT"
        health_lines += f"\n  {name:<12} {status}"

    # If no relevant health rows found, just show Libero based on env var
    if not health_lines:
        import os
        key_ok = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
        health_lines = f"\n  LIBERO       {'ONLINE' if key_ok else 'FAULT'}"

    # Queue breakdown
    approved_posts = [p for p in queue if p["status"] == "approved"]
    draft_posts    = [p for p in queue if p["status"] in ("draft", "pending_reschedule")]

    next_post = None
    for post in sorted(queue, key=lambda p: p.get("scheduled_time") or ""):
        if post.get("scheduled_time"):
            next_post = post
            break

    next_info = (
        f"  NEXT        {next_post['scheduled_time']} IST [{next_post['status'].upper()}]"
        if next_post else "  NEXT        none scheduled"
    )

    queue_breakdown = (
        f"  APPROVED    {len(approved_posts)} post(s) — will go live automatically\n"
        f"  DRAFTS      {len(draft_posts)} post(s) — awaiting your approval"
    )

    msg = (
        f"<b>LIBERO / STATUS</b>\n"
        f"<code>{now_ist}</code>\n\n"
        f"<b>SESSION HEALTH</b>"
        f"<code>{health_lines}</code>\n\n"
        f"<b>QUEUE</b>\n"
        f"<code>{next_info}\n"
        f"{queue_breakdown}</code>"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ── Command: /queue ───────────────────────────────────────────────────────────

async def handle_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    posts = queries.get_posts_by_status(["draft", "approved", "scheduled", "pending_reschedule"])
    if not posts:
        await update.message.reply_text(
            "<b>QUEUE EMPTY</b>\n\n"
            "<code>Auto-generation schedule (IST):\n"
            "  MON 06:00  draft for TUE post\n"
            "  TUE 06:00  draft for WED post\n"
            "  WED 06:00  draft for THU post</code>",
            parse_mode="HTML",
        )
        return

    status_label = {
        "draft": "DRAFT", "approved": "APPROVED",
        "scheduled": "SCHEDULED", "pending_reschedule": "RESCHEDULED",
    }
    lines = ["<b>QUEUE</b>\n"]
    # Sort newest first (matches /queue numbering for /reject 1, /reject 2 etc.)
    posts_sorted = sorted(posts[:5], key=lambda p: p.get("created_at", ""), reverse=True)
    for i, p in enumerate(posts_sorted, 1):
        sl = status_label.get(p["status"], p["status"].upper())
        snippet = (p["content"] or "")[:55].replace("\n", " ")
        lines.append(
            f"<code>{i}. [{sl}]  {p.get('scheduled_time', 'unscheduled')}\n"
            f"   {snippet}...\n"
            f"   Use: /reject {i} or /approve {i}</code>"
        )
    lines.append("\n<i>Use the number (e.g. /reject 2) or short ID to target a specific post.</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── Command: /approve ─────────────────────────────────────────────────────────


def _resolve_post_id(arg: str, statuses: list[str]) -> tuple[str | None, str | None]:
    """
    Resolve a user-supplied argument to a full post ID.
    Accepts:
      - A queue number: "1", "2", "3"  (from /queue output)
      - A short ID:    "0bab32a5"      (first 8 chars shown in /queue)
      - A full UUID:   "0bab32a5-..."  (full ID)
    Returns (full_post_id, error_message).
    """
    posts = queries.get_posts_by_status(statuses)
    if not posts:
        return None, "No posts found in queue."

    # Sort same way /queue does (newest created first, so index matches /queue output)
    posts_sorted = sorted(posts, key=lambda p: p.get("created_at", ""), reverse=True)

    # Queue number (1, 2, 3...)
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(posts_sorted):
            return posts_sorted[idx]["id"], None
        return None, f"No post at position {arg}. Queue has {len(posts_sorted)} post(s)."

    # Short ID or full UUID — match against start of ID
    arg_lower = arg.lower()
    for post in posts_sorted:
        if post["id"].lower().startswith(arg_lower):
            return post["id"], None

    return None, f"No post found matching '{arg}'. Use /queue to see IDs."

async def handle_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /approve  or  /approve <number_or_id>
    /approve       — approves the most recent draft
    /approve 1     — approves queue item #1
    /approve 2     — approves queue item #2
    /approve 0bab32 — approves by short ID"""
    statuses = ["draft", "pending_reschedule"]
    if not context.args:
        drafts = queries.get_posts_by_status(statuses)
        if not drafts:
            await update.message.reply_text("[ERROR] No draft posts to approve.")
            return
        post_id = drafts[0]["id"]
    else:
        post_id, err = _resolve_post_id(context.args[0], statuses)
        if err:
            await update.message.reply_text(f"[ERROR] {err}")
            return

    result = approve_post(post_id)
    if result.get("error"):
        await update.message.reply_text(f"[ERROR] {result['error']}")
        return

    post = result["post"]
    await update.message.reply_text(
        f"<b>[CONFIRMED]</b>\n\n"
        f"<code>STATUS     APPROVED\n"
        f"SLOT       {post.get('scheduled_time', 'TBD')} IST\n"
        f"ID         {post['id'][:8].upper()}</code>\n\n"
        f"Post will go live at the scheduled time.",
        parse_mode="HTML",
    )


# ── Command: /reject ──────────────────────────────────────────────────────────

async def handle_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /reject  or  /reject <number_or_id>
    /reject       — rejects the most recent draft
    /reject 1     — rejects queue item #1
    /reject 2     — rejects queue item #2
    /reject 0bab32 — rejects by short ID"""
    statuses = ["draft", "pending_reschedule"]
    if not context.args:
        drafts = queries.get_posts_by_status(statuses)
        if not drafts:
            await update.message.reply_text("[ERROR] No draft posts to reject.")
            return
        post_id = drafts[0]["id"]
    else:
        post_id, err = _resolve_post_id(context.args[0], statuses)
        if err:
            await update.message.reply_text(f"[ERROR] {err}")
            return

    # Fetch slot BEFORE rejecting so we can check time remaining
    pre_reject = queries.get_post_by_id(post_id)
    slot = pre_reject.get("scheduled_time") if pre_reject else None

    result = reject_post(post_id)
    if result.get("error"):
        await update.message.reply_text(f"[ERROR] {result['error']}")
        return

    # Trigger immediate regeneration if slot is still >1 hour away
    if slot:
        try:
            slot_dt = IST.localize(datetime.strptime(slot, "%Y-%m-%d %H:%M"))
            hours_left = (slot_dt - datetime.now(IST)).total_seconds() / 3600
            if hours_left > 1:
                from services.content_pipeline import run_content_pipeline
                asyncio.create_task(run_content_pipeline())
                await update.message.reply_text(
                    f"<b>[REJECTED]</b>\n\n"
                    f"<code>SLOT       {slot} IST\n"
                    f"TIME LEFT  {hours_left:.1f}h\n"
                    f"REGEN      TRIGGERED</code>\n\n"
                    f"New draft arriving in ~30 seconds.",
                    parse_mode="HTML",
                )
                return
        except Exception as e:
            logger.warning("Post-reject regen trigger failed: %s", e)

    await update.message.reply_text(
        "<b>[REJECTED]</b>\n\n"
        "Post discarded. New draft will be generated in the next content cycle.",
        parse_mode="HTML",
    )


# ── Command: /reschedule ──────────────────────────────────────────────────────

async def handle_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /reschedule  or  /reschedule <number_or_id>"""
    statuses = ["approved", "scheduled", "pending_reschedule", "draft"]
    if not context.args:
        posts = queries.get_posts_by_status(statuses)
        if not posts:
            await update.message.reply_text("[ERROR] No posts to reschedule.")
            return
        post_id = posts[0]["id"]
    else:
        post_id, err = _resolve_post_id(context.args[0], statuses)
        if err:
            await update.message.reply_text(f"[ERROR] {err}")
            return

    result = reschedule_post(post_id)
    if result.get("error"):
        await update.message.reply_text(f"[ERROR] {result['error']}")
        return
    if result.get("expired"):
        await update.message.reply_text(
            "<b>[EXPIRED]</b>\n\n"
            "<code>3-reschedule cap reached.</code>\n\n"
            "/approve — post it now\n"
            "/reject  — discard it",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        f"<b>[RESCHEDULED]</b>\n\n"
        f"<code>NEW SLOT   {result['new_time']} IST</code>",
        parse_mode="HTML",
    )


# ── Command: /edit ────────────────────────────────────────────────────────────

async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /edit <new content>
    /edit <post_id_8chars> <new content>
    Strips **bold** markdown automatically. LinkedIn limit: 3000 chars.
    """
    if not context.args:
        await update.message.reply_text(
            "<b>USAGE</b>\n\n"
            "<code>/edit &lt;new content&gt;\n"
            "/edit &lt;post_id&gt; &lt;new content&gt;</code>\n\n"
            "Strips **bold** markdown automatically.",
            parse_mode="HTML",
        )
        return

    first_arg = context.args[0]
    if re.match(r'^[0-9a-f-]{8,36}$', first_arg, re.I) and len(context.args) > 1:
        drafts = queries.get_posts_by_status(["draft", "approved", "pending_reschedule"])
        post = next(
            (p for p in drafts
             if p["id"].lower().startswith(first_arg.lower())
             or p["id"][:8].upper() == first_arg.upper()),
            None
        )
        if not post:
            await update.message.reply_text(f"[ERROR] No draft found with ID starting with {first_arg}")
            return
        new_content = " ".join(context.args[1:]).strip()
    else:
        drafts = queries.get_posts_by_status(["draft", "approved", "pending_reschedule"])
        if not drafts:
            await update.message.reply_text("[ERROR] No draft posts to edit.")
            return
        post = drafts[0]
        new_content = " ".join(context.args).strip()

    if not new_content:
        await update.message.reply_text("[ERROR] New content cannot be empty.")
        return

    new_content = re.sub(r"\*\*|__", "", new_content).strip()
    if len(new_content) > 3000:
        await update.message.reply_text(
            f"[ERROR] {len(new_content)} chars — LinkedIn limit is 3000.\n"
            f"Shorten by {len(new_content) - 3000} chars."
        )
        return

    queries.update_post_content(post["id"], new_content)
    await update.message.reply_text(
        f"<b>[UPDATED]</b>\n\n"
        f"<code>ID         {post['id'][:8].upper()}\n"
        f"LENGTH     {len(new_content)} chars\n"
        f"STATUS     {post['status'].upper()}</code>\n\n"
        f"<i>{new_content[:200]}{'...' if len(new_content) > 200 else ''}</i>\n\n"
        f"Send /approve to confirm.",
        parse_mode="HTML",
    )


# ── Command: /strip ───────────────────────────────────────────────────────────

async def handle_strip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Removes **bold** markdown from the latest draft."""
    drafts = queries.get_posts_by_status(["draft", "approved", "pending_reschedule"])
    if not drafts:
        await update.message.reply_text("[ERROR] No draft posts found.")
        return

    post = drafts[0]
    original = post.get("content", "")
    cleaned = re.sub(r"\*\*|__", "", original).strip()

    if cleaned == original:
        await update.message.reply_text(
            "<b>[STRIP]</b>\n\n"
            "<code>Content is already clean — no markdown found.</code>",
            parse_mode="HTML",
        )
        return

    queries.update_post_content(post["id"], cleaned)
    await update.message.reply_text(
        f"<b>[STRIPPED]</b>\n\n"
        f"<code>ID         {post['id'][:8].upper()}\n"
        f"LENGTH     {len(cleaned)} chars</code>\n\n"
        f"<i>{cleaned[:200]}{'...' if len(cleaned) > 200 else ''}</i>\n\n"
        f"Send /approve to confirm.",
        parse_mode="HTML",
    )


# ── Command: /generate_image ──────────────────────────────────────────────────

def _build_image_prompt(post: dict) -> str:
    content = post.get("content", "")
    signal_card = post.get("signal_card") or {}
    topic = (
        signal_card.get("selected_topic")
        or signal_card.get("trigger", "")
        or "Product Management"
    )[:80]
    hook = next((l.strip() for l in content.split("\n") if l.strip()), content[:80])

    return (
        f"Create a professional LinkedIn post image.\n\n"
        f"TOPIC: {topic}\n"
        f"KEY MESSAGE: {hook[:100]}\n\n"
        f"STYLE:\n"
        f"- Realistic or semi-realistic illustration (not a text card or quote graphic)\n"
        f"- A visual scene representing the theme metaphorically\n"
        f"- Warm professional palette: navy, amber, or teal\n"
        f"- Cinematic lighting with depth of field\n"
        f"- NO text on the image whatsoever\n"
        f"- Format: 1200x627px (LinkedIn 1.91:1 ratio)\n"
        f"- Mood: thought-provoking, professional\n\n"
        f"AVOID: generic handshakes, suits, clipart, any typography on the image"
    )


async def handle_generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    posts = queries.get_posts_by_status(["approved", "draft", "scheduled"])
    if not posts:
        await update.message.reply_text("[ERROR] No posts in queue. Run /run_now first.")
        return

    arg = context.args[0].lower().strip() if context.args else None
    if arg:
        post = next((p for p in posts if p["id"].lower().startswith(arg)), None)
        if not post:
            id_list = "\n".join(f"  {p['id'][:8]}" for p in posts)
            await update.message.reply_text(
                f"[ERROR] No post found with ID starting with {arg}\n\n"
                f"Available:\n<code>{id_list}</code>",
                parse_mode="HTML",
            )
            return
    elif len(posts) == 1:
        post = posts[0]
    else:
        lines = []
        for p in posts:
            hook = next((l.strip() for l in p["content"].split("\n") if l.strip()), "")[:50]
            has_img = "[IMG]" if p.get("image_url") else "[NO IMG]"
            lines.append(
                f"<code>{p['id'][:8]}  {has_img}  {p.get('scheduled_time', '')[:10]}\n"
                f"  {hook}...</code>"
            )
        await update.message.reply_text(
            f"<b>[IMAGE PROMPT]</b>\n\n"
            f"{len(posts)} posts in queue. Specify which:\n\n"
            + "\n\n".join(lines)
            + "\n\n<b>Usage:</b> /generate_image &lt;ID first 8 chars&gt;",
            parse_mode="HTML",
        )
        return

    # Persist the pending post ID to Supabase (survives Railway restarts)
    queries.set_pending_image_post(post["id"])

    prompt = _build_image_prompt(post)
    hook = next((l.strip() for l in post["content"].split("\n") if l.strip()), "")
    has_image_already = bool(post.get("image_url"))

    await update.message.reply_text(
        f"<b>[IMAGE PROMPT]</b>\n"
        + (f"<code>[WARN] Post already has an image — new one replaces it.</code>\n" if has_image_already else "")
        + f"\n<code>POST ID    {post['id'][:8].upper()}\n"
        f"HOOK       {hook[:65]}...</code>\n\n"
        f"<b>STEPS</b>\n"
        f"1. Copy the prompt below\n"
        f"2. Paste into ChatGPT / Gemini / Grok\n"
        f"3. Download the generated image\n"
        f"4. Send it here as a photo\n\n"
        f"<b>PROMPT</b>\n\n"
        f"<code>{prompt}</code>\n\n"
        f"<i>Waiting for your image...</i>",
        parse_mode="HTML",
    )


# ── Photo handler ─────────────────────────────────────────────────────────────

async def handle_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Receives the image sent after /generate_image.
    Pending post ID is read from Supabase — survives Railway restarts.
    """
    if str(update.effective_chat.id) != settings.TELEGRAM_CHAT_ID:
        return

    # Read pending post ID from DB (not memory — restart-safe)
    post_id = queries.get_pending_image_post()

    if not post_id:
        await update.message.reply_text(
            "[ERROR] No post waiting for an image.\n"
            "Send /generate_image first, then send the image here."
        )
        return

    try:
        import os, time

        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        save_path = f"/tmp/libero_img_{int(time.time())}.jpg"
        await file.download_to_drive(save_path)

        logger.info("[handle_photo] Downloaded %s for post %s", save_path, post_id[:8])

        storage_path = f"posts/{post_id[:8]}_{int(time.time())}.jpg"
        public_url = None

        try:
            import httpx as _httpx
            from config import settings as _settings

            with open(save_path, "rb") as f:
                image_bytes = f.read()

            upload_url = f"{_settings.SUPABASE_URL}/storage/v1/object/post-images/{storage_path}"
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
                public_url = f"{_settings.SUPABASE_URL}/storage/v1/object/public/post-images/{storage_path}"
                logger.info("[handle_photo] Uploaded: %s", public_url)
            else:
                raise Exception(f"Storage {upload_resp.status_code}: {upload_resp.text[:200]}")

        except Exception as storage_err:
            logger.warning("[handle_photo] Storage upload failed: %s", storage_err)
            public_url = f"telegram://user_upload/{os.path.basename(save_path)}"

        # Save image URL — image_generator mapped to 'none' (valid enum value)
        queries.update_post_image(post_id=post_id, image_url=public_url, image_generator="none")

        # Clear the pending post ID from DB
        queries.clear_pending_image_post()

        try:
            os.remove(save_path)
        except Exception:
            pass

        post = queries.get_post_by_id(post_id)
        status = post.get("status", "draft") if post else "draft"
        stored_ok = not public_url.startswith("telegram://")

        await update.message.reply_text(
            f"<b>[IMAGE SAVED]</b>\n\n"
            f"<code>POST ID    {post_id[:8].upper()}\n"
            f"STATUS     {status.upper()}\n"
            f"STORAGE    {'SUPABASE OK' if stored_ok else 'LOCAL REF (upload failed)'}</code>\n\n"
            + (
                "Post is approved — goes live at scheduled time with this image."
                if status == "approved" else
                "Send /approve to confirm. Post will publish with this image."
            ),
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error("[handle_photo] Failed: %s", e, exc_info=True)
        await update.message.reply_text(f"[ERROR] Failed to save image: {str(e)[:200]}\n\nTry again.")


# ── Plain text handler — what's on my mind ────────────────────────────────────

async def handle_mind_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any non-command text is treated as a content signal (P1 priority)."""
    if str(update.effective_chat.id) != settings.TELEGRAM_CHAT_ID:
        return
    text = update.message.text.strip()
    queries.create_telegram_input(message=text, source="telegram")
    await update.message.reply_text(
        f"<b>[SIGNAL SAVED]</b>\n\n"
        f"<code>PRIORITY   P1 (highest)\n"
        f"SOURCE     telegram\n"
        f"FIRES AT   next content generation</code>\n\n"
        f"<i>\"{text[:100]}\"</i>",
        parse_mode="HTML",
    )


# ── Command: /run_now ─────────────────────────────────────────────────────────

async def handle_run_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if str(update.effective_chat.id) != settings.TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text(
        "<b>[RUNNING]</b>\n\n"
        "<code>Content pipeline triggered.\n"
        "Draft arriving in ~30 seconds.</code>",
        parse_mode="HTML",
    )
    try:
        from services.content_pipeline import run_content_pipeline
        asyncio.create_task(run_content_pipeline())
    except Exception as e:
        await update.message.reply_text(f"[ERROR] Pipeline trigger failed: {e}")


# ── Application factory ───────────────────────────────────────────────────────

def build_telegram_app() -> Application:
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("approve", handle_approve))
    app.add_handler(CommandHandler("reject", handle_reject))
    app.add_handler(CommandHandler("reschedule", handle_reschedule))
    app.add_handler(CommandHandler("edit", handle_edit))
    app.add_handler(CommandHandler("strip", handle_strip))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("queue", handle_queue))
    app.add_handler(CommandHandler("generate_image", handle_generate_image))
    app.add_handler(CommandHandler("run_now", handle_run_now))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_mind_input))
    return app
