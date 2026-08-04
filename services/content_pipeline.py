"""
services/content_pipeline.py — Full autonomous content generation pipeline.

Called by APScheduler on Mon/Tue/Wed at 6:00 AM IST.
Also called manually via /run_now Telegram command or POST /run_now endpoint.

Pipeline steps:
  1. Slot guard             → skip if ALL three weekly slots already have a post
  2. Collect signals        → signal_collector.collect_all_signals()
  3. Select topic           → content_brain.select_topic()
  4. Generate post          → content_generator.generate_post(topic, last_topics, signal_card)
  5. Compute next slot      → schedule_utils.next_available_slot()
  6. Save to Supabase       → queries.create_post()
  7. Mark telegram input    → queries.mark_telegram_input_used() [if P1]
  8. Telegram notification  → routers.telegram.send_telegram_message()

Fix (2026-08-04):
  - Slot guard added at step 1: if next_available_slot() finds no empty slot
    (i.e. all Tue/Wed/Thu this week already have a draft/approved/scheduled post),
    the pipeline returns {"success": True, "skipped": True} immediately.
    This prevents duplicate posts when Shiwang manually generates all three posts
    on Monday. The Mon/Tue/Wed 6AM scheduler jobs will all skip gracefully.
  - When /run_now is triggered manually, the guard is bypassed intentionally
    (run_now=True flag) so Shiwang can still force-generate if needed.

All DB calls use YOUR exact queries.py function signatures.
generate_post signature matches YOUR services/content_generator.py exactly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def run_content_pipeline(run_now: bool = False) -> dict[str, Any]:
    """
    Execute the full content generation pipeline.

    Args:
        run_now: If True, bypass the slot guard (manual /run_now trigger).

    Returns:
        {
          "success": bool,
          "skipped": bool,   # True if slot guard fired
          "post_id": str|None,
          "topic": str|None,
          "error": str|None
        }
    """
    result: dict[str, Any] = {
        "success": False, "skipped": False,
        "post_id": None, "topic": None, "error": None
    }

    try:
        logger.info("=== Content pipeline starting (run_now=%s) ===", run_now)

        # ── Step 0: Check if this is for a Thursday slot (Market Strategy day) ─
        # If the next available slot is a Thursday, generate a Market Strategy post
        # instead of a regular post.
        is_thursday_slot = False
        try:
            from services.schedule_utils import next_available_slot as _peek_slot
            import pytz as _pytz
            from datetime import datetime as _dt
            _IST = _pytz.timezone("Asia/Kolkata")
            _peek = _peek_slot()
            _peek_dt = _IST.localize(_dt.strptime(_peek, "%Y-%m-%d %H:%M"))
            is_thursday_slot = (_peek_dt.weekday() == 3)  # 3 = Thursday
            logger.info("[pipeline] Next slot: %s (Thursday: %s)", _peek, is_thursday_slot)
        except Exception as _e:
            logger.debug("[pipeline] Thursday check failed (non-fatal): %s", _e)

        # If Thursday slot, run the Market Strategy pipeline instead
        if is_thursday_slot and not run_now:
            from services.content_pipeline import _run_market_strategy_pipeline
            return await _run_market_strategy_pipeline()

        # ── Step 1: Slot guard ────────────────────────────────────────────────
        # Check if there is actually a free slot to fill.
        # next_available_slot() already checks occupied slots from the DB.
        # But we need to confirm the slot it returns isn't already beyond this
        # week — if it had to go 2+ weeks out, all this week's slots are full.
        #
        # Only enforce when run_now=False (scheduled auto-generation).
        # Manual /run_now always proceeds regardless.
        if not run_now:
            from services.schedule_utils import next_available_slot, ist_now
            from db import queries as _q

            # How many of the three weekly slots already have a post?
            queued = _q.get_posts_by_status(
                ["draft", "approved", "scheduled", "pending_reschedule"]
            )
            queued_slots = {p["scheduled_time"] for p in queued if p.get("scheduled_time")}

            # Compute all three posting slots for the next 7 days
            import datetime as _dt
            import pytz as _pytz
            _IST = _pytz.timezone("Asia/Kolkata")
            now = ist_now()
            _WEEKLY_SLOTS = [(1, 8, 30), (2, 12, 0), (3, 9, 0)]
            upcoming_slots: list[str] = []
            for delta in range(8):
                candidate_date = (now + _dt.timedelta(days=delta)).date()
                weekday = candidate_date.weekday()
                for wd, hour, minute in _WEEKLY_SLOTS:
                    if weekday == wd:
                        slot_dt = _IST.localize(_dt.datetime(
                            candidate_date.year, candidate_date.month,
                            candidate_date.day, hour, minute
                        ))
                        if slot_dt > now:
                            slot_str = slot_dt.strftime("%Y-%m-%d %H:%M")
                            upcoming_slots.append(slot_str)

            # If every upcoming slot in the next 7 days already has a post → skip
            open_slots = [s for s in upcoming_slots if s not in queued_slots]
            if not open_slots:
                logger.info(
                    "[pipeline] Slot guard fired — all %d upcoming slots occupied (%s). Skipping.",
                    len(upcoming_slots), queued_slots,
                )
                result["success"] = True
                result["skipped"] = True
                result["error"] = f"All slots occupied: {sorted(queued_slots)}"
                return result

            logger.info(
                "[pipeline] Slot guard passed — %d open slot(s) available: %s",
                len(open_slots), open_slots,
            )

        # ── Step 2: Collect signals ───────────────────────────────────────────
        from services.signal_collector import collect_all_signals
        bundle = await collect_all_signals()
        logger.info(
            "Signals collected: %d telegram inputs, %d linkedin topics, %d past posts",
            len(bundle.telegram_inputs),
            len(bundle.linkedin_topics),
            len(bundle.covered_topics),
        )

        # ── Step 3: Select topic ──────────────────────────────────────────────
        from services.content_brain import select_topic
        selection = select_topic(bundle)
        result["topic"] = selection.topic
        logger.info("Topic selected [%s]: '%s'", selection.priority, selection.topic)

        # ── Step 4: Generate post via Anthropic API ───────────────────────────
        from services.content_generator import generate_post
        last_topics_str = "\n".join(f"- {t}" for t in selection.last_5_post_topics) \
                          if selection.last_5_post_topics else ""
        trigger = selection.signal_card.get("trigger", selection.topic)

        # Fetch recent angles for diversity tracking
        used_angles: list[str] = []
        try:
            recent_angle_rows = queries.get_used_angles(n=6)
            used_angles = [r["used_angle"] for r in recent_angle_rows if r.get("used_angle")]
        except Exception:
            pass

        post_text = await generate_post(
            topic=selection.topic,
            last_topics=last_topics_str,
            signal_card=trigger,
            used_angles=used_angles,
        )

        # Strip markdown formatting that LinkedIn renders as literal characters
        import re as _re
        post_text = post_text.replace("**", "").replace("__", "")
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
            if viral_score_v2 > viral_score:
                post_text = post_text_v2
                viral_score = viral_score_v2
                logger.info("Using v2 post (higher score)")
            else:
                logger.info("Keeping v1 post (v2 didn't improve)")

        # ── Step 5: Compute next posting slot ─────────────────────────────────
        from services.schedule_utils import next_available_slot
        scheduled_time = next_available_slot()
        logger.info("Next posting slot: %s IST", scheduled_time)

        # ── Step 6: Save to Supabase ──────────────────────────────────────────
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

        # Save angle + topic slug for diversity tracking
        try:
            from services.content_generator import detect_angle
            angle = detect_angle(post_text)
            topic_slug = selection.topic.lower().strip()[:120]
            queries.save_post_angle(post_id, angle, topic_slug)
            logger.info("Saved angle='%s' slug='%s'", angle, topic_slug[:40])
        except Exception as exc:
            logger.warning("Failed to save post angle: %s", exc)

        # ── Step 7: Mark telegram input as used if P1 ─────────────────────────
        if selection.telegram_input_id:
            try:
                queries.mark_telegram_input_used(selection.telegram_input_id, post_id)
                logger.info("Marked telegram input %s as used", selection.telegram_input_id)
            except Exception as exc:
                logger.warning("Failed to mark telegram input as used: %s", exc)

        # ── Step 7b: Mark LinkedIn signal as used so it never repeats ────────
        # mark_signal_used was never called — same cached topic kept appearing
        # every pipeline run within 24h because it stayed used=False forever.
        try:
            signal_topic = selection.signal_card.get("selected_topic", "")
            if signal_topic:
                all_signals = queries.get_unused_signals(source="linkedin_trending", max_age_hours=24)
                for sig in all_signals:
                    if sig.get("topic", "").lower().strip() == signal_topic.lower().strip():
                        queries.mark_signal_used(sig["id"], post_id)
                        logger.info("Marked LinkedIn signal '%s' as used", signal_topic[:50])
                        break
        except Exception as exc:
            logger.warning("Failed to mark LinkedIn signal as used: %s", exc)

        # ── Step 8: Telegram notification ─────────────────────────────────────
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
    if first_line.endswith("?"):
        score += 18
    elif re.search(r"\d+", first_line):
        score += 16
    elif re.search(r"^(nobody|most|stop|why|the truth|unpopular|here|what)", first_line, re.I):
        score += 15
    elif len(first_line) < 80:
        score += 11
    else:
        score += 5

    # 2. Personal voice (0-20)
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
    if 150 <= word_count <= 250:
        score += 8
    elif 100 <= word_count < 150 or 250 < word_count <= 300:
        score += 5
    else:
        score += 2
    para_breaks = post_text.count("\n\n")
    score += min(7, para_breaks * 2)
    hashtags = len(re.findall(r"#\w+", post_text))
    if 1 <= hashtags <= 3:
        score += 5
    elif hashtags > 3:
        score += 1

    return min(100, score)


# ---------------------------------------------------------------------------
# Sync wrapper for APScheduler
# ---------------------------------------------------------------------------


async def _run_market_strategy_pipeline() -> dict:
    """
    Thursday-specific pipeline: picks a Market Strategy story and generates
    a post in the personal storytelling format with a trending hook.
    """
    result = {"success": False, "skipped": False, "post_id": None,
              "topic": None, "error": None, "is_market_strategy": True}
    try:
        from db import queries
        from services.content_generator import generate_market_strategy_post, detect_angle
        from services.schedule_utils import next_available_slot

        # Get unused strategy
        strategy = queries.get_unused_market_strategy()
        if not strategy:
            logger.warning("[market_strategy] No strategies available — falling back to regular pipeline")
            return await run_content_pipeline(run_now=True)

        # Get used trending refs for deduplication
        used_refs = queries.get_used_trending_refs(n=20)

        # Generate post
        post_text, trending_ref = await generate_market_strategy_post(
            strategy=strategy,
            used_trending_refs=used_refs,
        )

        # Clean up
        import re as _re
        post_text = post_text.replace("**", "").replace("__", "")
        post_text = _re.sub(r"  +", " ", post_text).strip()

        scheduled_time = next_available_slot()

        # Build signal card
        signal_card = {
            "primary_signal": "market_strategy",
            "selected_topic": strategy.get("title", ""),
            "trigger": f"Thursday Market Strategy: {strategy.get('strategy_name', '')} — {strategy.get('company', '')}",
            "niche_match": ["Market Strategy"],
            "company": strategy.get("company", ""),
            "strategy_name": strategy.get("strategy_name", ""),
            "industry": strategy.get("industry", ""),
        }

        from services.content_pipeline import _compute_viral_score
        viral_score = _compute_viral_score(post_text)

        post_row = queries.create_post(
            content=post_text,
            scheduled_time=scheduled_time,
            signal_card=signal_card,
            viral_score=viral_score,
            platform="linkedin",
        )
        post_id = post_row["id"]

        # Save angle + topic slug
        angle = detect_angle(post_text)
        queries.save_post_angle(post_id, angle, strategy.get("title", "").lower()[:120])

        # Mark strategy as used
        queries.mark_market_strategy_used(strategy["id"], post_id)

        # Save trending reference
        if trending_ref:
            queries.save_used_trending_ref(post_id, trending_ref)

        # Notify via Telegram
        from routers.telegram import send_telegram_message
        from services.schedule_utils import human_readable_slot
        readable = human_readable_slot(scheduled_time)

        await send_telegram_message(
            f"📊 <b>Thursday Market Strategy Draft</b>\n\n"
            f"<b>Scheduled:</b> {readable}\n"
            f"<b>Strategy:</b> {strategy.get('strategy_name')}\n"
            f"<b>Company:</b> {strategy.get('company')}\n"
            f"<b>Post ID:</b> <code>{post_id[:8]}</code>\n\n"
            f"{post_text[:400]}...\n\n"
            f"/approve — Approve\n"
            f"/reject — Reject and get a different strategy\n"
            f"/generate_image — Attach image"
        )

        result["success"] = True
        result["post_id"] = post_id
        result["topic"] = strategy.get("title")
        logger.info("[market_strategy] Pipeline complete: post_id=%s", post_id)

    except Exception as exc:
        logger.error("[market_strategy] Pipeline failed: %s", exc, exc_info=True)
        result["error"] = str(exc)

    return result


def run_content_pipeline_sync() -> dict[str, Any]:
    """Sync wrapper called by APScheduler as fallback."""
    return asyncio.run(run_content_pipeline())
