"""
services/content_pipeline.py — Full autonomous content generation pipeline.

Called by APScheduler on Mon/Tue/Wed at 6:00 AM IST.

Pipeline steps:
  1. Collect signals         → signal_collector.collect_all_signals()
  2. Select topic            → content_brain.select_topic()
  3. Generate post           → content_generator.generate_post(topic, last_topics, signal_card)
  4. Compute next slot       → schedule_utils.next_available_slot()
  5. Save to Supabase        → queries.create_post()
  6. Mark telegram input used → queries.mark_telegram_input_used() [if P1]
  7. Telegram notification   → routers.telegram.send_telegram_message()

All DB calls use YOUR exact queries.py function signatures.
generate_post signature matches YOUR services/content_generator.py exactly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def run_content_pipeline() -> dict[str, Any]:
    """
    Execute the full content generation pipeline.
    Returns: {"success": bool, "post_id": str|None, "topic": str|None, "error": str|None}
    """
    result: dict[str, Any] = {
        "success": False, "post_id": None, "topic": None, "error": None
    }

    try:
        logger.info("=== Content pipeline starting ===")

        # Step 1: Collect signals
        from services.signal_collector import collect_all_signals
        bundle = await collect_all_signals()
        logger.info(
            "Signals collected: %d telegram inputs, %d linkedin topics, %d past posts",
            len(bundle.telegram_inputs),
            len(bundle.linkedin_topics),
            len(bundle.covered_topics),
        )

        # Step 2: Select topic
        from services.content_brain import select_topic
        selection = select_topic(bundle)
        result["topic"] = selection.topic
        logger.info("Topic selected [%s]: '%s'", selection.priority, selection.topic)

        # Step 3: Generate post via Anthropic API
        # Matches YOUR content_generator.generate_post(topic, last_topics, signal_card) signature
        from services.content_generator import generate_post
        last_topics_str = "\n".join(f"- {t}" for t in selection.last_5_post_topics) \
                          if selection.last_5_post_topics else ""
        trigger = selection.signal_card.get("trigger", selection.topic)

        post_text = await generate_post(
            topic=selection.topic,
            last_topics=last_topics_str,
            signal_card=trigger,
        )

        # Strip markdown formatting that LinkedIn renders as literal characters
        import re as _re
        post_text = post_text.replace("**", "").replace("__", "")
        # Clean up any double spaces left behind
        post_text = _re.sub(r"  +", " ", post_text).strip()

        logger.info("Post generated: %d chars", len(post_text))

        # Compute viral score
        viral_score = _compute_viral_score(post_text)
        logger.info("Viral score: %d/100 (first attempt)", viral_score)

        # If score is below 70, regenerate once with an explicit quality boost instruction
        if viral_score < 70:
            logger.info("Score below 70 — regenerating with quality boost")
            boost_signal = (
                f"{trigger} | QUALITY BOOST: previous attempt scored {viral_score}/100. "
                f"This time: stronger hook (specific tension or number in line 1), "
                f"more personal story detail (exact situation, not generic), "
                f"end with a genuine question the reader actually wants to answer."
            )
            post_text_v2 = await generate_post(
                topic=selection.topic,
                last_topics=last_topics_str,
                signal_card=boost_signal,
            )
            post_text_v2 = post_text_v2.replace("**", "").replace("__", "")
            import re as _re2
            post_text_v2 = _re2.sub(r"  +", " ", post_text_v2).strip()
            viral_score_v2 = _compute_viral_score(post_text_v2)
            logger.info("Viral score v2: %d/100", viral_score_v2)
            # Use whichever version scored higher
            if viral_score_v2 > viral_score:
                post_text = post_text_v2
                viral_score = viral_score_v2
                logger.info("Using v2 post (higher score)")
            else:
                logger.info("Keeping v1 post (v2 didn't improve)")

        # Step 4: Compute next posting slot
        from services.schedule_utils import next_available_slot
        scheduled_time = next_available_slot()
        logger.info("Next posting slot: %s IST", scheduled_time)

        # Step 5: Save to Supabase
        # Uses YOUR queries.create_post(content, scheduled_time, signal_card, viral_score, platform)
        from db import queries
        post_row = queries.create_post(
            content=post_text,
            scheduled_time=scheduled_time,
            signal_card=selection.signal_card,
            viral_score=viral_score,
            platform="linkedin",
        )
        post_id = post_row["id"]
        result["post_id"] = post_id
        logger.info("Post saved to Supabase: %s", post_id)

        # Step 6: Mark telegram input as used if P1
        if selection.telegram_input_id:
            try:
                queries.mark_telegram_input_used(selection.telegram_input_id, post_id)
                logger.info("Marked telegram input %s as used", selection.telegram_input_id)
            except Exception as exc:
                logger.warning("Failed to mark telegram input as used: %s", exc)

        # Step 7: Telegram notification with draft + signal card
        await _send_draft_notification(
            post_id=post_id,
            post_text=post_text,
            signal_card=selection.signal_card,
            priority=selection.priority,
            scheduled_time=scheduled_time,
            viral_score=viral_score,
        )

        result["success"] = True
        logger.info("=== Content pipeline complete: post_id=%s ===", post_id)

    except Exception as exc:
        logger.error("Content pipeline failed: %s", exc, exc_info=True)
        result["error"] = str(exc)
        try:
            from routers.telegram import send_telegram_message
            await send_telegram_message(
                f"❌ <b>Content pipeline failed</b>\n\n"
                f"<b>Error:</b> {str(exc)[:300]}\n\n"
                f"Check Railway logs for details."
            )
        except Exception as notify_exc:
            logger.error("Failed to send error alert: %s", notify_exc)

    return result


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------

_PRIORITY_LABELS = {
    "P1": "🎯 P1 — Your Telegram Input",
    "P2": "📈 P2 — Trending + Gap Fill",
    "P3": "📊 P3 — Trending Topic",
    "P_fallback": "🔄 Fallback Topic",
}


async def _send_draft_notification(
    post_id: str,
    post_text: str,
    signal_card: dict[str, Any],
    priority: str,
    scheduled_time: str,
    viral_score: int,
) -> None:
    """Send Telegram notification with draft post and signal card."""
    from routers.telegram import send_telegram_message
    from services.schedule_utils import human_readable_slot

    readable_time = human_readable_slot(scheduled_time)
    priority_label = _PRIORITY_LABELS.get(priority, priority)

    trigger = signal_card.get("trigger", "")
    tg_input = signal_card.get("telegram_input_used", "")
    gap = signal_card.get("gap_filled", "")
    trending = signal_card.get("trending_topics", [])
    niche = signal_card.get("niche_match", [])

    # Build signal card section
    sc_lines = [f"<b>Signal:</b> {priority_label}"]
    if trigger:
        sc_lines.append(f"<b>Trigger:</b> {trigger[:120]}")
    if tg_input:
        sc_lines.append(f"<b>Your input:</b> {tg_input[:100]}")
    if gap:
        sc_lines.append(f"<b>Gap filled:</b> {gap[:80]}")
    if trending:
        sc_lines.append(f"<b>Trending:</b> {', '.join(trending[:3])}")
    if niche:
        sc_lines.append(f"<b>Niche:</b> {', '.join(niche)}")

    preview = post_text[:400] + ("..." if len(post_text) > 400 else "")

    message = (
        f"📝 <b>New draft ready for review</b>\n\n"
        f"<b>Scheduled:</b> {readable_time}\n"
        f"<b>Viral Score:</b> {viral_score}/100\n"
        f"<b>Post ID:</b> <code>{post_id[:8]}</code>\n\n"
        f"{preview}\n\n"
        f"{'—' * 20}\n"
        f"<b>SIGNAL CARD</b>\n"
        f"{chr(10).join(sc_lines)}\n\n"
        f"{'—' * 20}\n"
        f"<b>Actions:</b>\n"
        f"/approve — Approve this post\n"
        f"/reject — Reject and discard\n"
        f"/generate_image chatgpt — Generate image\n"
        f"/queue — View all pending posts"
    )

    try:
        await send_telegram_message(message)
        logger.info("Draft notification sent to Telegram")
    except Exception as exc:
        logger.error("Failed to send draft notification: %s", exc)


# ---------------------------------------------------------------------------
# Viral score (lightweight, no external deps)
# ---------------------------------------------------------------------------

def _compute_viral_score(post_text: str) -> int:
    """
    Score 0-100 across 5 dimensions (20 points each).

    Intentionally does NOT penalise non-PM topics — a Gaming or Anime
    post can score just as high as a PM frameworks post if it has
    good structure, personal voice, specificity, and a strong CTA.
    """
    import re
    score = 0
    lines = post_text.strip().split("\n")
    first_line = lines[0].strip() if lines else ""
    content_lines = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    cta_line = content_lines[-1] if content_lines else ""

    # 1. Hook strength (0-20)
    # Short, specific, surprising, or asks a question
    if first_line.endswith("?"):
        score += 18
    elif re.search(r"\d+", first_line):
        score += 16   # has a number in hook
    elif re.search(r"^(nobody|most|stop|why|the truth|unpopular|here|what)", first_line, re.I):
        score += 15   # strong opening word
    elif len(first_line) < 80:
        score += 11
    else:
        score += 5

    # 2. Personal voice (0-20)
    # "I" statements with specific details are the highest trust signal
    personal = len(re.findall(r"\b(I |I'|my |me |I was|I learned|I built|I spent|I killed|I made)\b",
                              post_text, re.I))
    specific_time = bool(re.search(
        r"\b(last week|last month|yesterday|4 years|6 months|\d+ days|\d+ weeks|\d+ months)\b",
        post_text, re.I
    ))
    score += min(20, personal * 3 + (5 if specific_time else 0))

    # 3. Specificity — concrete details, numbers, named things (0-20)
    numbers = len(re.findall(r"\d+", post_text))
    named = len(re.findall(
        r"\b(LinkedIn|GTA|PlayStation|Solo Leveling|Netflix|Spotify|Figma|Notion|"
        r"Slack|Railway|Supabase|Claude|ChatGPT|India|Mumbai|Bangalore|"
        r"PM|API|OKR|PRD|UX|MVP|SaaS|startup|Libero)\b",
        post_text, re.I
    ))
    score += min(20, numbers * 2 + named * 2)

    # 4. Engagement CTA (0-20)
    if cta_line.endswith("?"):
        # Real question vs generic "Thoughts?"
        if re.search(r"\b(what|how|when|which|have you|do you|what would|tell me)\b", cta_line, re.I):
            score += 20
        else:
            score += 13
    elif re.search(r"(comment|share|tell me|drop|thoughts|agree|disagree)", cta_line, re.I):
        score += 8
    else:
        score += 2

    # 5. Content quality signals (0-20)
    word_count = len(post_text.split())
    # LinkedIn sweet spot: 150-250 words
    if 150 <= word_count <= 250:
        score += 8
    elif 100 <= word_count < 150 or 250 < word_count <= 300:
        score += 5
    else:
        score += 2
    # Paragraph breaks (white space = readable)
    para_breaks = post_text.count("\n\n")
    score += min(7, para_breaks * 2)
    # Hashtags present (max 3)
    hashtags = len(re.findall(r"#\w+", post_text))
    if 1 <= hashtags <= 3:
        score += 5
    elif hashtags > 3:
        score += 1

    return min(100, score)


# ---------------------------------------------------------------------------
# Sync wrapper for APScheduler
# ---------------------------------------------------------------------------

def run_content_pipeline_sync() -> dict[str, Any]:
    """
    Sync wrapper called by APScheduler (which uses AsyncIOScheduler in your scheduler.py).
    Since your scheduler is AsyncIOScheduler, it can call the async function directly.
    This sync wrapper is provided as a fallback.
    """
    return asyncio.run(run_content_pipeline())
