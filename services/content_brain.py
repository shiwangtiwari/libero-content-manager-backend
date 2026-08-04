"""
services/content_brain.py — Content intelligence engine.

Receives a SignalBundle from signal_collector.py and outputs a TopicSelection:
  - Selected topic (string)
  - Priority that triggered it (P1/P2/P3)
  - Signal card (JSONB-ready dict)
  - telegram_input_id if P1 (for marking as used in queries.py)

Priority rules (master doc Section 11.1):
  P1: Telegram input from Shiwang in last 7 days — always wins
  P2: LinkedIn trending topic that fills a gap in last 20 posts (both conditions required)
  P3: LinkedIn trending topic even if partially covered — last resort

Fix (2026-08-04 v2):
  _detect_niche_matches() was checking Product Management BEFORE Developer-to-PM.
  The regex r"pm\b" matched the word "PM" inside "Developer to PM Transition",
  so that topic got primary_cat="Product Management" instead of "Developer-to-PM".
  With the GTA6 post (primary_cat=PM) in queue, queued_category_set={"Product Management"}.
  ALL niche topics ended up hard-blocked because they all mapped to PM.
  The brain fell through to Pass 4 (all constraints relaxed) and picked the first
  topic anyway — which happened to be a PM/DevPM topic, giving the impression of
  "same topic again".

  Fix: check specific categories (Developer-to-PM, NextLeap, AI in PM, etc.) BEFORE
  the broad Product Management catch. "Developer to PM Transition" now correctly
  returns primary_cat="Developer-to-PM", which is NOT in queued_category_set when
  the queued post is "Product Management" — so Pass 3 fires correctly.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from services.signal_collector import SignalBundle, LinkedInTopic, category_recently_used

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Niche content filter — topic must match at least one category
# ---------------------------------------------------------------------------
_NICHE_PATTERNS: list[re.Pattern] = [re.compile(p, re.I) for p in [
    r"\bproduct\s*manag",
    r"\bpm\b",
    r"\bproduct\s*manager",
    r"\bdev(eloper)?\s*to\s*pm",
    r"\bengineer\s*to\s*pm",
    r"\bnextleap",
    r"\bfellowship",
    r"\bai\s*(in|for|and)\s*(pm|product)",
    r"\bai\s*product",
    r"\bindia\s*(tech|startup|pm)",
    r"\bpersonal\s*brand",
    r"\blinkedin\s*content",
    r"\blearning\s*in\s*public",
    r"\btech\s*career",
    r"\bcareer\s*(transition|switch|change)",
    r"\broadmap",
    r"\bprioritis",
    r"\buser\s*research",
    r"\bmetrics?",
    r"\bokr",
    r"\bnorth\s*star",
    r"\bstartup",
    r"\bsaas",
    r"\btechnical\s*(pm|product)",
    r"\binterview\s*(prep|preparation)?",
    r"\bproduct\s*spec",
    r"\bproduct\s*think",
]]


def _matches_niche(topic: str) -> bool:
    return any(p.search(topic) for p in _NICHE_PATTERNS)


def _topic_is_covered(topic: str, covered_keywords: set[str]) -> bool:
    """
    True if 2+ niche keywords from the topic appear in covered_keywords.
    Threshold of 2 avoids false positives from single common words like 'pm'.
    """
    topic_lower = topic.lower()
    hits = sum(1 for kw in covered_keywords if kw in topic_lower)
    return hits >= 2


# ---------------------------------------------------------------------------
# Topic selection result
# ---------------------------------------------------------------------------

@dataclass
class TopicSelection:
    topic: str
    priority: str                      # "P1" | "P2" | "P3" | "P_fallback"
    signal_card: dict[str, Any]
    telegram_input_id: str | None      # Set if P1, so caller can mark input used
    last_5_post_topics: list[str]      # Passed to content_generator as context


# ---------------------------------------------------------------------------
# Signal card builder
# ---------------------------------------------------------------------------

def _build_signal_card(
    priority: str,
    selected_topic: str,
    trigger: str,
    trending_topics: list[str],
    gap_filled: str,
    telegram_input_used: str,
    niche_matches: list[str],
    last_5_post_topics: list[str],
) -> dict[str, Any]:
    return {
        "primary_signal": priority,
        "selected_topic": selected_topic,
        "trigger": trigger,
        "trending_topics": trending_topics[:5],
        "gap_filled": gap_filled,
        "telegram_input_used": telegram_input_used,
        "niche_match": niche_matches,
        "last_5_post_topics": last_5_post_topics,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _input_to_topic(message: str) -> str:
    """Convert a raw Telegram message to a post topic."""
    topic = message.strip().rstrip("?").strip()
    if len(topic) > 80:
        for sep in [".", "!", "\n"]:
            if sep in topic:
                topic = topic.split(sep)[0].strip()
                break
    return topic[:120]


def _detect_niche_matches(topic: str) -> list[str]:
    """
    Detect niche categories for a topic string. Returns list with primary first.

    ORDERING IS CRITICAL: more specific patterns must be checked BEFORE broader ones.
    'Developer to PM Transition' contains the word 'pm' which would match
    Product Management if checked first. By checking Developer-to-PM first,
    we correctly assign it as the primary category.
    """
    matches = []
    t = topic.lower()

    # --- Specific categories first ---
    # Developer-to-PM: must come before Product Management to avoid 'pm' false match
    if re.search(r"dev\s*(eloper)?\s*to\s*pm|engineer\s*(to|-)\s*pm|technical\s*pm"
                 r"|from\s*(developer|engineer|dev|coder|coding)", t):
        matches.append("Developer-to-PM")

    # NextLeap: must come before Product Management (contains 'pm program')
    if re.search(r"nextleap|fellowship|pm\s*program", t):
        matches.append("NextLeap fellowship")

    # AI in PM: specific enough, but check before broad PM
    if re.search(r"ai\s*(in|for|and|tool)|llm|chatgpt|gemini|claude|ai\s*product", t):
        matches.append("AI in PM")

    # India Tech
    if re.search(r"india\s*tech|indian\s*startup|india\s*startup|india\s*pm", t):
        matches.append("India Tech")

    # Personal Brand
    if re.search(r"personal\s*brand|linkedin\s*content|learning\s*in\s*public", t):
        matches.append("Personal Brand")

    # --- Broad Product Management catch — checked LAST ---
    # Uses stricter patterns that don't match 'pm' inside 'developer to pm'
    if re.search(r"product\s*manag|product\s*think|product\s*spec|product\s*strateg"
                 r"|roadmap|prioritis|user\s*research|north\s*star|okr"
                 r"|data.driven\s*product|product\s*decision", t):
        matches.append("Product Management")

    # Final broad catch: \bpm\b or metrics — only if nothing more specific matched
    if not matches and re.search(r"\bpm\b|metrics?|saas|startup", t):
        matches.append("Product Management")

    return matches or ["Product Management"]


def _gap_description(topic: str, covered: set[str]) -> str:
    topic_lower = topic.lower()
    hits = [kw for kw in covered if kw in topic_lower]
    if not hits:
        return f"No post on '{topic}' in last 20 posts"
    return f"Partially covered ({', '.join(hits[:3])}) — fresh angle available"


def _last_5_topics(bundle: SignalBundle) -> list[str]:
    topics = []
    for ct in bundle.covered_topics[:5]:
        if ct.keywords:
            topics.append(", ".join(ct.keywords[:4]))
        elif ct.signal_card.get("selected_topic"):
            topics.append(ct.signal_card["selected_topic"])
    return topics


def _human_time(iso_str: str) -> str:
    try:
        import datetime, pytz
        ist = pytz.timezone("Asia/Kolkata")
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = ist.localize(dt)
        else:
            dt = dt.astimezone(ist)
        diff = datetime.datetime.now(ist) - dt
        hours = int(diff.total_seconds() / 3600)
        if hours < 1:
            return "just now"
        if hours < 24:
            return f"{hours}h ago"
        return f"{diff.days}d ago"
    except Exception:
        return iso_str


def _topic_already_queued(topic: str, bundle: SignalBundle) -> bool:
    """
    Returns True if this exact topic (or a very close variant) is already
    in the queue as a draft/approved post. Prevents the same signal being
    picked on every pipeline run when mark_signal_used() wasn't called,
    and catches cases where category detection misses the duplicate
    (e.g. GTA6 categorised as General so category block doesn't fire).
    """
    topic_lower = topic.lower().strip()[:80]
    for queued_str in bundle.queued_topic_set:
        # Exact match or one contains the other (handles trigger vs topic variants)
        if topic_lower == queued_str:
            return True
        if len(topic_lower) > 15 and topic_lower in queued_str:
            return True
        if len(queued_str) > 15 and queued_str in topic_lower:
            return True
    return False


def _category_is_blocked(category: str, bundle: SignalBundle) -> tuple[bool, str]:
    """
    Check whether a category should be avoided.

    Returns (blocked: bool, reason: str).

    Two-tier avoidance:
      HARD: category is in queued_category_set (post already in queue with this category)
      SOFT: category appeared in the last MIN_CATEGORY_GAP_POSTS posts (recent_category_sequence)
    """
    if not category or category == "General":
        return False, ""

    # Non-PM categories are never blocked — they add variety
    non_blockable = {"Gaming", "Building", "Culture", "Personal", "General", "AI"}
    if category in non_blockable:
        return False, ""

    # Hard block: already in queue
    if category in bundle.queued_category_set:
        return True, f"HARD BLOCK — '{category}' already in queue"

    # Soft block: appeared too recently
    from services.signal_collector import MIN_CATEGORY_GAP_POSTS
    if category_recently_used(category, bundle.recent_category_sequence, MIN_CATEGORY_GAP_POSTS):
        return True, f"SOFT BLOCK — '{category}' in last {MIN_CATEGORY_GAP_POSTS} posts"

    return False, ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def select_topic(bundle: SignalBundle) -> TopicSelection:
    """
    Score all signals and return the best topic.
    Never raises — falls back to hardcoded topic if everything is empty.

    Category avoidance is applied in four passes:
      Pass 1: gap fill + NOT blocked (hard or soft)
      Pass 2: gap fill + NOT hard blocked (soft block tolerated)
      Pass 3: any niche topic + NOT hard blocked
      Pass 4: any niche topic (all constraints relaxed — last resort)
    """
    trending_strings = [lt.topic for lt in bundle.linkedin_topics]
    # Pool topics (source="niche_pool" or "cache") are pre-curated — don't filter them.
    # Only apply _matches_niche to HTTP-scraped topics.
    # Without this, Culture/Gaming/Building/Personal topics are silently excluded
    # and the brain only ever sees PM topics.
    niche_topics = [
        lt for lt in bundle.linkedin_topics
        if lt.source in ("niche_pool", "cache") or _matches_niche(lt.topic)
    ]
    last5 = _last_5_topics(bundle)

    logger.info(
        "select_topic: %d niche topics, queued_categories=%s, recent_seq=%s",
        len(niche_topics),
        bundle.queued_category_set,
        bundle.recent_category_sequence[:6],
    )

    # --- P1: Telegram input (highest priority, always wins) ---
    if bundle.telegram_inputs:
        inp = bundle.telegram_inputs[0]
        topic = _input_to_topic(inp.message)
        gap = _gap_description(topic, bundle.covered_keyword_set)
        niche_matches = _detect_niche_matches(topic) or ["Developer-to-PM"]
        sc = _build_signal_card(
            priority="telegram_input",
            selected_topic=topic,
            trigger=f'You said: "{inp.message[:120]}" ({_human_time(inp.created_at)})',
            trending_topics=trending_strings,
            gap_filled=gap,
            telegram_input_used=f'"{inp.message[:120]}" — {_human_time(inp.created_at)}',
            niche_matches=niche_matches,
            last_5_post_topics=last5,
        )
        logger.info("P1 selected: '%s' (Telegram input)", topic)
        return TopicSelection(
            topic=topic, priority="P1", signal_card=sc,
            telegram_input_id=inp.id, last_5_post_topics=last5,
        )

    # --- P2/P3: LinkedIn trending ---

    # Pass 1: gap fill + fully unblocked (not hard, not soft) + not already queued
    for lt in niche_topics:
        if _topic_already_queued(lt.topic, bundle):
            continue
        if not _topic_is_covered(lt.topic, bundle.covered_keyword_set):
            topic_categories = _detect_niche_matches(lt.topic)
            primary_cat = topic_categories[0] if topic_categories else ""
            blocked, reason = _category_is_blocked(primary_cat, bundle)
            if not blocked:
                gap = _gap_description(lt.topic, bundle.covered_keyword_set)
                sc = _build_signal_card(
                    priority="linkedin_trending",
                    selected_topic=lt.topic,
                    trigger=f'LinkedIn trending + gap fill: "{lt.topic}"',
                    trending_topics=trending_strings,
                    gap_filled=gap,
                    telegram_input_used="",
                    niche_matches=topic_categories,
                    last_5_post_topics=last5,
                )
                logger.info("P2 selected: '%s' (gap + unblocked '%s')", lt.topic, primary_cat)
                return TopicSelection(
                    topic=lt.topic, priority="P2", signal_card=sc,
                    telegram_input_id=None, last_5_post_topics=last5,
                )

    # Pass 2: gap fill, soft block tolerated (but NOT hard block) + not already queued
    for lt in niche_topics:
        if _topic_already_queued(lt.topic, bundle):
            continue
        if not _topic_is_covered(lt.topic, bundle.covered_keyword_set):
            topic_categories = _detect_niche_matches(lt.topic)
            primary_cat = topic_categories[0] if topic_categories else ""
            hard_blocked = primary_cat in bundle.queued_category_set
            if not hard_blocked:
                gap = _gap_description(lt.topic, bundle.covered_keyword_set)
                sc = _build_signal_card(
                    priority="linkedin_trending",
                    selected_topic=lt.topic,
                    trigger=f'LinkedIn trending + gap fill (recent repeat tolerated): "{lt.topic}"',
                    trending_topics=trending_strings,
                    gap_filled=gap,
                    telegram_input_used="",
                    niche_matches=topic_categories,
                    last_5_post_topics=last5,
                )
                logger.info("P2 selected: '%s' (gap fill, soft tolerated, cat='%s')", lt.topic, primary_cat)
                return TopicSelection(
                    topic=lt.topic, priority="P2", signal_card=sc,
                    telegram_input_id=None, last_5_post_topics=last5,
                )

    # Pass 3: any niche topic, not hard blocked, not already queued
    for lt in niche_topics:
        if _topic_already_queued(lt.topic, bundle):
            continue
        topic_categories = _detect_niche_matches(lt.topic)
        primary_cat = topic_categories[0] if topic_categories else ""
        hard_blocked = primary_cat in bundle.queued_category_set
        if not hard_blocked:
            sc = _build_signal_card(
                priority="linkedin_trending",
                selected_topic=lt.topic,
                trigger=f'LinkedIn trending (different category from queue): "{lt.topic}"',
                trending_topics=trending_strings,
                gap_filled="",
                telegram_input_used="",
                niche_matches=topic_categories,
                last_5_post_topics=last5,
            )
            logger.info("P3 selected: '%s' (not hard-blocked, cat='%s')", lt.topic, primary_cat)
            return TopicSelection(
                topic=lt.topic, priority="P3", signal_card=sc,
                telegram_input_id=None, last_5_post_topics=last5,
            )

    # Pass 4: all constraints relaxed — only fires when every niche topic's
    # primary category is in queued_category_set (extremely unlikely in practice)
    if niche_topics:
        lt = niche_topics[0]
        sc = _build_signal_card(
            priority="linkedin_trending",
            selected_topic=lt.topic,
            trigger=f'LinkedIn trending (all categories queued, constraints relaxed): "{lt.topic}"',
            trending_topics=trending_strings,
            gap_filled="",
            telegram_input_used="",
            niche_matches=_detect_niche_matches(lt.topic),
            last_5_post_topics=last5,
        )
        logger.warning("P4 selected: '%s' (all %d niche topics hard-blocked)", lt.topic, len(niche_topics))
        return TopicSelection(
            topic=lt.topic, priority="P3", signal_card=sc,
            telegram_input_id=None, last_5_post_topics=last5,
        )

    # Fallback: no niche topics at all
    if bundle.linkedin_topics:
        lt = bundle.linkedin_topics[0]
        sc = _build_signal_card(
            priority="linkedin_trending",
            selected_topic=lt.topic,
            trigger=f'Fallback — no niche topics found: "{lt.topic}"',
            trending_topics=trending_strings,
            gap_filled="",
            telegram_input_used="",
            niche_matches=[],
            last_5_post_topics=last5,
        )
        logger.warning("Fallback selected: '%s' (no niche match)", lt.topic)
        return TopicSelection(
            topic=lt.topic, priority="P_fallback", signal_card=sc,
            telegram_input_id=None, last_5_post_topics=last5,
        )

    # Absolute fallback
    topic = "What I'm learning in my first month as a NextLeap PM Fellow"
    sc = _build_signal_card(
        priority="fallback_hardcoded",
        selected_topic=topic,
        trigger="All signal sources empty — using hardcoded fallback",
        trending_topics=[],
        gap_filled="",
        telegram_input_used="",
        niche_matches=["NextLeap fellowship", "Developer-to-PM"],
        last_5_post_topics=last5,
    )
    logger.error("All signals empty — hardcoded fallback topic used")
    return TopicSelection(
        topic=topic, priority="P_fallback", signal_card=sc,
        telegram_input_id=None, last_5_post_topics=last5,
    )
