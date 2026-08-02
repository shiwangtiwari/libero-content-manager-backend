"""
pw/claude_generate.py
---------------------
Opens claude.ai with session cookies, submits a prompt, waits for the
response to finish streaming, and returns the assistant text.

Phase 2: called by /test/claude to validate Railway → Claude.ai connectivity.
Phase 3: called by the content generation job with the full LinkedIn post prompt.
"""

import asyncio
import logging

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .session_loader import get_browser_context

logger = logging.getLogger(__name__)

# Timeouts (milliseconds)
NAV_TIMEOUT          = 45_000
INPUT_WAIT_TIMEOUT   = 5_000   # per selector attempt
RESPONSE_START_MS    = 20_000
RESPONSE_FINISH_MS   = 120_000


async def generate_linkedin_post(prompt: str) -> str:
    """
    Submit a prompt to claude.ai and return the assistant's response as plain text.
    """
    logger.info("[claude_generate] Starting — opening claude.ai")

    pw, browser, context = await get_browser_context("claude")
    try:
        page = await context.new_page()

        # ── 1. Navigate ───────────────────────────────────────────────────────
        try:
            await page.goto(
                "https://claude.ai/new",
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT,
            )
        except PlaywrightTimeoutError:
            raise RuntimeError(
                "claude.ai/new did not load within 45s. "
                "Session may be expired — re-export CLAUDE_COOKIES."
            )

        # ── 2. Confirm we landed on the chat UI ───────────────────────────────
        current_url = page.url
        if any(kw in current_url for kw in ["login", "upgrade", "onboard", "sign"]):
            raise RuntimeError(
                f"Redirected to {current_url} — session cookies are expired or invalid. "
                "Re-export CLAUDE_COOKIES from claude.ai and update Railway Variables."
            )

        await asyncio.sleep(1.5)

        # ── 3. Find input box — try multiple selectors ────────────────────────
        input_selectors = [
            'div[contenteditable="true"]',
            'div[contenteditable="true"][data-placeholder]',
            '.ProseMirror',
            '[data-testid="composer-input"]',
            'div[role="textbox"]',
        ]

        input_box = None
        for selector in input_selectors:
            try:
                await page.wait_for_selector(selector, timeout=INPUT_WAIT_TIMEOUT)
                input_box = page.locator(selector).first
                logger.info(f"[claude_generate] Found input with selector: {selector}")
                break
            except PlaywrightTimeoutError:
                continue

        if input_box is None:
            await page.screenshot(path="/tmp/claude_no_input.png")
            # Dump all contenteditable and textbox elements to logs for debugging
            all_editable = await page.evaluate("""() => {
                const els = document.querySelectorAll('[contenteditable], [role="textbox"], textarea, .ProseMirror');
                return Array.from(els).map(el => ({
                    tag: el.tagName,
                    role: el.getAttribute('role'),
                    contenteditable: el.getAttribute('contenteditable'),
                    class: el.className.substring(0, 80),
                    dataTestId: el.getAttribute('data-testid'),
                    placeholder: el.getAttribute('placeholder') || el.getAttribute('data-placeholder'),
                }));
            }""")
            logger.error(f"[claude_generate] Editable elements found: {all_editable}")
            page_url = page.url
            logger.error(f"[claude_generate] Current URL: {page_url}")
            raise RuntimeError(
                "Could not find Claude.ai input box. "
                "Check Railway logs for 'Editable elements found' to see actual DOM selectors."
            )

        # ── 4. Fill and submit ────────────────────────────────────────────────
        await input_box.click()
        await asyncio.sleep(0.3)
        await input_box.fill(prompt)
        await asyncio.sleep(0.8)

        logger.info("[claude_generate] Submitting prompt")
        await page.keyboard.press("Enter")

        # ── 5. Wait for response to start ─────────────────────────────────────
        try:
            await page.wait_for_selector(
                'button[aria-label="Stop"]',
                timeout=RESPONSE_START_MS,
            )
        except PlaywrightTimeoutError:
            logger.warning("[claude_generate] Stop button never appeared — may be instant response")

        # ── 6. Wait for response to finish ────────────────────────────────────
        try:
            await page.wait_for_selector(
                'button[aria-label="Stop"]',
                state="hidden",
                timeout=RESPONSE_FINISH_MS,
            )
        except PlaywrightTimeoutError:
            raise RuntimeError(
                "Claude response did not finish streaming within 120s."
            )

        await asyncio.sleep(1.0)

        # ── 7. Extract response ───────────────────────────────────────────────
        response_text = await _extract_response(page)
        logger.info(f"[claude_generate] Done — {len(response_text)} chars extracted")
        return response_text

    finally:
        await browser.close()
        await pw.stop()
        logger.info("[claude_generate] Browser closed")


async def _extract_response(page) -> str:
    """Extract the last assistant message. Tries three selectors in order."""
    messages = await page.locator(".font-claude-message").all()
    if messages:
        text = await messages[-1].inner_text()
        if text and text.strip():
            return text.strip()

    messages = await page.locator('[data-testid="assistant-message"]').all()
    if messages:
        text = await messages[-1].inner_text()
        if text and text.strip():
            return text.strip()

    messages = await page.locator('[data-message-author-role="assistant"]').all()
    if messages:
        text = await messages[-1].inner_text()
        if text and text.strip():
            return text.strip()

    body_snippet = (await page.inner_text("body"))[:500]
    logger.error(f"[claude_generate] No response found. Page body: {body_snippet}")
    raise RuntimeError(
        "No assistant response found on the page. "
        "Claude.ai DOM may have changed — check Railway logs."
    )


async def validate_session() -> dict:
    """Quick health check: can we open claude.ai with current cookies?"""
    result = {"is_healthy": False, "url_after_load": None, "error": None}
    try:
        pw, browser, context = await get_browser_context("claude")
        try:
            page = await context.new_page()
            await page.goto("https://claude.ai", wait_until="domcontentloaded", timeout=30_000)
            url = page.url
            result["url_after_load"] = url
            result["is_healthy"] = not any(
                kw in url for kw in ["login", "upgrade", "onboard", "sign"]
            )
            if not result["is_healthy"]:
                result["error"] = f"Redirected to {url} — session expired"
        finally:
            await browser.close()
            await pw.stop()
    except Exception as e:
        result["error"] = str(e)
    return result
