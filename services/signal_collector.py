"""
services/signal_collector.py — Signal collection orchestrator.

Gathers all three signal sources and returns a unified SignalBundle:
  1. Telegram inputs from Shiwang (last 7 days, unused only)   → db.queries.get_unused_telegram_inputs()
  2. LinkedIn trending topics                                   → pw.linkedin_scraper.collect_linkedin_topics()
  3. Gap analysis from last 20 posts in Supabase               → db.queries.get_last_n_posts()

Fix (2026-08-04):
  - Gap analysis now ALSO includes posts currently in queue (draft / approved / scheduled /
    pending_reschedule). A PM post sitting approved in queue counts as "covered" so the
    next generation won't pick another PM topic.
  - Category-gap window: the same niche category must have at least MIN_CATEGORY_GAP_POSTS
    posts of any category between appearances. This replaces the old "never in 20 posts" rule.
    Default: 6. So you can post about PM again, just not twice in a row or too close together.
  - covered_keyword_set still used for keyword-level deduplication as before.
  - queued_category_set: new field on SignalBundle — the set of primary categories currently
    in the queue (draft/approved). content_brain uses this to skip categories already queued.

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

# How many posts of ANY category must appear between two posts of the same category.
# E.g. MIN_CATEGORY_GAP_POSTS = 6 means: if the last 6 posts all cover "Product Management",
# the next one should pick a different category. But if there's been a mix, PM is fine again.
MIN_CATEGORY_GAP_POSTS = 6


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
    # Primary niche category of this post (for category-gap tracking)
    primary_category: str = ""
    # True if this post is still in queue (not yet posted) — stronger avoidance
    in_queue: bool = False


@dataclass
class SignalBundle:
    """Everything content_brain.py needs to select a topic."""
    telegram_inputs: list[TelegramInput] = field(default_factory=list)
    linkedin_topics: list[LinkedInTopic] = field(default_factory=list)
    covered_topics: list[CoveredTopic] = field(default_factory=list)
    covered_keyword_set: set[str] = field(default_factory=set)

    # Categories currently sitting in queue (draft / approved / scheduled).
    # content_brain uses this to avoid generating a post in a category that's
    # already waiting for approval.
    queued_category_set: set[str] = field(default_factory=set)

    # The ordered list of primary categories from recent posts (newest first).
    # Used to enforce MIN_CATEGORY_GAP_POSTS.
    recent_category_sequence: list[str] = field(default_factory=list)


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


def _detect_primary_category(post: dict) -> str:
    """
    Detect the primary niche category of a post.
    Returns one of: "Product Management", "Developer-to-PM", "AI in PM",
    "India Tech", "Personal Brand", "NextLeap", or "General".
    """
    text = (post.get("content", "") + " " + str(post.get("signal_card", ""))).lower()
    sc = post.get("signal_card") or {}
    niche_list = sc.get("niche_match", [])
    if niche_list:
        return niche_list[0]  # Trust what's already stored in signal_card

    # Infer from content
    if re.search(r"nextleap|fellowship|pm\s*program", text):
        return "NextLeap fellowship"
    if re.search(r"dev\s*to\s*pm|engineer\s*to|technical\s*pm|coding|developer", text):
        return "Developer-to-PM"
    if re.search(r"ai\s*(in|for|and|tool)|llm|chatgpt|ai\s*product", text):
        return "AI in PM"
    if re.search(r"india|indian|startup|saas", text):
        return "India Tech"
    if re.search(r"personal\s*brand|linkedin\s*content|learning\s*in\s*public", text):
        return "Personal Brand"
    if re.search(r"product\s*manag|pm\b|roadmap|prioritis|user\s*research|north\s*star|okr", text):
        return "Product Management"
    return "General"


def _post_to_covered_topic(post: dict, in_queue: bool = False) -> CoveredTopic:
    """Convert a Supabase post row to a CoveredTopic."""
    keywords = _extract_keywords(post.get("content", ""))
    sc = post.get("signal_card") or {}
    if isinstance(sc, dict) and sc.get("trigger"):
        keywords += _extract_keywords(sc["trigger"])
    primary_cat = _detect_primary_category(post)
    return CoveredTopic(
        post_id=post.get("id", ""),
        keywords=list(set(keywords)),
        signal_card=sc,
        primary_category=primary_cat,
        in_queue=in_queue,
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

    # --- Source 3: Gap analysis ---
    # We analyse TWO pools:
    #   A) Posted posts (last 20) — historical topic coverage
    #   B) Queued posts (draft / approved / scheduled / pending_reschedule)
    #      — already-committed topics we must treat as "covered right now"
    #
    # Both pools contribute to covered_keyword_set.
    # Pool B also populates queued_category_set (hard block) and contributes
    # to recent_category_sequence for the gap window check.
    try:
        # Pool A: actually posted posts
        last_posts = queries.get_last_n_posts(n=20)
        posted_covered = [_post_to_covered_topic(p, in_queue=False) for p in last_posts]

        # Pool B: posts currently in queue
        queued_posts = queries.get_posts_by_status(
            ["draft", "approved", "scheduled", "pending_reschedule"]
        )
        queued_covered = [_post_to_covered_topic(p, in_queue=True) for p in queued_posts]

        # Combine: queued posts first (stronger weight), then posted history
        all_covered = queued_covered + posted_covered
        bundle.covered_topics = all_covered

        # Build keyword set from BOTH pools
        all_kw: set[str] = set()
        for ct in all_covered:
            all_kw.update(ct.keywords)
        bundle.covered_keyword_set = all_kw

        # queued_category_set: hard block — don't generate a post in a category
        # that's already sitting in queue waiting for approval
        bundle.queued_category_set = {
            ct.primary_category
            for ct in queued_covered
            if ct.primary_category and ct.primary_category != "General"
        }

        # recent_category_sequence: the ordered list of categories from recent posts
        # (queued newest-first, then posted newest-first) for the gap window check.
        # We only track the last MIN_CATEGORY_GAP_POSTS * 2 entries to keep it bounded.
        seq: list[str] = []
        for ct in queued_covered:
            if ct.primary_category:
                seq.append(ct.primary_category)
        for ct in posted_covered:
            if ct.primary_category:
                seq.append(ct.primary_category)
        bundle.recent_category_sequence = seq[:MIN_CATEGORY_GAP_POSTS * 3]

        logger.info(
            "Gap analysis: %d posted, %d queued, %d covered keywords, queued_categories=%s",
            len(posted_covered),
            len(queued_covered),
            len(all_kw),
            bundle.queued_category_set,
        )
    except Exception as exc:
        logger.error("Failed to run gap analysis: %s", exc)

    return bundle


def category_recently_used(category: str, sequence: list[str], gap: int = MIN_CATEGORY_GAP_POSTS) -> bool:
    """
    Returns True if `category` appears in the last `gap` entries of `sequence`.
    Used by content_brain to enforce the category spacing rule.

    Example with gap=6:
      sequence = [PM, PM, DevPM, AIinPM, IndTech, PersonBrand, PM, ...]
      category_recently_used("Product Management", sequence, 6) → True  (appears at pos 0,1,6)
      category_recently_used("NextLeap", sequence, 6) → False (not in last 6)
    """
    recent = sequence[:gap]
    return category in recent
