"""
pw/linkedin_scraper.py — LinkedIn trending topic collector.

Reality: LinkedIn blocks Railway datacenter IPs for scraping.
Strategy: Multi-layer fallback that always returns something useful.

Layer 1: LinkedIn public HTTP (best effort, usually blocked)
Layer 2: Niche topic pool rotated by date (always works, deterministic)

The result feeds into content_signals table via signal_collector.py.
Table used: content_signals (source = 'linkedin_trending')
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Niche topic pool — rotated deterministically by date
# These are always-valid PM/tech/developer topics for Shiwang's niche
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Topic pool — diverse, reflects Shiwang's actual life and interests.
# Intentionally NOT all PM topics. Mix of PM, building, gaming, culture,
# India tech, AI, and personal observations.
#
# Rule: no two consecutive pool-selected posts should be from the same category.
# The _rotate_pool_diverse() function enforces category alternation.
# ---------------------------------------------------------------------------
_NICHE_POOL: list[dict[str, str]] = [
    # Product Management — core craft
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
    # Developer to PM — transition story
    {"topic": "Developer to PM Transition", "category": "Developer-to-PM"},
    {"topic": "Why Engineers Make Great PMs", "category": "Developer-to-PM"},
    {"topic": "Using Coding Skills in Product Management", "category": "Developer-to-PM"},
    {"topic": "From Engineer to Product Leader", "category": "Developer-to-PM"},
    {"topic": "Technical Debt and PM Decisions", "category": "Developer-to-PM"},
    {"topic": "Reading Technical Docs as a PM", "category": "Developer-to-PM"},
    # Building things — autonomous systems, side projects
    {"topic": "Building things that work when you are not watching", "category": "Building"},
    {"topic": "What I learned building an autonomous content system", "category": "Building"},
    {"topic": "Why I automated my LinkedIn presence", "category": "Building"},
    {"topic": "The difference between a project and a product", "category": "Building"},
    {"topic": "When to build vs when to buy", "category": "Building"},
    {"topic": "Shipping something real vs shipping something perfect", "category": "Building"},
    # AI — tools, observations, real usage
    {"topic": "AI Tools for Product Managers in 2025", "category": "AI"},
    {"topic": "How I actually use Claude and ChatGPT in my workflow", "category": "AI"},
    {"topic": "The gap between AI hype and AI usefulness", "category": "AI"},
    {"topic": "AI-First Product Thinking", "category": "AI"},
    {"topic": "What Copilot taught me about how developers really work", "category": "AI"},
    {"topic": "When AI makes you faster and when it makes you lazy", "category": "AI"},
    # India tech — startup ecosystem, job market
    {"topic": "PM Roles in Indian Startups", "category": "India Tech"},
    {"topic": "Breaking into Product Management in India", "category": "India Tech"},
    {"topic": "What India's startup ecosystem looks like from the inside", "category": "India Tech"},
    {"topic": "SaaS PM vs Consumer PM in India", "category": "India Tech"},
    {"topic": "Why Indian developers are underrated globally", "category": "India Tech"},
    # Gaming — GTA 6, pre-orders, launch psychology
    {"topic": "GTA 6 pre-order psychology and what it teaches product managers", "category": "Gaming"},
    {"topic": "Why gaming communities are the best product feedback loops", "category": "Gaming"},
    {"topic": "What game design gets right that most apps get wrong", "category": "Gaming"},
    {"topic": "The product lessons inside open-world games", "category": "Gaming"},
    # Personal observations — life, discipline, how I see things
    {"topic": "What consistency looks like when no one is watching", "category": "Personal"},
    {"topic": "The habit I built that changed how I think about shipping", "category": "Personal"},
    {"topic": "Why I started documenting instead of performing on LinkedIn", "category": "Personal"},
    {"topic": "What I wish I knew before switching from engineering to product", "category": "Personal"},
    {"topic": "The day I stopped waiting to feel ready", "category": "Personal"},
    # Culture and pop references — Bollywood, OTT, trending India moments
    {"topic": "What the Ramayana remake teaches us about audience expectations", "category": "Culture"},
    {"topic": "Lessons from how OTT platforms killed weekend plans", "category": "Culture"},
    {"topic": "What Netflix India gets right about product-market fit", "category": "Culture"},
    {"topic": "The Kota Factory effect — what Indian ambition actually looks like", "category": "Culture"},
]


def _rotate_pool(n: int = 6) -> list[dict[str, str]]:
    """
    Return N topics from the pool with two guarantees:
    1. Topics rotate by date — different set surfaces each day, no state needed.
    2. No two consecutive topics are from the same category.
       This prevents "Building a Personal Brand as PM" followed by
       "Developer to PM Transition" (both feel like the same post).
    """
    today = date.today().isoformat()
    seed = int(hashlib.md5(today.encode()).hexdigest(), 16)
    pool = _NICHE_POOL.copy()
    # Shuffle deterministically by date
    for i in range(len(pool) - 1, 0, -1):
        j = seed % (i + 1)
        pool[i], pool[j] = pool[j], pool[i]
        seed = (seed >> 1) or 1

    # Pick n topics ensuring no two consecutive are same category
    result = []
    used_indices = set()
    last_category = None
    attempts = 0
    while len(result) < n and attempts < len(pool) * 2:
        attempts += 1
        for i, topic in enumerate(pool):
            if i in used_indices:
                continue
            if topic["category"] != last_category:
                result.append(topic)
                used_indices.add(i)
                last_category = topic["category"]
                break
        else:
            # All remaining topics are same category — just take the next unused one
            for i, topic in enumerate(pool):
                if i not in used_indices:
                    result.append(topic)
                    used_indices.add(i)
                    last_category = topic["category"]
                    break

    return result[:n]


# ---------------------------------------------------------------------------
# HTTP attempt (best effort)
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
    Return a list of trending topic dicts:
      [{"topic": "...", "category": "...", "source": "..."}, ...]

    Never raises. Always returns at least the rotated niche pool.
    Each dict is safe to pass directly to queries.create_signal().
    """
    topics: list[dict[str, str]] = []

    # Try HTTP
    http_topics = await _try_http_topics()
    if http_topics:
        logger.info("HTTP LinkedIn topics: %d", len(http_topics))
        topics.extend(http_topics)

    # Always add niche pool to guarantee variety
    pool = _rotate_pool(n=6)
    for t in pool:
        t["source"] = "niche_pool"
    topics.extend(pool)

    logger.info("Total topics available: %d (%d from HTTP, %d from pool)",
                len(topics), len(http_topics), len(pool))
    return topics
