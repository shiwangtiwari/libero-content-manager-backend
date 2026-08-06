"""
Telegram bot command handlers.
Bot runs in polling mode (not webhook) as a background task inside FastAPI.

Aesthetic: system terminal style. Bracketed markers instead of emojis.
[CONFIRMED] [REJECTED] [RESCHEDULED] [EXPIRED] [UPDATED] [SIGNAL SAVED]
[IMAGE PROMPT] [IMAGE SAVED] [RUNNING] [STRIPPED] [POSTED] [POST FAILED]

LIBERO is the system name — not "Claude". Session health shows LIBERO, not CLAUDE.

_pending_image_post_id is stored in Supabase user_profile table (survives restarts).

Fix (2026-08-04):
  - /reject and /approve now resolve queue numbers against ALL queue statuses
    (draft, approved, scheduled, pending_reschedule) so /reject 2 works even
    when post 1 or 3 are already approved.
  - /queue numbering and _resolve_post_id now use the same full status list,
    so the numbers always match what the user sees.
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

# The FULL set of statuses the queue shows. Used by both /queue display
# and _resolve_post_id so numbers always match.
_QUEUE_STATUSES = ["draft", "approved", "scheduled", "pending_reschedule"]


async def handle_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    posts = queries.get_posts_by_status(_QUEUE_STATUSES)
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
    # Sort newest first — this defines the numbering for /reject 1, /reject 2 etc.
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


# ── _resolve_post_id ──────────────────────────────────────────────────────────

def _resolve_post_id(arg: str, statuses: list[str]) -> tuple[str | None, str | None]:
    """
    Resolve a user-supplied argument to a full post ID.

    IMPORTANT: Always queries against _QUEUE_STATUSES (all queue posts) so that
    the position numbers shown in /queue match /reject N and /approve N.

    The `statuses` parameter is kept for API compatibility but is no longer used
    as the search scope — we always search the full queue so numbering is stable.

    Accepts:
      - A queue number: "1", "2", "3"  (from /queue output)
      - A short ID:    "0bab32a5"      (first 8 chars shown in /queue)
      - A full UUID:   "0bab32a5-..."  (full ID)
    Returns (full_post_id, error_message).
    """
    # Always load the full queue — same scope as /queue display
    posts = queries.get_posts_by_status(_QUEUE_STATUSES)
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


# ── Command: /approve ─────────────────────────────────────────────────────────

async def handle_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /approve  or  /approve <number_or_id>
    /approve       — approves the most recent draft
    /approve 1     — approves queue item #1
    /approve 2     — approves queue item #2
    /approve 0bab32 — approves by short ID"""

    if not context.args:
        # No argument — approve the most recent approvable post (draft/pending)
        drafts = queries.get_posts_by_status(["draft", "pending_reschedule"])
        if not drafts:
            await update.message.reply_text("[ERROR] No draft posts to approve.")
            return
        post_id = drafts[0]["id"]
    else:
        # Resolve against full queue (so /approve 2 works even if post 1 is approved)
        post_id, err = _resolve_post_id(context.args[0], _QUEUE_STATUSES)
        if err:
            await update.message.reply_text(f"[ERROR] {err}")
            return

    result = approve_post(post_id)
    if result.get("error"):
        await update.message.reply_text(f"[ERROR] {result['error']}")
        return

    # approve_post returns {"ok": True, "scheduled_time": "..."} — no "post" key
    scheduled_time = result.get("scheduled_time", "TBD")
    await update.message.reply_text(
        f"<b>[CONFIRMED]</b>\n\n"
        f"<code>STATUS     APPROVED\n"
        f"SLOT       {scheduled_time} IST\n"
        f"ID         {post_id[:8].upper()}</code>\n\n"
        f"Post will go live at the scheduled time.",
        parse_mode="HTML",
    )


# ── Command: /reject ──────────────────────────────────────────────────────────

async def handle_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /reject  or  /reject <number_or_id>
    /reject       — rejects the most recent draft
    /reject 1     — rejects queue item #1  (draft OR approved)
    /reject 2     — rejects queue item #2
    /reject 0bab32 — rejects by short ID"""

    if not context.args:
        # No argument — reject the most recent draft/pending
        drafts = queries.get_posts_by_status(["draft", "pending_reschedule"])
        if not drafts:
            await update.message.reply_text("[ERROR] No draft posts to reject.")
            return
        post_id = drafts[0]["id"]
    else:
        # Resolve against full queue — /reject 2 works even if post 1 is approved
        post_id, err = _resolve_post_id(context.args[0], _QUEUE_STATUSES)
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
    if not context.args:
        posts = queries.get_posts_by_status(_QUEUE_STATUSES)
        if not posts:
            await update.message.reply_text("[ERROR] No posts to reschedule.")
            return
        post_id = posts[0]["id"]
    else:
        post_id, err = _resolve_post_id(context.args[0], _QUEUE_STATUSES)
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

        if stored_ok:
            img_line = (
                f"URL        <a href=\"{public_url}\">Tap to verify image loads</a>"
            )
            confidence = "[CONFIRMED] Image will post to LinkedIn."
        else:
            img_line = f"URL        {public_url[:60]}"
            confidence = "[WARN] Supabase upload failed. Post will go text-only unless you retry."

        await update.message.reply_text(
            f"<b>[IMAGE SAVED]</b>\n\n"
            f"<code>POST ID    {post_id[:8].upper()}\n"
            f"STATUS     {status.upper()}\n"
            f"STORAGE    {'SUPABASE OK' if stored_ok else 'FAILED — telegram:// fallback'}</code>\n"
            f"{img_line}\n\n"
            f"{confidence}\n\n"
            + (
                "Goes live at scheduled time with this image."
                if status == "approved" else
                "Send /approve to confirm. Post will publish with this image."
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    except Exception as e:
        logger.error("[handle_photo] Failed: %s", e, exc_info=True)
        await update.message.reply_text(f"[ERROR] Failed to save image: {str(e)[:200]}\n\nTry again.")


# ── Plain text handler — what's on my mind ────────────────────────────────────

async def handle_mind_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Any non-command text handler.
    First checks if it's a reply to /schedule_next (1 or 2).
    Otherwise treats as a content signal (P1 priority).
    Also checks if it introduces a genuinely new topic — if so, saves to
    custom_topics table so it becomes part of the permanent pool.
    """
    if str(update.effective_chat.id) != settings.TELEGRAM_CHAT_ID:
        return
    text = update.message.text.strip()

    # Check if this is a reply to /schedule_next
    chat_id = str(update.effective_chat.id)
    if text in ("1", "2") and chat_id in _pending_schedule_next:
        handled = await handle_schedule_next_choice(chat_id, text, update)
        if handled:
            return

    queries.create_telegram_input(message=text, source="telegram")

    # Check if this introduces a new topic not already in the pool
    new_topic_saved = False
    try:
        if len(text) >= 10 and not queries.topic_exists_in_pool(text):
            # Detect category from the text
            import re as _re
            t = text.lower()
            if _re.search(r"gta|game|gaming|playstation|xbox|steam", t):
                cat = "Gaming"
            elif _re.search(r"netflix|ott|bollywood|movie|series|show|web series", t):
                cat = "Culture"
            elif _re.search(r"ai|chatgpt|claude|llm|gemini|copilot|automation", t):
                cat = "AI"
            elif _re.search(r"build|automat|deploy|ship|launch|product", t):
                cat = "Building"
            elif _re.search(r"india|startup|saas|bengaluru|gurugram|delhi", t):
                cat = "India Tech"
            elif _re.search(r"dev.*pm|engineer.*pm|pm.*developer|transition|switch", t):
                cat = "Developer-to-PM"
            elif _re.search(r"product|pm|roadmap|priorit|sprint|okr|user research", t):
                cat = "Product Management"
            else:
                cat = "Personal"
            queries.create_custom_topic(topic=text[:200], category=cat)
            new_topic_saved = True
    except Exception as exc:
        logger.warning("Custom topic detection failed (non-fatal): %s", exc)

    extra = ""
    if new_topic_saved:
        extra = (
            "\n\n<code>NEW TOPIC ADDED to pool permanently.</code>\n"
            "Future posts can pick this up even without a Telegram input."
        )

    await update.message.reply_text(
        f"<b>[SIGNAL SAVED]</b>\n\n"
        f"<code>PRIORITY   P1 (highest)\n"
        f"SOURCE     telegram\n"
        f"FIRES AT   next content generation</code>\n\n"
        f"<i>\"{text[:100]}\"</i>"
        + extra,
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



# ── Command: /post_now ───────────────────────────────────────────────────────

async def handle_post_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Force-post an approved post to LinkedIn immediately — bypasses the scheduler.
    Used for test runs and emergency posting outside the Tue/Wed/Thu slots.

    Usage:
      /post_now              → posts the most recent approved post
      /post_now <id_or_num>  → posts a specific post by queue number or short ID

    The post must be in 'approved' status. Draft posts need /approve first.
    Uses the same claim → post → confirm flow as the scheduler so the duplicate
    guard still applies — no risk of double-posting.
    """
    if str(update.effective_chat.id) != settings.TELEGRAM_CHAT_ID:
        return

    # Resolve which post to post
    approved = queries.get_posts_by_status(["approved", "scheduled"])
    if not approved:
        await update.message.reply_text(
            "[ERROR] No approved or scheduled posts found.\n\n"
            "Approve a draft first with /approve, then run /post_now."
        )
        return

    if context.args:
        post_id, err = _resolve_post_id(context.args[0], _QUEUE_STATUSES)
        if err:
            await update.message.reply_text(f"[ERROR] {err}")
            return
        post = queries.get_post_by_id(post_id)
        if not post or post["status"] not in ("approved", "scheduled"):
            await update.message.reply_text(
                f"[ERROR] Post {context.args[0]} is not approved or scheduled "
                f"(status: {post['status'] if post else 'not found'}).\n"
                f"Run /approve first."
            )
            return
    else:
        post = approved[0]
        post_id = post["id"]

    # Image pre-flight — warn but don't block
    image_url = post.get("image_url") or ""
    if not image_url:
        img_warn = "[NO IMAGE] Post will go live as TEXT ONLY.\n"
    elif image_url.startswith("https://"):
        img_warn = "[IMAGE OK] Will post with image.\n"
    else:
        img_warn = "[BAD IMAGE URL] Supabase upload previously failed — TEXT ONLY.\n"

    hook = next((l.strip() for l in (post.get("content") or "").split("\n") if l.strip()), "")[:60]

    await update.message.reply_text(
        f"<b>[POST NOW]</b>\n\n"
        f"<code>POST ID    {post_id[:8].upper()}\n"
        f"SLOT       {post.get('scheduled_time', 'immediate')}\n"
        f"HOOK       {hook}...</code>\n\n"
        f"{img_warn}\n"
        f"Posting to LinkedIn now...",
        parse_mode="HTML",
    )

    # Claim the post atomically (duplicate guard)
    claimed = queries.claim_post_for_posting(post_id)
    if not claimed:
        await update.message.reply_text(
            "[ERROR] Could not claim post — it may have already been posted "
            "or claimed by the scheduler. Check /queue."
        )
        return

    try:
        from services.linkedin_poster import post_to_linkedin
        result = await post_to_linkedin(post_id)

        post_url = f"https://www.linkedin.com/feed/update/{result['linkedin_post_id']}"
        posted_with_image = result.get("posted_with_image", False)
        img_result = "WITH IMAGE" if posted_with_image else "TEXT ONLY"

        await update.message.reply_text(
            f"<b>[POSTED]</b>\n\n"
            f"<code>POST ID    {post_id[:8].upper()}\n"
            f"IMAGE      {img_result}\n"
            f"LI ID      {result['linkedin_post_id'][:30]}</code>\n\n"
            f"View: {post_url}",
            parse_mode="HTML",
        )

    except Exception as e:
        # Revert so post can be retried
        queries.revert_post_to_approved(post_id)
        await update.message.reply_text(
            f"<b>[POST FAILED]</b>\n\n"
            f"<code>ERROR  {str(e)[:200]}</code>\n\n"
            f"Post reverted to APPROVED. Fix the issue and try /post_now again.",
            parse_mode="HTML",
        )



# ── Command: /schedule_next ───────────────────────────────────────────────────

# State machine for /schedule_next — stored in memory (Railway restart clears it,
# acceptable since this is a short interactive flow)
_pending_schedule_next: dict[str, str] = {}  # {chat_id: post_id}


async def handle_schedule_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Insert the most recent draft into the schedule and ask whether to:
    1. Push to next posting day (shifting existing approved posts back)
    2. Schedule to next open slot
    
    Usage: /schedule_next
    Then reply with 1 or 2.
    """
    if str(update.effective_chat.id) != settings.TELEGRAM_CHAT_ID:
        return

    drafts = queries.get_posts_by_status(["draft", "pending_reschedule"])
    if not drafts:
        await update.message.reply_text(
            "[ERROR] No draft posts found.\n\n"
            "Run /run_now to generate a draft first."
        )
        return

    post = drafts[0]
    post_id = post["id"]
    hook = next((l.strip() for l in (post.get("content") or "").split("\n") if l.strip()), "")[:55]

    # Find next posting day slot and check if it's occupied
    from services.post_manager import next_available_slot, POSTING_SLOTS
    from datetime import datetime, timedelta
    import pytz as _pytz
    _IST = _pytz.timezone("Asia/Kolkata")
    now = datetime.now(_IST)

    # Find the chronologically next posting slot (occupied or not)
    next_slots = []
    for delta in range(8):
        candidate_date = (now + timedelta(days=delta)).date()
        weekday = candidate_date.weekday()
        for wd, hour, minute in POSTING_SLOTS:
            if weekday == wd:
                from datetime import datetime as _dt
                slot_dt = _IST.localize(_dt(
                    candidate_date.year, candidate_date.month,
                    candidate_date.day, hour, minute
                ))
                if slot_dt > now:
                    next_slots.append(slot_dt.strftime("%Y-%m-%d %H:%M"))

    next_slots = sorted(set(next_slots))[:3]

    # Check which slots are occupied
    queued = queries.get_posts_by_status(["approved", "scheduled", "pending_reschedule"])
    occupied = {p["scheduled_time"]: p for p in queued if p.get("scheduled_time")}

    next_day_slot = next_slots[0] if next_slots else None
    open_slot = next_available_slot()

    next_day_occupied = next_day_slot and next_day_slot in occupied
    next_day_post = occupied.get(next_day_slot, {})
    next_day_hook = ""
    if next_day_post:
        next_day_hook = next((
            l.strip() for l in (next_day_post.get("content") or "").split("\n") if l.strip()
        ), "")[:40]

    # Save pending state
    _pending_schedule_next[settings.TELEGRAM_CHAT_ID] = post_id

    if next_day_occupied:
        option1_text = (
            f"<b>1</b> — Post on <b>{next_day_slot}</b> (next posting day)\n"
            f"   Pushes current post (<i>{next_day_hook}...</i>) to the slot after"
        )
    else:
        option1_text = f"<b>1</b> — Post on <b>{next_day_slot}</b> (next posting day, slot is open)"

    option2_text = f"<b>2</b> — Schedule for next open slot: <b>{open_slot}</b>"

    await update.message.reply_text(
        f"<b>[SCHEDULE]</b>\n\n"
        f"<code>DRAFT      {post_id[:8].upper()}\n"
        f"HOOK       {hook}...</code>\n\n"
        f"Where should this post go?\n\n"
        f"{option1_text}\n\n"
        f"{option2_text}\n\n"
        f"Reply with <b>1</b> or <b>2</b>.",
        parse_mode="HTML",
    )


async def handle_schedule_next_choice(chat_id: str, choice: str, update: Update) -> bool:
    """
    Handle the 1/2 reply after /schedule_next.
    Returns True if handled, False if not a schedule_next reply.
    """
    post_id = _pending_schedule_next.get(chat_id)
    if not post_id:
        return False
    if choice not in ("1", "2"):
        return False

    del _pending_schedule_next[chat_id]

    from services.post_manager import next_available_slot, POSTING_SLOTS
    from datetime import datetime, timedelta
    import pytz as _pytz
    _IST = _pytz.timezone("Asia/Kolkata")
    now = datetime.now(_IST)

    if choice == "2":
        # Simple: assign to next open slot
        slot = next_available_slot()
        queries.update_post_status(post_id, "approved", {"scheduled_time": slot})
        await update.message.reply_text(
            f"<b>[SCHEDULED]</b>\n\n"
            f"<code>SLOT   {slot} IST\n"
            f"ID     {post_id[:8].upper()}</code>\n\n"
            f"Post will go live at the scheduled time.",
            parse_mode="HTML",
        )
        return True

    # Choice 1: push to next posting day, shift existing posts back
    # Find next posting day slot
    next_slots = []
    for delta in range(8):
        candidate_date = (now + timedelta(days=delta)).date()
        weekday = candidate_date.weekday()
        for wd, hour, minute in POSTING_SLOTS:
            if weekday == wd:
                from datetime import datetime as _dt
                slot_dt = _IST.localize(_dt(
                    candidate_date.year, candidate_date.month,
                    candidate_date.day, hour, minute
                ))
                if slot_dt > now:
                    next_slots.append((slot_dt, slot_dt.strftime("%Y-%m-%d %H:%M")))

    if not next_slots:
        await update.message.reply_text("[ERROR] Could not find next posting slot.")
        return True

    next_slots.sort(key=lambda x: x[0])
    target_slot_str = next_slots[0][1]

    # Get all approved posts sorted by slot
    queued = queries.get_posts_by_status(["approved", "scheduled"])
    queued_sorted = sorted(
        [p for p in queued if p.get("scheduled_time") and p["id"] != post_id],
        key=lambda p: p["scheduled_time"]
    )

    # Build the full ordered slot list
    all_slot_strs = sorted(set([s for _, s in next_slots[:6]]))

    # Shift occupied posts: each gets bumped to the next slot in sequence
    occupied_at_target = [p for p in queued_sorted if p["scheduled_time"] == target_slot_str]
    if occupied_at_target:
        # Shift chain: find all posts in sequence and push each one forward
        bumped = []
        for i, p in enumerate(queued_sorted):
            if i < len(all_slot_strs) - 1:
                next_slot = all_slot_strs[i + 1]
            else:
                # Find next available after all known slots
                last_slot_dt = next_slots[min(i, len(next_slots)-1)][0]
                # Find the slot after this one
                extra_slot = next_available_slot(after=last_slot_dt)
                next_slot = extra_slot
            queries.update_post_status(p["id"], p["status"], {"scheduled_time": next_slot})
            bumped.append(f"{p['id'][:8]} → {next_slot}")

        bump_summary = "\n".join(f"  {b}" for b in bumped[:3])
        shifted_msg = f"\n\n<code>Shifted posts:\n{bump_summary}</code>"
    else:
        shifted_msg = ""

    # Assign the new post to the target slot
    queries.update_post_status(post_id, "approved", {"scheduled_time": target_slot_str})

    await update.message.reply_text(
        f"<b>[SCHEDULED]</b>\n\n"
        f"<code>SLOT   {target_slot_str} IST\n"
        f"ID     {post_id[:8].upper()}</code>\n\n"
        f"Post inserted at next posting day."
        + shifted_msg,
        parse_mode="HTML",
    )
    return True


# ── Command: /check_image ─────────────────────────────────────────────────────

async def handle_check_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Check image status for all posts in queue.
    Shows exactly what will happen at posting time:
      CONFIRMED  → https:// URL, Supabase reachable → will post WITH image
      NO IMAGE   → no image_url set → will post TEXT ONLY
      BAD URL    → telegram:// fallback → will post TEXT ONLY
    """
    posts = queries.get_posts_by_status(["approved", "draft", "scheduled"])
    if not posts:
        await update.message.reply_text(
            "<b>[IMAGE CHECK]</b>\n\n<code>No posts in queue.</code>",
            parse_mode="HTML",
        )
        return

    lines = ["<b>[IMAGE CHECK]</b>\n"]
    for p in posts:
        url = p.get("image_url") or ""
        pid = p["id"][:8].upper()
        slot = p.get("scheduled_time", "?")
        hook = next((l.strip() for l in (p.get("content") or "").split("\n") if l.strip()), "")[:40]

        if not url:
            verdict = "NO IMAGE   — will post TEXT ONLY"
            url_line = "Run /generate_image to attach one."
        elif url.startswith("https://"):
            verdict = "CONFIRMED  — will post WITH IMAGE"
            url_line = f'<a href="{url}">Tap to verify image loads</a>'
        elif url.startswith("telegram://"):
            verdict = "BAD URL    — Supabase upload failed, TEXT ONLY"
            url_line = "Run /generate_image again and resend the photo."
        else:
            verdict = f"UNKNOWN    — {url[:40]}"
            url_line = "Run /generate_image to replace."

        lines.append(
            f"<code>{pid}  [{p['status'].upper()}]  {slot}\n"
            f"{hook}...\n"
            f"{verdict}</code>\n"
            f"{url_line}"
        )

    await update.message.reply_text(
        "\n\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )



# ── Command: /health_check ────────────────────────────────────────────────────

async def handle_health_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Full end-to-end pipeline health check.
    Tests: Anthropic API, Supabase, LinkedIn token, Storage upload,
    post generation, image prompt. Ends with a real LinkedIn post
    (labelled as health check test) — you delete it after confirming.

    This is a long-running command (~2-3 minutes). The bot sends
    progress updates as each step completes.
    """
    if str(update.effective_chat.id) != settings.TELEGRAM_CHAT_ID:
        return

    await update.message.reply_text(
        "<b>[HEALTH CHECK]</b> Starting full pipeline test...\n\n"
        "<code>This will take 2-3 minutes.\n"
        "A test post will be published to LinkedIn.\n"
        "Delete it after confirming.</code>",
        parse_mode="HTML",
    )

    results: list[tuple[str, bool, str]] = []  # (label, passed, detail)

    async def step(label: str, passed: bool, detail: str = ""):
        results.append((label, passed, detail))
        icon = "✅" if passed else "❌"
        await update.message.reply_text(
            f"{icon} <b>{label}</b>" + (f"\n<code>{detail[:200]}</code>" if detail else ""),
            parse_mode="HTML",
        )

    # Step 1: Anthropic API
    try:
        import os, httpx as _httpx
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        async with _httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-sonnet-4-5", "max_tokens": 10,
                      "messages": [{"role": "user", "content": "Reply: OK"}]},
            )
        ok = resp.status_code == 200
        await step("Anthropic API", ok, "claude-sonnet-4-5 reachable" if ok else f"HTTP {resp.status_code}")
    except Exception as e:
        await step("Anthropic API", False, str(e)[:100])

    # Step 2: Supabase
    try:
        posts = queries.get_posts_by_status("posted")
        await step("Supabase DB", True, f"{len(posts)} posted posts readable")
    except Exception as e:
        await step("Supabase DB", False, str(e)[:100])

    # Step 3: LinkedIn token
    try:
        from services.health_monitor import run_session_health_check
        health = await run_session_health_check()
        ok = health.get("healthy", False)
        await step("LinkedIn Token", ok, "Token valid" if ok else str(health.get("issues", []))[:100])
    except Exception as e:
        await step("LinkedIn Token", False, str(e)[:100])

    # Step 4: Supabase Storage
    try:
        import httpx as _httpx, time as _time
        from config import settings as _s
        test_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # minimal fake PNG
        storage_path = f"health_check/test_{int(_time.time())}.png"
        upload_url = f"{_s.SUPABASE_URL}/storage/v1/object/post-images/{storage_path}"
        async with _httpx.AsyncClient(timeout=15) as client:
            up = await client.post(
                upload_url, content=test_bytes,
                headers={"Authorization": f"Bearer {_s.SUPABASE_SERVICE_KEY}",
                         "Content-Type": "image/png", "x-upsert": "true"},
            )
        ok = up.status_code in (200, 201)
        if ok:
            # Clean up
            async with _httpx.AsyncClient(timeout=10) as client:
                await client.delete(
                    f"{_s.SUPABASE_URL}/storage/v1/object/post-images/{storage_path}",
                    headers={"Authorization": f"Bearer {_s.SUPABASE_SERVICE_KEY}"},
                )
        await step("Supabase Storage", ok, "Upload + delete OK" if ok else f"HTTP {up.status_code}")
    except Exception as e:
        await step("Supabase Storage", False, str(e)[:100])

    # Step 5: Post generation
    test_post_text = ""
    try:
        from services.content_generator import generate_post
        test_post_text = await generate_post(
            topic="[LIBERO HEALTH CHECK] This is an automated test post — please ignore or delete",
            last_topics="",
            signal_card="Health check test — generate a very short 3-sentence post",
        )
        ok = len(test_post_text) > 20
        await step("Post Generation", ok, f"{len(test_post_text)} chars generated")
    except Exception as e:
        await step("Post Generation", False, str(e)[:100])
        test_post_text = "[LIBERO HEALTH CHECK] Automated test post. Please delete this."

    # Step 6: Image prompt generation
    try:
        from routers.telegram import _build_image_prompt
        prompt = _build_image_prompt({
            "content": test_post_text,
            "signal_card": {"selected_topic": "Health check test"},
        })
        ok = len(prompt) > 50
        await step("Image Prompt", ok, f"{len(prompt)} char prompt generated")
        await update.message.reply_text(
            f"<b>IMAGE PROMPT FOR TEST:</b>\n\n<code>{prompt[:400]}</code>\n\n"
            f"Generate an image from this prompt and send it here as a photo.\n"
            f"Then I will post to LinkedIn with it.",
            parse_mode="HTML",
        )
    except Exception as e:
        await step("Image Prompt", False, str(e)[:100])

    # Save health check draft to Supabase so photo handler can attach image
    try:
        from services.schedule_utils import next_available_slot
        hc_slot = next_available_slot()
        hc_post = queries.create_post(
            content=test_post_text,
            scheduled_time=hc_slot,
            signal_card={"selected_topic": "Health check test", "trigger": "LIBERO HEALTH CHECK"},
            viral_score=0,
            platform="linkedin",
        )
        hc_id = hc_post["id"]
        queries.set_pending_image_post(hc_id)
        await update.message.reply_text(
            f"<b>[HEALTH CHECK DRAFT SAVED]</b>\n\n"
            f"<code>POST ID  {hc_id[:8].upper()}</code>\n\n"
            f"1. Generate image from prompt above\n"
            f"2. Send it here as a photo\n"
            f"3. Bot will post to LinkedIn with image\n"
            f"4. Check LinkedIn and delete the test post\n\n"
            f"Or send /post_now {hc_id[:8]} to post text-only now.",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"[ERROR] Could not save health check draft: {e}")

    # Summary
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    all_ok = passed == total
    summary_icon = "✅" if all_ok else "⚠️"
    await update.message.reply_text(
        f"{summary_icon} <b>Health check: {passed}/{total} passed</b>\n\n"
        + ("\n".join(
            f"{'✅' if ok else '❌'} {label}"
            for label, ok, _ in results
        )),
        parse_mode="HTML",
    )


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
    app.add_handler(CommandHandler("post_now", handle_post_now))
    app.add_handler(CommandHandler("schedule_next", handle_schedule_next))
    app.add_handler(CommandHandler("health_check", handle_health_check))
    app.add_handler(CommandHandler("check_image", handle_check_image))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_mind_input))
    return app
