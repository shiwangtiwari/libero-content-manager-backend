"""
services/content_generator.py
------------------------------
Generates LinkedIn post content using the Anthropic API directly.
Replaces the Playwright → claude.ai approach (blocked by Cloudflare).

Model: claude-sonnet-4-5
Cost: ~$0.005 per post, $5 free credits lasts ~8 years at 3 posts/week.

Called by:
  - /test/claude endpoint (Phase 2 validation)
  - Content generation scheduler job (Phase 3)
  - Manual trigger via Telegram /generate command (future)
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 1024
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


# ── Prompt builder (from master doc Section 11.3) ─────────────────────────────

def build_post_prompt(
    topic: str,
    last_topics: str = "",
    signal_card: str = "",
) -> str:
    return f"""You are writing a LinkedIn post for Shiwang, a developer transitioning into Product Management through the NextLeap fellowship.

Voice: Direct, practical, first-person. Conversational but credible. No corporate speak. No generic advice. Real experience, real perspective.

Topic: {topic}

Trigger signal: {signal_card if signal_card else "General content cycle"}

Recent post topics to avoid repeating:
{last_topics if last_topics else "None provided"}

Structure requirements:
- Hook: First line must stop the scroll. Question, bold claim, or surprising fact.
- Body: 3-5 short paragraphs or 5-8 punchy lines. No walls of text.
- CTA: End with a question that invites comments.
- Length: 150-250 words optimal.
- Max 3 relevant hashtags at the end. No hashtag dumps.

Viral scoring targets — must hit 85+ across these:
- Strong hook (0-10)
- Personal story or clear POV (0-10)
- Specific and concrete, not vague (0-10)
- Invites engagement (0-10)
- Niche relevance (0-10)

Generate the post only. No preamble. No explanation."""


# ── API call ──────────────────────────────────────────────────────────────────

async def generate_post(
    topic: str,
    last_topics: str = "",
    signal_card: str = "",
) -> str:
    """
    Call Anthropic API and return the generated LinkedIn post text.

    Raises:
        EnvironmentError  — ANTHROPIC_API_KEY not set
        RuntimeError      — API call failed
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY is not set in Railway Variables. "
            "Get your key from platform.anthropic.com → API Keys."
        )

    prompt = build_post_prompt(topic, last_topics, signal_card)

    logger.info(f"[content_generator] Calling Anthropic API — topic: {topic[:50]}")

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": MAX_TOKENS,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
            },
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Anthropic API returned {response.status_code}: {response.text[:300]}"
        )

    data = response.json()

    try:
        content = data["content"][0]["text"].strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError(
            f"Unexpected Anthropic API response structure: {e}. "
            f"Response: {str(data)[:300]}"
        )

    # Log usage for cost tracking
    usage = data.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cost_usd = (input_tokens * 3.0 + output_tokens * 15.0) / 1_000_000
    logger.info(
        f"[content_generator] Done — "
        f"input={input_tokens} output={output_tokens} "
        f"cost=${cost_usd:.5f}"
    )

    return content


async def validate_api_key() -> dict:
    """
    Quick check that the API key works.
    Used by GET /health/sessions.
    """
    result = {"is_healthy": False, "error": None}
    try:
        text = await generate_post(
            topic="test",
            last_topics="",
            signal_card="API key validation test — respond with: OK",
        )
        result["is_healthy"] = bool(text)
    except Exception as e:
        result["error"] = str(e)
    return result
