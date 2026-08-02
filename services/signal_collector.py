"""
services/signal_collector.py — Signal collection orchestrator.

Gathers all three signal sources and returns a unified SignalBundle:
  1. Telegram inputs from Shiwang (last 7 days, unused only)   → db.queries.get_unused_telegram_inputs()
  2. LinkedIn trending topics                                   → pw.linkedin_scraper.collect_linkedin_topics()
  3. Gap analysis from last 20 posts in Supabase               → db.queries.get_last_n_posts()

All DB calls use the exact function names from YOUR db/queries.py.
content_brain.py receives the bundle and scores it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from db import queries
from pw.linkedin_scraper import collect_linkedin_topics

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TelegramInput:
    id: str
    message: str
    created_at: str


@dataclass
class LinkedInTopic:
    topic: str
    category: str
    source: str  # "rss" | "playwright" | "niche_pool"


@dataclass
class CoveredTopic:
    """A topic extracted from a past post (for gap analysis)."""
    post_id: str
    keywords: list[str]
    signal_card: dict[str, Any]


@dataclass
class SignalBundle:
    """Everything content_brain.py needs to select a topic."""
    telegram_inputs: list[TelegramInput] = field(default_factory=list)
    linkedin_topics: list[LinkedInTopic] = field(default_factory=list)
    covered_topics: list[CoveredTopic] = field(default_factory=list)
    covered_keyword_set: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Gap analysis helpers
# ---------------------------------------------------------------------------

_NICHE_KEYWORDS = {
    "product management", "pm", "product manager", "product spec",
    "roadmap", "north star", "metrics", "okr", "discovery", "user research",
    "developer to pm", "engineer to pm", "technical pm", "dev to pm",
    "nextleap", "fellowship", "pm fellowship",
    "ai in pm", "ai tools", "chatgpt", "llm", "ai product",
    "india tech", "indian startup", "startup pm", "saas",
    "personal brand", "linkedin content", "learning in public",
    "interview", "prioritisation", "prioritization",
}


def _extract_keywords(text: str) -> list[str]:
    """Return niche keywords found in the text."""
    text_clean = re.sub(r"[^\w\s]", " ", text.lower())
    return [kw for kw in _NICHE_KEYWORDS if kw in text_clean]


def _post_to_covered_topic(post: dict[str, Any]) -> CoveredTopic:
    """Convert a Supabase post row to a CoveredTopic."""
    keywords = _extract_keywords(post.get("content", ""))
    sc = post.get("signal_card") or {}
    if isinstance(sc, dict) and sc.get("trigger"):
        keywords += _extract_keywords(sc["trigger"])
    return CoveredTopic(
        post_id=post.get("id", ""),
        keywords=list(set(keywords)),
        signal_card=sc,
    )


# ---------------------------------------------------------------------------
# LinkedIn signal caching via content_signals table
# ---------------------------------------------------------------------------

async def _get_linkedin_topics_with_cache() -> list[LinkedInTopic]:
    """
    Return LinkedIn topics, using content_signals table as a cache.
    If fresh signals exist (unused, from last ~20 hours), use them.
    Otherwise scrape fresh and cache them.
    """
    # Check cache — get unused signals from 'linkedin_trending' source
    cached = queries.get_unused_signals(source="linkedin_trending")
    if cached:
        logger.info("Using %d cached LinkedIn signals from content_signals table", len(cached))
        return [
            LinkedInTopic(
                topic=row.get("topic", ""),
                category=row.get("raw_data", {}).get("category", "LinkedIn Trending"),
                source="cache",
            )
            for row in cached
            if row.get("topic")
        ]

    # Fresh scrape
    logger.info("No cached signals — collecting fresh LinkedIn topics...")
    raw_topics = await collect_linkedin_topics()
    topics = []

    for t in raw_topics:
        if not t.get("topic"):
            continue
        try:
            # content_signals source CHECK: 'linkedin_trending', 'past_post_gap', 'telegram_input'
            queries.create_signal(
                source="linkedin_trending",
                topic=t["topic"],
                raw_data={"category": t.get("category", ""), "scrape_source": t.get("source", "")},
            )
        except Exception as exc:
            logger.warning("Failed to cache signal '%s': %s", t["topic"], exc)

        topics.append(LinkedInTopic(
            topic=t["topic"],
            category=t.get("category", "LinkedIn Trending"),
            source=t.get("source", "niche_pool"),
        ))

    logger.info("Collected and cached %d fresh LinkedIn topics", len(topics))
    return topics


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def collect_all_signals() -> SignalBundle:
    """
    Collect all three signal sources and return a SignalBundle.
    Never raises — logs errors and continues with whatever is available.
    """
    bundle = SignalBundle()

    # --- Source 1: Telegram inputs ---
    # Uses: queries.get_unused_telegram_inputs(days=7)
    try:
        raw_inputs = queries.get_unused_telegram_inputs(days=7)
        bundle.telegram_inputs = [
            TelegramInput(
                id=row["id"],
                message=row["message"],
                created_at=row["created_at"],
            )
            for row in raw_inputs
            if row.get("message")
        ]
        logger.info("Telegram inputs: %d unused in last 7 days", len(bundle.telegram_inputs))
    except Exception as exc:
        logger.error("Failed to collect Telegram inputs: %s", exc)

    # --- Source 2: LinkedIn trending ---
    try:
        bundle.linkedin_topics = await _get_linkedin_topics_with_cache()
        logger.info("LinkedIn topics available: %d", len(bundle.linkedin_topics))
    except Exception as exc:
        logger.error("Failed to collect LinkedIn topics: %s", exc)

    # --- Source 3: Gap analysis from last 20 posts ---
    # Uses: queries.get_last_n_posts(n=20)
    try:
        last_posts = queries.get_last_n_posts(n=20)
        covered = [_post_to_covered_topic(p) for p in last_posts]
        bundle.covered_topics = covered
        all_kw: set[str] = set()
        for ct in covered:
            all_kw.update(ct.keywords)
        bundle.covered_keyword_set = all_kw
        logger.info(
            "Gap analysis: %d past posts, %d covered keywords",
            len(covered), len(all_kw),
        )
    except Exception as exc:
        logger.error("Failed to run gap analysis: %s", exc)

    return bundle
