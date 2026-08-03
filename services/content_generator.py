"""
services/content_generator.py
------------------------------
Generates LinkedIn post content using the Anthropic API directly.
Model: claude-sonnet-4-5

Phase 6 upgrades:
- linkedin-voice skill rules embedded in prompt (post craft, formats, line-level rules)
- About Me profile injected from user_profile table on every generation
- NextLeap removed from active "reference this" voice instruction
  (it stays in the system's understanding of who Shiwang is, not as a push to mention it)
- Hashtag rules explicit: 3 max, topic-relevant, never #NextLeap as default
- NextLeap removed from viral score named_things regex (no scoring bonus for mentioning it)
- _fetch_trending_context pulls from 5 sources in parallel (Reddit + Google Trends + Verge)
- build_post_prompt accepts used_angles for angle diversity tracking
"""

import asyncio
import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 1500
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

CONTENT_ANGLES = [
    "personal_story",
    "contrarian_take",
    "framework_breakdown",
    "cultural_bridge",
    "number_list",
    "hot_take",
]


# ── Trending context fetcher ──────────────────────────────────────────────────

async def _fetch_source(client: httpx.AsyncClient, url: str, label: str, parser_fn) -> list[str]:
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LiberoBot/1.0)"},
            follow_redirects=True,
            timeout=8,
        )
        if resp.status_code == 200:
            results = parser_fn(resp.text)
            logger.debug("[trending] %s: %d items", label, len(results))
            return results
    except Exception as exc:
        logger.debug("[trending] %s failed (non-fatal): %s", label, exc)
    return []


def _parse_google_trends(xml: str) -> list[str]:
    titles = re.findall(r"<title><!\[CDATA\[(.+?)\]\]></title>", xml)
    return [t.strip() for t in titles[1:7]]


def _parse_reddit_hot(json_text: str, max_items: int = 5) -> list[str]:
    import json as _json
    try:
        data = _json.loads(json_text)
        posts = data.get("data", {}).get("children", [])
        results = []
        for post in posts[:max_items]:
            pd = post.get("data", {})
            title = pd.get("title", "").strip()
            score = pd.get("score", 0)
            if title and score > 50:
                results.append(title)
        return results
    except Exception:
        return []


def _parse_verge_rss(xml: str) -> list[str]:
    titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.+?)(?:\]\]>)?</title>", xml)
    clean = [t.strip() for t in titles if t.strip() and len(t.strip()) > 20]
    return clean[1:6]


async def _fetch_trending_context(topic: str) -> str:
    """Pull live cultural + tech context from 5 sources in parallel."""
    reddit_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; LiberoContentBot/1.0)",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=8, follow_redirects=True, headers=reddit_headers) as client:
        results = await asyncio.gather(
            _fetch_source(client, "https://trends.google.com/trending/rss?geo=IN",
                          "Google Trends IN", _parse_google_trends),
            _fetch_source(client, "https://www.reddit.com/r/india/hot.json?limit=8",
                          "r/india", lambda t: _parse_reddit_hot(t, 4)),
            _fetch_source(client, "https://www.reddit.com/r/bollywood/hot.json?limit=8",
                          "r/bollywood", lambda t: _parse_reddit_hot(t, 3)),
            _fetch_source(client, "https://www.reddit.com/r/IndianGaming/hot.json?limit=6",
                          "r/IndianGaming", lambda t: _parse_reddit_hot(t, 3)),
            _fetch_source(client, "https://www.theverge.com/rss/index.xml",
                          "The Verge", _parse_verge_rss),
            return_exceptions=True,
        )

    google_trends, r_india, r_bollywood, r_gaming, tech_news = [
        r if isinstance(r, list) else [] for r in results
    ]

    sections = []
    if google_trends:
        sections.append("Search spikes in India: " + " | ".join(google_trends[:5]))
    if r_india:
        sections.append("What India is talking about:\n" + "\n".join(f"  • {t}" for t in r_india))
    if r_bollywood:
        sections.append("Bollywood/OTT buzz:\n" + "\n".join(f"  • {t}" for t in r_bollywood))
    if r_gaming:
        sections.append("Gaming/pop culture:\n" + "\n".join(f"  • {t}" for t in r_gaming))
    if tech_news:
        sections.append("Global tech news:\n" + "\n".join(f"  • {t}" for t in tech_news[:4]))

    return "\n\n".join(sections) if sections else ""


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_post_prompt(
    topic: str,
    last_topics: str = "",
    signal_card: str = "",
    trending_context: str = "",
    used_angles: list[str] | None = None,
    user_profile_context: str = "",
) -> str:
    used_angles = used_angles or []

    # ── Profile context block ─────────────────────────────────────────────
    profile_block = f"""
{user_profile_context}

Use this context to write in Shiwang's authentic voice. Do not quote or list these facts directly — let them inform the perspective, the examples chosen, and the way he frames things.
""" if user_profile_context else ""

    # ── Trending block ────────────────────────────────────────────────────
    if trending_context:
        trending_block = f"""
LIVE CULTURAL + TECH CONTEXT
Use ONE item below as a hook or analogy IF it connects genuinely to the topic.
Name the specific thing — not "a recent movie" but "the Ramayana trailer".
Only use if the connection earns its place. A forced reference is worse than none.

{trending_context}

Examples of references that work:
  "GTA 6 broke pre-order records before anyone played it. That's a trust asset built over 10 years. Your personal brand works the same way."
  "The Ramayana trailer dropped and everyone's debating casting. That's a product launch: perception is the real ship date."
  "The revocation meme is everywhere. In product, every feature you ship is a revocation risk."
"""
    else:
        trending_block = ""

    # ── Angle diversity block ─────────────────────────────────────────────
    if used_angles:
        angle_names = {
            "personal_story": "Personal story (I was in a meeting when...)",
            "contrarian_take": "Contrarian take (Most PMs are wrong about X)",
            "framework_breakdown": "Framework breakdown (step-by-step concept)",
            "cultural_bridge": "Cultural bridge (movie/show/meme as hook)",
            "number_list": "Number list (3 things I learned...)",
            "hot_take": "Hot take (bold one-liner, no softening)",
        }
        blocked = [angle_names.get(a, a) for a in used_angles if a in angle_names]
        available = [a for a in CONTENT_ANGLES if a not in used_angles]

        angle_block = f"\nANGLE DIVERSITY — your last {len(blocked)} post(s) used:\n"
        angle_block += "\n".join(f"  AVOID: {b}" for b in blocked)

        if available:
            forced = available[0]
            instructions = {
                "personal_story": "Open with a specific moment: 'Last week I was in a sprint review when...'",
                "contrarian_take": "Open with a claim most people in your niche would push back on.",
                "framework_breakdown": "Teach one PM concept with a clear before/after or 3-step structure.",
                "cultural_bridge": "Open with a specific trending reference, then connect it to a PM insight.",
                "number_list": "'3 things', '5 mistakes' — be specific, not generic.",
                "hot_take": "One bold sentence that takes a real position, then back it up.",
            }
            angle_block += f"\nUSE THIS ANGLE: {instructions.get(forced, '')}"
    else:
        angle_block = ""

    return f"""You are writing a LinkedIn post for Shiwang — a software developer transitioning into Product Management. Indian audience, tech-savvy, ages 22-35.
{profile_block}
TOPIC: {topic}
SIGNAL: {signal_card if signal_card else "Content cycle"}

RECENT POSTS — do not repeat these topics or angles:
{last_topics if last_topics else "None — this is a fresh start"}
{angle_block}
{trending_block}
LINKEDIN VOICE RULES (follow these precisely):

1. HOOK (line 1 only) — the only line mobile users see before tapping "see more":
   - Specific tension, number, or contrarian claim
   - Never "I'm excited to share" or "I've been thinking about"
   - Formats that work: "Most [X] advice is wrong.", "I reviewed 47 [X] this week.", "Nobody talks about this.", "[Trending thing] teaches us [PM insight]."

2. BODY — lesson-from-a-real-decision format works best:
   - Situation → what Shiwang chose → what happened → one principle
   - OR: honest-mistake post (highest trust, use sparingly)
   - OR: teardown/observation of a product, tool, or India tech market moment
   - 3-5 paragraphs, max 2 sentences each, white space between every paragraph
   - One concrete story with real details — never "a PM I know"
   - One insight the reader can act on this week
   - One unexpected angle or contradiction

3. CLOSE — genuine question OR clean stop:
   - Good: "What's the hardest no you've said as a PM?"
   - Never: "What do you think? Drop a comment below!"
   - Never: "Agree?" as the sole CTA

VOICE — Shiwang's actual style:
- First person, direct, no hedging
- "I had 3 conflicting feature requests with zero data" not "PMs face prioritisation challenges"
- Sharp but conversational — like a smart friend, not a consultant
- Indian context welcome: reference Indian startups, India tech ecosystem, Indian PM job market
- Reference his own building experience when relevant (he builds real things)
- No corporate speak, no "game-changer", no "leverage synergies"

FORMAT RULES:
- 180-240 words total
- Line breaks between every paragraph
- No bold, no bullet points, no em dashes
- No "I'm excited to share", "Great question", "In conclusion"
- No fabricated statistics

HASHTAGS (at the very end, on their own line):
- Maximum 3 hashtags
- Must be directly relevant to THIS post's specific topic
- Examples of good hashtags: #ProductManagement #DevToPM #IndiaStartups #AIinProduct #PersonalBranding
- Never use #NextLeap unless this specific post is explicitly about that learning chapter
- Never use generic dumps like #PM #Career #Life #Growth

VIRAL SCORE TARGETS — every post must hit all 5:
1. Hook stops scroll in 2 seconds
2. Personal story with real details
3. One concrete insight the reader can act on this week
4. Ends with a genuine question or clean stop
5. Niche-relevant to PM, developer-to-PM, India tech, or AI in product

Generate the post only. No preamble. No "Here's a post:". No explanation after."""


# ── API call ──────────────────────────────────────────────────────────────────

async def generate_post(
    topic: str,
    last_topics: str = "",
    signal_card: str = "",
    used_angles: list[str] | None = None,
) -> str:
    """
    Call Anthropic API and return the generated LinkedIn post text.
    Pulls About Me profile from Supabase and injects into prompt on every call.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY is not set in Railway Variables.")

    # Fetch user profile context (non-blocking — empty string if table doesn't exist yet)
    user_profile_context = ""
    try:
        from db.queries import get_profile_as_context
        user_profile_context = get_profile_as_context()
    except Exception as e:
        logger.debug("[content_generator] Profile fetch failed (non-fatal): %s", e)

    # Fetch trending context in parallel with nothing else
    trending_context = await _fetch_trending_context(topic)
    if trending_context:
        logger.info("[content_generator] Trending context fetched (%d chars)", len(trending_context))

    prompt = build_post_prompt(
        topic=topic,
        last_topics=last_topics,
        signal_card=signal_card,
        trending_context=trending_context,
        used_angles=used_angles or [],
        user_profile_context=user_profile_context,
    )

    logger.info("[content_generator] Generating post — topic: %s", topic[:60])

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
                "messages": [{"role": "user", "content": prompt}],
            },
        )

    if response.status_code == 401:
        raise RuntimeError(
            "Anthropic API 401 — API key invalid or expired. "
            "Go to Railway Variables → ANTHROPIC_API_KEY → re-paste fresh key."
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Anthropic API returned {response.status_code}: {response.text[:300]}"
        )

    data = response.json()
    try:
        content = data["content"][0]["text"].strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected Anthropic API response: {e}. Raw: {str(data)[:300]}")

    usage = data.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cost_usd = (input_tokens * 3.0 + output_tokens * 15.0) / 1_000_000
    logger.info(
        "[content_generator] Done — %d chars, input=%d output=%d cost=$%.5f",
        len(content), input_tokens, output_tokens, cost_usd,
    )

    return content


# ── Angle detector ────────────────────────────────────────────────────────────

def detect_angle(post_text: str) -> str:
    if not post_text:
        return "personal_story"
    lines = [l.strip() for l in post_text.strip().split("\n") if l.strip()]
    first = lines[0].lower() if lines else ""
    full = post_text.lower()

    if re.match(r"^\d+\s+", first) or re.search(r"\b\d+\s+(things|mistakes|lessons|reasons|tips|ways)\b", first):
        return "number_list"
    cultural_signals = [
        r"\btrailer\b", r"\bseason\b", r"\bepisode\b", r"\bmovie\b", r"\bgame\b",
        r"\bgta\b", r"\bspider.?man\b", r"\bramayana\b", r"\bsplit villa\b",
        r"\bbigg boss\b", r"\bnetflix\b", r"\bprime\b", r"\bmeme\b", r"\btrending\b",
    ]
    if any(re.search(p, first) for p in cultural_signals):
        return "cultural_bridge"
    if len(first.split()) <= 8 and not first.endswith("?"):
        if any(re.search(p, first) for p in [r"\bwrong\b", r"\bstop\b", r"\bnobody\b",
                                               r"\bmost\b", r"\bunpopular\b", r"\boverrated\b"]):
            return "hot_take"
    if any(re.search(p, full[:200]) for p in [r"\bwrong\b", r"\bmyth\b", r"\bactually\b",
                                                r"\bcontrary\b", r"\boverrated\b"]):
        return "contrarian_take"
    if any(re.search(p, full[:400]) for p in [r"\bstep\s+\d\b", r"\bframework\b",
                                                r"\bmodel\b", r"\bprocess\b", r"\bhow to\b",
                                                r"\bhere.?s how\b", r"\bformula\b"]):
        return "framework_breakdown"
    return "personal_story"


# ── Viral score ───────────────────────────────────────────────────────────────

def compute_viral_score(post_text: str) -> int:
    score = 0
    lines = [l for l in post_text.strip().split("\n") if l.strip()]
    first_line = lines[0].strip() if lines else ""
    content_lines = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    cta_line = content_lines[-1] if content_lines else ""

    # 1. Hook strength (0-20)
    if first_line.endswith("?"):
        score += 18
    elif re.search(r"\d+", first_line):
        score += 17
    elif re.search(r"^(nobody|most|stop|why|the truth|unpopular|here'?s)", first_line, re.I):
        score += 16
    elif len(first_line) < 70 and first_line:
        score += 12
    else:
        score += 6

    # 2. Personal story (0-20)
    personal_hits = len(re.findall(
        r"\b(I|my|me|I've|I'm|I was|I learned|I realised|I noticed)\b", post_text, re.I
    ))
    specific_story = bool(re.search(
        r"\b(when|last week|yesterday|this week|3 months|6 months|\d+ days)\b", post_text, re.I
    ))
    score += min(20, personal_hits * 3 + (5 if specific_story else 0))

    # 3. Specificity (0-20) — NextLeap removed from named_things bonus
    numbers = len(re.findall(r"\d+", post_text))
    named_things = len(re.findall(
        r"\b(LinkedIn|Slack|Notion|Figma|PM|API|sprint|OKR|PRD|Jira|"
        r"Supabase|Railway|Claude|ChatGPT|Gemini|India|Mumbai|Bangalore|startup)\b",
        post_text, re.I
    ))
    score += min(20, numbers * 2 + named_things * 2)

    # 4. CTA quality (0-20)
    if cta_line.endswith("?"):
        if re.search(r"\b(what'?s|how|when|which|have you|do you|what would)\b", cta_line, re.I):
            score += 20
        else:
            score += 14
    elif re.search(r"(comment|share|tell me|drop|thoughts|agree|disagree)", cta_line, re.I):
        score += 8
    else:
        score += 2

    # 5. Niche relevance (0-20)
    niche_hits = len(re.findall(
        r"\b(product\s*manag|pm\b|developer|engineer|transition|career|"
        r"fellowship|ai|startup|india|tech|prioriti|roadmap|user\s*research|okr|sprint)\b",
        post_text, re.I,
    ))
    score += min(20, niche_hits * 3)

    return min(100, score)


async def validate_api_key() -> dict:
    result: dict[str, Any] = {"is_healthy": False, "error": None}
    try:
        text = await generate_post(
            topic="test",
            last_topics="",
            signal_card="API key validation — respond with: OK",
        )
        result["is_healthy"] = bool(text)
    except Exception as e:
        result["error"] = str(e)
    return result
