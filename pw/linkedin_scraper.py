"""
pw/linkedin_scraper.py — LinkedIn trending topic collector.

Reality: LinkedIn blocks Railway datacenter IPs for scraping.
Strategy: Multi-layer fallback that always returns something useful.

Layer 1: LinkedIn public HTTP (best effort, usually blocked)
Layer 2: Full niche topic pool — all 46 topics, one per category per run,
         filtered against recently used topics and angles.
Layer 3: Custom topics added by Shiwang via Telegram (stored in Supabase)

Key changes (2026-08-04):
- OLD: _rotate_pool(n=6) returned only 6 topics per day via date-based shuffle.
  This meant only 3-4 non-PM topics surfaced and the brain always picked PM.
- NEW: get_full_pool() returns ALL topics from the pool (46 hardcoded + custom
  from Supabase), with one topic per category. The brain receives the full
  diverse set and picks based on what hasn't been used recently.
- Custom topics from Supabase custom_topics table are merged into the same pool.
"""

from __future__ import annotations

import logging
from datetime import date

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Niche topic pool — all topics, diverse categories
# Covers Shiwang's full identity: PM, Dev-to-PM, Building, AI, India Tech,
# Gaming, Personal, Culture. NOT just PM topics.
# ---------------------------------------------------------------------------
_NICHE_POOL: list[dict[str, str]] = [
    # Product Management — PM craft and career
    {"topic": "PM Interview Preparation", "category": "Product Management"},
    {"topic": "How to Write a Product Spec", "category": "Product Management"},
    {"topic": "North Star Metrics for Product Teams", "category": "Product Management"},
    {"topic": "Product Discovery vs Delivery", "category": "Product Management"},
    {"topic": "Saying No as a Product Manager", "category": "Product Management"},
    {"topic": "Roadmap Prioritisation Frameworks", "category": "Product Management"},
    {"topic": "Working with Engineering as a PM", "category": "Product Management"},
    {"topic": "User Research for Product Managers", "category": "Product Management"},
    {"topic": "Data-Driven Product Decisions", "category": "Product Management"},
    {"topic": "OKRs for Product Teams", "category": "Product Management"},

    # Developer to PM — the transition story
    {"topic": "Developer to PM Transition", "category": "Developer-to-PM"},
    {"topic": "Why Engineers Make Great PMs", "category": "Developer-to-PM"},
    {"topic": "Using Coding Skills in Product Management", "category": "Developer-to-PM"},
    {"topic": "From Engineer to Product Leader", "category": "Developer-to-PM"},
    {"topic": "Technical Debt and PM Decisions", "category": "Developer-to-PM"},
    {"topic": "Reading Technical Docs as a PM", "category": "Developer-to-PM"},

    # Building — making things, shipping, autonomy
    {"topic": "Building things that work when you are not watching", "category": "Building"},
    {"topic": "What I learned building an autonomous content system", "category": "Building"},
    {"topic": "Why I automated my LinkedIn presence", "category": "Building"},
    {"topic": "The difference between a project and a product", "category": "Building"},
    {"topic": "When to build vs when to buy", "category": "Building"},
    {"topic": "Shipping something real vs shipping something perfect", "category": "Building"},

    # AI — tools, workflows, honest takes
    {"topic": "AI Tools for Product Managers in 2025", "category": "AI"},
    {"topic": "How I actually use Claude and ChatGPT in my workflow", "category": "AI"},
    {"topic": "The gap between AI hype and AI usefulness", "category": "AI"},
    {"topic": "AI-First Product Thinking", "category": "AI"},
    {"topic": "What Copilot taught me about how developers really work", "category": "AI"},
    {"topic": "When AI makes you faster and when it makes you lazy", "category": "AI"},

    # India Tech — Indian ecosystem, startups, PM jobs
    {"topic": "PM Roles in Indian Startups", "category": "India Tech"},
    {"topic": "Breaking into Product Management in India", "category": "India Tech"},
    {"topic": "What India's startup ecosystem looks like from the inside", "category": "India Tech"},
    {"topic": "SaaS PM vs Consumer PM in India", "category": "India Tech"},
    {"topic": "Why Indian developers are underrated globally", "category": "India Tech"},

    # Gaming — Shiwang's genuine interest, connected to product thinking
    {"topic": "GTA 6 pre-order psychology and what it teaches product managers", "category": "Gaming"},
    {"topic": "Why gaming communities are the best product feedback loops", "category": "Gaming"},
    {"topic": "What game design gets right that most apps get wrong", "category": "Gaming"},
    {"topic": "The product lessons inside open-world games", "category": "Gaming"},

    # Personal — real observations, discipline, honesty
    {"topic": "What consistency looks like when no one is watching", "category": "Personal"},
    {"topic": "The habit I built that changed how I think about shipping", "category": "Personal"},
    {"topic": "Why I started documenting instead of performing on LinkedIn", "category": "Personal"},
    {"topic": "What I wish I knew before switching from engineering to product", "category": "Personal"},
    {"topic": "The day I stopped waiting to feel ready", "category": "Personal"},

    # Culture — India moments, OTT, pop references used as PM lenses
    {"topic": "What the Ramayana remake teaches us about audience expectations", "category": "Culture"},
    {"topic": "Lessons from how OTT platforms killed weekend plans", "category": "Culture"},
    {"topic": "What Netflix India gets right about product-market fit", "category": "Culture"},
    {"topic": "The Kota Factory effect — what Indian ambition actually looks like", "category": "Culture"},
]

# Category order for diversity rotation — ensures variety in what the brain sees
_CATEGORY_ORDER = [
    "Building",
    "Personal",
    "AI",
    "Culture",
    "Gaming",
    "India Tech",
    "Developer-to-PM",
    "Product Management",
]


def _get_custom_topics() -> list[dict[str, str]]:
    """Pull active custom topics from Supabase custom_topics table."""
    try:
        from db.supabase_client import get_supabase
        db = get_supabase()
        result = db.table("custom_topics").select("topic, category").eq("active", True).execute()
        return [
            {"topic": r["topic"], "category": r.get("category", "Personal")}
            for r in (result.data or [])
            if r.get("topic")
        ]
    except Exception as exc:
        logger.debug("[scraper] Custom topics fetch failed (non-fatal): %s", exc)
        return []


def _get_used_topic_slugs(n: int = 7) -> set[str]:
    """
    Return topic slugs used in the last n posts (posted + queued).
    A slug is the topic string lowercased and stripped.
    Topics in this set are blocked for the current selection.
    """
    try:
        from db.queries import get_last_n_posts, get_posts_by_status

        used: set[str] = set()

        # Last n posted
        for post in get_last_n_posts(n=n):
            sc = post.get("signal_card") or {}
            topic = sc.get("selected_topic", "")
            if topic:
                used.add(topic.lower().strip())
            slug = post.get("topic_slug", "")
            if slug:
                used.add(slug.lower().strip())

        # Currently queued (draft/approved — don't repeat these either)
        for post in get_posts_by_status(["draft", "approved", "scheduled", "pending_reschedule"]):
            sc = post.get("signal_card") or {}
            topic = sc.get("selected_topic", "")
            if topic:
                used.add(topic.lower().strip())

        return used
    except Exception as exc:
        logger.debug("[scraper] Used topic slugs fetch failed (non-fatal): %s", exc)
        return set()


def get_full_pool(used_topic_slugs: set[str] | None = None) -> list[dict[str, str]]:
    """
    Return the full topic pool — all 46 hardcoded + custom from Supabase.
    One topic per category, ordered by _CATEGORY_ORDER for diversity.
    Topics in used_topic_slugs are skipped.

    This replaces the old _rotate_pool(n=6) which only returned 6 topics
    per day, causing the brain to always pick from a PM-heavy subset.
    """
    if used_topic_slugs is None:
        used_topic_slugs = set()

    # Merge hardcoded pool + custom topics
    all_topics = _NICHE_POOL.copy()
    custom = _get_custom_topics()
    if custom:
        logger.info("[scraper] Adding %d custom topics from Supabase", len(custom))
        all_topics.extend(custom)

    # Group by category, filter out used topics
    by_category: dict[str, list[dict]] = {}
    for t in all_topics:
        cat = t.get("category", "Personal")
        if t["topic"].lower().strip() not in used_topic_slugs:
            by_category.setdefault(cat, []).append(t)

    # Pick one per category in diversity order
    # Use today's date as a secondary shuffle within each category
    # so the same topic doesn't always surface first within a category
    from datetime import date as _date
    import hashlib as _hashlib
    today_seed = int(_hashlib.md5(_date.today().isoformat().encode()).hexdigest(), 16)

    result: list[dict[str, str]] = []
    all_categories = list(_CATEGORY_ORDER)
    # Add any custom categories not in the order list
    for cat in by_category:
        if cat not in all_categories:
            all_categories.append(cat)

    for cat in all_categories:
        topics_in_cat = by_category.get(cat, [])
        if not topics_in_cat:
            continue
        # Pick one topic from this category using date seed
        idx = today_seed % len(topics_in_cat)
        result.append({**topics_in_cat[idx], "source": "niche_pool"})

    # Fall back: if we blocked too many and result is tiny, add some used topics back
    if len(result) < 3:
        logger.warning("[scraper] Pool nearly exhausted — relaxing used-topic filter")
        for cat in all_categories:
            all_in_cat = [t for t in all_topics if t.get("category") == cat]
            if all_in_cat and not any(r.get("category") == cat for r in result):
                idx = today_seed % len(all_in_cat)
                result.append({**all_in_cat[idx], "source": "niche_pool"})

    logger.info(
        "[scraper] Full pool: %d topics across %d categories (after filtering %d used)",
        len(result), len(by_category), len(used_topic_slugs),
    )
    return result


# ---------------------------------------------------------------------------
# HTTP attempt (best effort — usually blocked on Railway)
# ---------------------------------------------------------------------------

async def _try_http_topics() -> list[dict[str, str]]:
    """Attempt LinkedIn public endpoint. Returns [] on any failure."""
    topics: list[dict[str, str]] = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            resp = await client.get(
                "https://www.linkedin.com/news/api/stories?count=10",
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                for story in data.get("stories", [])[:6]:
                    title = story.get("title") or story.get("headline", "")
                    if title:
                        topics.append({"topic": title, "category": "LinkedIn Trending", "source": "http"})
    except Exception as exc:
        logger.debug("LinkedIn HTTP attempt failed (expected on Railway): %s", exc)
    return topics


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def collect_linkedin_topics() -> list[dict[str, str]]:
    """
    Return full topic list for content_signals caching.
    Never raises. Always returns at least the full niche pool.
    """
    # Get used topics to filter pool
    used_slugs = _get_used_topic_slugs(n=7)

    # Try HTTP (usually blocked, non-fatal)
    topics: list[dict[str, str]] = []
    http_topics = await _try_http_topics()
    if http_topics:
        logger.info("HTTP LinkedIn topics: %d", len(http_topics))
        topics.extend(http_topics)

    # Always add full pool
    pool = get_full_pool(used_topic_slugs=used_slugs)
    topics.extend(pool)

    logger.info(
        "Total topics available: %d (%d from HTTP, %d from pool)",
        len(topics), len(http_topics), len(pool),
    )
    return topics
