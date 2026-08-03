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
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from services.signal_collector import SignalBundle, LinkedInTopic

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
    # If long paragraph, use first sentence
    if len(topic) > 80:
        for sep in [".", "!", "\n"]:
            if sep in topic:
                topic = topic.split(sep)[0].strip()
                break
    return topic[:120]


def _detect_niche_matches(topic: str) -> list[str]:
    matches = []
    t = topic.lower()
    if re.search(r"product\s*manag|pm\b|roadmap|prioritis|user\s*research|north\s*star|okr", t):
        matches.append("Product Management")
    if re.search(r"dev\s*to\s*pm|engineer\s*to|technical\s*pm|coding|developer", t):
        matches.append("Developer-to-PM")
    if re.search(r"nextleap|fellowship|pm\s*program", t):
        matches.append("NextLeap fellowship")
    if re.search(r"ai\s*(in|for|and|tool)|llm|chatgpt|ai\s*product", t):
        matches.append("AI in PM")
    if re.search(r"india|indian|startup|saas", t):
        matches.append("India Tech")
    if re.search(r"personal\s*brand|linkedin\s*content|learning\s*in\s*public", t):
        matches.append("Personal Brand")
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


def _last_2_categories(bundle: SignalBundle) -> list[str]:
    """
    Return the niche categories of the last 2 posts.
    Used to prevent two adjacent posts feeling like the same topic area.
    """
    categories = []
    for ct in bundle.covered_topics[:2]:
        sc = ct.signal_card or {}
        niche = sc.get("niche_match", [])
        if niche:
            categories.append(niche[0])
    return categories


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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def select_topic(bundle: SignalBundle) -> TopicSelection:
    """
    Score all signals and return the best topic.
    Never raises — falls back to hardcoded topic if everything is empty.
    """
    trending_strings = [lt.topic for lt in bundle.linkedin_topics]
    niche_topics = [lt for lt in bundle.linkedin_topics if _matches_niche(lt.topic)]
    last5 = _last_5_topics(bundle)

    # --- P1: Telegram input (highest priority, always wins) ---
    if bundle.telegram_inputs:
        inp = bundle.telegram_inputs[0]  # most recent unused
        topic = _input_to_topic(inp.message)
        gap = _gap_description(topic, bundle.covered_keyword_set)
        sc = _build_signal_card(
            priority="telegram_input",
            selected_topic=topic,
            trigger=f'You said: "{inp.message[:120]}" ({_human_time(inp.created_at)})',
            trending_topics=trending_strings,
            gap_filled=gap,
            telegram_input_used=f'"{inp.message[:120]}" — {_human_time(inp.created_at)}',
            niche_matches=_detect_niche_matches(topic) or ["Developer-to-PM"],
            last_5_post_topics=last5,
        )
        logger.info("P1 selected: '%s' (Telegram input)", topic)
        return TopicSelection(
            topic=topic, priority="P1", signal_card=sc,
            telegram_input_id=inp.id, last_5_post_topics=last5,
        )

    # --- P2: Trending topic that fills a content gap AND is a different category ---
    recent_categories = _last_2_categories(bundle)
    last_category = recent_categories[0] if recent_categories else None

    # First pass: find topic that fills gap AND is different category from last post
    for lt in niche_topics:
        if not _topic_is_covered(lt.topic, bundle.covered_keyword_set):
            topic_categories = _detect_niche_matches(lt.topic)
            topic_primary_cat = topic_categories[0] if topic_categories else ""
            if topic_primary_cat != last_category:
                gap = _gap_description(lt.topic, bundle.covered_keyword_set)
                sc = _build_signal_card(
                    priority="linkedin_trending",
                    selected_topic=lt.topic,
                    trigger=f'LinkedIn trending in niche + gap: "{lt.topic}"',
                    trending_topics=trending_strings,
                    gap_filled=gap,
                    telegram_input_used="",
                    niche_matches=topic_categories,
                    last_5_post_topics=last5,
                )
                logger.info("P2 selected: '%s' (trending + gap fill + category variety)", lt.topic)
                return TopicSelection(
                    topic=lt.topic, priority="P2", signal_card=sc,
                    telegram_input_id=None, last_5_post_topics=last5,
                )

    # Second pass: fill gap even if same category (better than no gap fill)
    for lt in niche_topics:
        if not _topic_is_covered(lt.topic, bundle.covered_keyword_set):
            gap = _gap_description(lt.topic, bundle.covered_keyword_set)
            sc = _build_signal_card(
                priority="linkedin_trending",
                selected_topic=lt.topic,
                trigger=f'LinkedIn trending in niche + gap: "{lt.topic}"',
                trending_topics=trending_strings,
                gap_filled=gap,
                telegram_input_used="",
                niche_matches=_detect_niche_matches(lt.topic),
                last_5_post_topics=last5,
            )
            logger.info("P2 selected: '%s' (trending + gap fill, same category tolerated)", lt.topic)
            return TopicSelection(
                topic=lt.topic, priority="P2", signal_card=sc,
                telegram_input_id=None, last_5_post_topics=last5,
            )

    # --- P3: Any niche trending topic, even if partially covered ---
    if niche_topics:
        lt = niche_topics[0]
        sc = _build_signal_card(
            priority="linkedin_trending",
            selected_topic=lt.topic,
            trigger=f'LinkedIn trending (partial coverage ok): "{lt.topic}"',
            trending_topics=trending_strings,
            gap_filled="",
            telegram_input_used="",
            niche_matches=_detect_niche_matches(lt.topic),
            last_5_post_topics=last5,
        )
        logger.info("P3 selected: '%s' (trending, ignoring coverage)", lt.topic)
        return TopicSelection(
            topic=lt.topic, priority="P3", signal_card=sc,
            telegram_input_id=None, last_5_post_topics=last5,
        )

    # --- Fallback: use any available topic ---
    if bundle.linkedin_topics:
        lt = bundle.linkedin_topics[0]
        sc = _build_signal_card(
            priority="linkedin_trending",
            selected_topic=lt.topic,
            trigger=f'Fallback — no niche match found, using: "{lt.topic}"',
            trending_topics=trending_strings,
            gap_filled="",
            telegram_input_used="",
            niche_matches=[],
            last_5_post_topics=last5,
        )
        logger.warning("Fallback selected: '%s'", lt.topic)
        return TopicSelection(
            topic=lt.topic, priority="P_fallback", signal_card=sc,
            telegram_input_id=None, last_5_post_topics=last5,
        )

    # --- Absolute fallback ---
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
