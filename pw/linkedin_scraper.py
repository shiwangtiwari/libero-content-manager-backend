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
_NICHE_POOL: list[dict[str, str]] = [
    # Product Management
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
    # Developer to PM
    {"topic": "Developer to PM Transition", "category": "Developer-to-PM"},
    {"topic": "Why Engineers Make Great PMs", "category": "Developer-to-PM"},
    {"topic": "Using Coding Skills in Product Management", "category": "Developer-to-PM"},
    {"topic": "From Engineer to Product Leader", "category": "Developer-to-PM"},
    {"topic": "Technical Debt and PM Decisions", "category": "Developer-to-PM"},
    {"topic": "Reading Technical Docs as a PM", "category": "Developer-to-PM"},
    # AI in PM
    {"topic": "AI Tools for Product Managers in 2025", "category": "AI in PM"},
    {"topic": "Using AI for User Research Synthesis", "category": "AI in PM"},
    {"topic": "ChatGPT Prompts for PMs", "category": "AI in PM"},
    {"topic": "AI-First Product Thinking", "category": "AI in PM"},
    {"topic": "LLMs in Product Workflows", "category": "AI in PM"},
    # India Tech
    {"topic": "PM Roles in Indian Startups", "category": "India Tech"},
    {"topic": "Breaking into Product Management in India", "category": "India Tech"},
    {"topic": "NextLeap PM Fellowship Experience", "category": "India Tech"},
    {"topic": "SaaS PM vs Consumer PM in India", "category": "India Tech"},
    # Personal Brand
    {"topic": "Building a Personal Brand as a PM", "category": "Personal Brand"},
    {"topic": "LinkedIn Content for Tech Professionals", "category": "Personal Brand"},
    {"topic": "Learning in Public as a Developer Turned PM", "category": "Personal Brand"},
    {"topic": "Portfolio for Aspiring PMs", "category": "Personal Brand"},
]


def _rotate_pool(n: int = 6) -> list[dict[str, str]]:
    """
    Return N topics from the pool, rotated deterministically by today's date.
    Different topics surface each day without any state.
    """
    today = date.today().isoformat()
    seed = int(hashlib.md5(today.encode()).hexdigest(), 16)
    pool = _NICHE_POOL.copy()
    for i in range(len(pool) - 1, 0, -1):
        j = seed % (i + 1)
        pool[i], pool[j] = pool[j], pool[i]
        seed = (seed >> 1) or 1
    return pool[:n]


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
