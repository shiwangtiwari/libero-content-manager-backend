"""
Telegram bot command handlers.
Bot runs in polling mode (not webhook) as a background task inside FastAPI.

Aesthetic: system terminal style. No emojis. Bracketed markers instead.
[CONFIRMED] [REJECTED] [ERROR] [WARN] [SIGNAL] [RUNNING] [SCHEDULED]
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

_bot: Bot | None = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    return _bot


async def send_telegram_message(text: str, parse_mode: str = "HTML") -> None:
    bot = get_bot()
    await bot.send_message(
        chat_id=settings.TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=parse_mode,
    )


async def send_telegram_file(file_path: str, caption: str = "") -> None:
    bot = get_bot()
    with open(file_path, "rb") as f:
        await bot.send_document(
            chat_id=settings.TELEGRAM_CHAT_ID,
            document=f,
            caption=caption,
        )


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    health = queries.get_all_session_health()
    queue = queries.get_posts_by_status(["draft", "approved", "scheduled", "pending_reschedule"])

    health_lines = ""
    for h in health:
        status = "ONLINE" if h["is_healthy"] else "FAULT"
        health_lines += f"\n  {h['platform'].upper():<12} {status}"

    next_post = None
    for post in sorted(queue, key=lambda p: p.get("scheduled_time") or ""):
        if post.get("scheduled_time"):
            next_post = post
            break

    next_info = (
        f"  NEXT        {next_post['scheduled_time']} IST [{next_post['status'].upper()}]"
        if next_post else "  NEXT        none scheduled"
    )

    msg = (
        f"<b>LIBERO / STATUS</b>\n"
        f"<code>{now_ist}</code>\n\n"
        f"<b>SESSION HEALTH</b>"
        f"<code>{health_lines}</code>\n\n"
        f"<b>SCHEDULE</b>\n"
        f"<code>{next_info}\n"
        f"  QUEUE       {len(queue)} post(s)</code>"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def handle_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    posts = queries.get_posts_by_status(["draft", "approved", "scheduled", "pending_reschedule"])
    if not posts:
        await update.message.reply_text(
            "<b>QUEUE EMPTY</b>\n\n"
            "<code>Auto-generation schedule:\n"
            "  MON 06:00 IST  draft for TUE post\n"
            "  TUE 06:00 IST  draft for WED post\n"
            "  WED 06:00 IST  draft for THU post</code>",
            parse_mode="HTML",
        )
        return

    status_label = {
        "draft": "DRAFT", "approved": "APPROVED",
        "scheduled": "SCHEDULED", "pending_reschedule": "RESCHEDULED",
    }
    lines = ["<b>QUEUE</b>\n"]
    for i, p in enumerate(posts[:5], 1):
        sl = status_label.get(p["status"], p["status"].upper())
        snippet = (p["content"] or "")[:55].replace("\n", " ")
        lines.append(
            f"<code>{i}. [{sl}]  {p.get('scheduled_time', 'unscheduled')}\n"
            f"   {snippet}...\n"
            f"   ID: {p['id'][:8]}</code>"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def handle_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    post_id = context.args[0] if context.args else None
    if not post_id:
        drafts = queries.get_posts_by_status(["draft", "pending_reschedule"])
        if not drafts:
            await update.message.reply_text("[ERROR] No draft posts to approve.")
            return
        post_id = drafts[0]["id"]

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


async def handle_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    post_id = context.args[0] if context.args else None
    if not post_id:
        drafts = queries.get_posts_by_status(["draft", "pending_reschedule"])
        if not drafts:
            await update.message.reply_text("[ERROR] No draft posts to reject.")
            return
        post_id = drafts[0]["id"]

    result = reject_post(post_id)
    if result.get("error"):
        await update.message.reply_text(f"[ERROR] {result['error']}")
        return

    try:
        rejected_row = queries.get_post_by_id(post_id)
        slot = rejected_row.get("scheduled_time") if rejected_row else None
        if slot:
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
        "Post discarded. New draft in the next content cycle.",
        parse_mode="HTML",
    )


async def handle_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    post_id = context.args[0] if context.args else None
    if not post_id:
        scheduled = queries.get_posts_by_status(["approved", "scheduled", "pending_reschedule"])
        if not scheduled:
            await update.message.reply_text("[ERROR] No posts to reschedule.")
            return
        post_id = scheduled[0]["id"]

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


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            (p for p in drafts if p["id"].lower().startswith(first_arg.lower())
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


async def handle_strip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


_pending_image_post_id: str | None = None


def _build_image_prompt(post: dict) -> str:
    content = post.get("content", "")
    signal_card = post.get("signal_card") or {}
    topic = (signal_card.get("selected_topic") or signal_card.get("trigger", "") or "Product Management")[:80]
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
        f"- NO text on the image\n"
        f"- Format: 1200x627px (LinkedIn 1.91:1 ratio)\n"
        f"- Mood: thought-provoking, professional\n\n"
        f"AVOID: generic handshakes, suits, clipart, any typography on image"
    )


async def handle_generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _pending_image_post_id

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

    _pending_image_post_id = post["id"]
    prompt = _build_image_prompt(post)
    hook = next((l.strip() for l in post["content"].split("\n") if l.strip()), "")
    has_image_already = bool(post.get("image_url"))

    await update.message.reply_text(
        f"<b>[IMAGE PROMPT]</b>\n"
        + (f"<code>[WARN] Post already has an image — new one replaces it.</code>\n" if has_image_already else "")
        + f"\n<code>POST ID    {post['id'][:8].upper()}\n"
        f"HOOK       {hook[:65]}...</code>\n\n"
        f"<b>STEPS</b>\n"
        f"1. Copy prompt below\n"
        f"2. Paste into ChatGPT / Gemini / Grok\n"
        f"3. Download the image\n"
        f"4. Send it here as a photo\n\n"
        f"<b>PROMPT</b>\n\n"
        f"<code>{prompt}</code>\n\n"
        f"<i>Waiting for your image...</i>",
        parse_mode="HTML",
    )


async def handle_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _pending_image_post_id

    if str(update.effective_chat.id) != settings.TELEGRAM_CHAT_ID:
        return

    if not _pending_image_post_id:
        await update.message.reply_text(
            "[ERROR] No post waiting for an image.\n"
            "Send /generate_image first, then send the image."
        )
        return

    post_id = _pending_image_post_id

    try:
        import os, time
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        save_path = f"/tmp/libero_img_{int(time.time())}.jpg"
        await file.download_to_drive(save_path)

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
                    upload_url, content=image_bytes,
                    headers={
                        "Authorization": f"Bearer {_settings.SUPABASE_SERVICE_KEY}",
                        "Content-Type": "image/jpeg",
                        "x-upsert": "true",
                    },
                )
            if upload_resp.status_code in (200, 201):
                public_url = f"{_settings.SUPABASE_URL}/storage/v1/object/public/post-images/{storage_path}"
            else:
                raise Exception(f"Storage {upload_resp.status_code}: {upload_resp.text[:200]}")
        except Exception as storage_err:
            logger.warning(f"[handle_photo] Storage upload failed: {storage_err}")
            public_url = f"telegram://user_upload/{os.path.basename(save_path)}"

        queries.update_post_image(post_id=post_id, image_url=public_url, image_generator="user_upload")

        try:
            os.remove(save_path)
        except Exception:
            pass

        _pending_image_post_id = None
        post = queries.get_post_by_id(post_id)
        status = post.get("status", "draft") if post else "draft"
        stored_ok = not public_url.startswith("telegram://")

        await update.message.reply_text(
            f"<b>[IMAGE SAVED]</b>\n\n"
            f"<code>POST ID    {post_id[:8].upper()}\n"
            f"STATUS     {status.upper()}\n"
            f"STORAGE    {'SUPABASE OK' if stored_ok else 'LOCAL REF'}</code>\n\n"
            + (
                "Post is approved — goes live at scheduled time with this image."
                if status == "approved" else
                "Send /approve to confirm. Post will publish with this image."
            ),
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(f"[handle_photo] Failed: {e}", exc_info=True)
        await update.message.reply_text(f"[ERROR] Failed to save image: {str(e)[:200]}\n\nTry again.")


async def handle_mind_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
