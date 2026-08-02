"""
LinkedIn trending scraper — Phase 3 component.
Scrapes LinkedIn explore/trending for PM + tech + dev-to-PM topics.
Does NOT require a separate LinkedIn session cookie — uses the OAuth2 access token
to browse the LinkedIn API, or falls back to public feed scraping.
Phase 3 — not active until Phase 3 deployment.
"""
import logging
import httpx
from config import settings

logger = logging.getLogger(__name__)

# Target niche keywords for filtering
NICHE_KEYWORDS = [
    "product management", "product manager", "pm",
    "developer to pm", "dev to pm", "technical pm",
    "nextleap", "pm fellowship",
    "ai product", "ai in product",
    "india startup", "india tech",
    "personal brand", "linkedin growth",
    "product thinking", "product strategy",
]


async def get_trending_topics(limit: int = 10) -> list[dict]:
    """
    Fetch trending topics relevant to Shiwang's niche.
    Phase 3 implementation — returns empty list in Phase 1.
    Returns list of {"topic": str, "source": "linkedin_trending", "raw_data": dict}
    """
    logger.info("[Scraper] LinkedIn scraping is a Phase 3 feature. Returning empty list.")
    return []


def is_niche_relevant(topic: str) -> bool:
    """Check if a topic matches Shiwang's niche filter."""
    topic_lower = topic.lower()
    return any(kw in topic_lower for kw in NICHE_KEYWORDS)
