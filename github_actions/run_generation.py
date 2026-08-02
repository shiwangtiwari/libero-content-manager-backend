"""
github_actions/run_generation.py
---------------------------------
Runs inside GitHub Actions (not Railway).
GitHub's IPs are not blocked by Cloudflare.

Flow:
1. Read env vars (topic, post_id, cookies, callback URL)
2. Open claude.ai with Playwright using session cookies
3. Submit the LinkedIn post generation prompt
4. Extract the response
5. POST the result back to Railway via /internal/generation-complete

This script is self-contained — no imports from the rest of the project.
It only needs: playwright, httpx (both installed in the workflow).
"""

import asyncio
import json
import logging
import os
import sys

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Read environment ──────────────────────────────────────────────────────────

CLAUDE_COOKIES_JSON  = os.environ.get("CLAUDE_COOKIES", "")
RAILWAY_CALLBACK_URL = os.environ.get("RAILWAY_CALLBACK_URL", "")
INTERNAL_SECRET      = os.environ.get("RAILWAY_INTERNAL_SECRET", "")
TOPIC                = os.environ.get("TOPIC", "test")
POST_ID              = os.environ.get("POST_ID", "test")
SIGNAL_CARD          = os.environ.get("SIGNAL_CARD", "{}")
LAST_TOPICS          = os.environ.get("LAST_TOPICS", "")

VALID_SAME_SITE = {"Strict", "Lax", "None"}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── Prompt template (from master doc Section 11.3) ────────────────────────────

def build_prompt(topic: str, last_topics: str, signal_card: str) -> str:
    return f"""You are writing a LinkedIn post for Shiwang, a developer transitioning into Product Management through the NextLeap fellowship.

Voice: Direct, practical, first-person. Conversational but credible. No corporate speak. No generic advice. Real experience, real perspective.

Topic: {topic}

Trigger signal: {signal_card}

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


# ── Playwright: open claude.ai and get response ───────────────────────────────

async def generate_with_playwright(prompt: str) -> str:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    if not CLAUDE_COOKIES_JSON:
        raise ValueError("CLAUDE_COOKIES secret is not set in GitHub repository secrets.")

    cookies = json.loads(CLAUDE_COOKIES_JSON)

    # Sanitise sameSite
    for cookie in cookies:
        if cookie.get("sameSite") not in VALID_SAME_SITE:
            cookie["sameSite"] = "Lax"

    logger.info(f"Loaded {len(cookies)} cookies for claude.ai")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )

        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        await context.add_cookies(cookies)

        page = await context.new_page()

        # Navigate
        logger.info("Navigating to claude.ai/new ...")
        try:
            await page.goto(
                "https://claude.ai/new",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except PWTimeout:
            raise RuntimeError("claude.ai/new did not load within 60s.")

        # Check we're not on login page
        url = page.url
        logger.info(f"Landed on: {url}")
        if any(kw in url for kw in ["login", "upgrade", "onboard", "sign"]):
            raise RuntimeError(
                f"Redirected to {url} — CLAUDE_COOKIES are expired. "
                "Re-export from browser and update GitHub secret."
            )

        # Wait for React to render
        await asyncio.sleep(4)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass

        # Find input box
        input_selectors = [
            'div[contenteditable="true"]',
            '.ProseMirror',
            '[data-testid="composer-input"]',
            'div[role="textbox"]',
        ]

        input_box = None
        for selector in input_selectors:
            try:
                await page.wait_for_selector(selector, timeout=5_000)
                input_box = page.locator(selector).first
                logger.info(f"Found input box: {selector}")
                break
            except PWTimeout:
                continue

        if input_box is None:
            # Log page state for debugging
            title = await page.title()
            body = (await page.inner_text("body"))[:500]
            logger.error(f"Page title: {title}")
            logger.error(f"Page body: {body}")
            raise RuntimeError(
                "Could not find input box. "
                f"Page title: {title}"
            )

        # Type and submit
        await input_box.click()
        await asyncio.sleep(0.3)
        await input_box.fill(prompt)
        await asyncio.sleep(0.8)

        logger.info("Submitting prompt...")
        await page.keyboard.press("Enter")

        # Wait for response to start
        try:
            await page.wait_for_selector('button[aria-label="Stop"]', timeout=20_000)
            logger.info("Claude is responding...")
        except PWTimeout:
            logger.warning("Stop button never appeared — may be instant response")

        # Wait for response to finish
        try:
            await page.wait_for_selector(
                'button[aria-label="Stop"]',
                state="hidden",
                timeout=120_000,
            )
            logger.info("Response complete.")
        except PWTimeout:
            raise RuntimeError("Claude response did not finish within 120s.")

        await asyncio.sleep(1.0)

        # Extract response
        for selector in [
            ".font-claude-message",
            '[data-testid="assistant-message"]',
            '[data-message-author-role="assistant"]',
        ]:
            messages = await page.locator(selector).all()
            if messages:
                text = await messages[-1].inner_text()
                if text and text.strip():
                    logger.info(f"Extracted {len(text)} chars via {selector}")
                    await browser.close()
                    return text.strip()

        await browser.close()
        raise RuntimeError("Could not extract response from claude.ai page.")


# ── Send result back to Railway ───────────────────────────────────────────────

async def send_result_to_railway(
    post_id: str,
    success: bool,
    content: str = "",
    error: str = "",
):
    if not RAILWAY_CALLBACK_URL:
        logger.warning("RAILWAY_CALLBACK_URL not set — skipping callback.")
        return

    payload = {
        "post_id": post_id,
        "success": success,
        "content": content,
        "error": error,
        "secret": INTERNAL_SECRET,
    }

    logger.info(f"Sending result to Railway: {RAILWAY_CALLBACK_URL}")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{RAILWAY_CALLBACK_URL}/internal/generation-complete",
            json=payload,
        )
        logger.info(f"Railway callback response: {resp.status_code}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    logger.info(f"Starting generation — topic: {TOPIC}, post_id: {POST_ID}")

    if TOPIC == "test" and POST_ID == "test":
        # Phase 2 test mode — simple validation prompt
        prompt = (
            "Please respond with exactly this sentence, nothing more: "
            "'Libero Phase 2 validation successful via GitHub Actions.'"
        )
    else:
        prompt = build_prompt(TOPIC, LAST_TOPICS, SIGNAL_CARD)

    try:
        content = await generate_with_playwright(prompt)
        logger.info("Generation successful.")
        logger.info(f"Content preview: {content[:100]}...")

        await send_result_to_railway(
            post_id=POST_ID,
            success=True,
            content=content,
        )
        sys.exit(0)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Generation failed: {error_msg}")

        await send_result_to_railway(
            post_id=POST_ID,
            success=False,
            error=error_msg,
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
