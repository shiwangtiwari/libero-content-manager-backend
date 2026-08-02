"""
Claude.ai content generator — Phase 2 component.
Hits claude.ai with session cookies, submits prompt, extracts response.
This is the highest-risk component. Validate in Phase 2 before building anything else.
"""
import asyncio
import logging
from playwright.session_loader import get_browser_context

logger = logging.getLogger(__name__)

CLAUDE_URL = "https://claude.ai/new"
RESPONSE_SELECTOR = ".font-claude-message"
STOP_BUTTON_SELECTOR = 'button[aria-label="Stop"]'
INPUT_SELECTOR = 'div[contenteditable="true"]'

# Timeout constants (ms)
INPUT_TIMEOUT = 15_000
STOP_APPEAR_TIMEOUT = 15_000
STOP_DISAPPEAR_TIMEOUT = 90_000


async def generate_linkedin_post(prompt: str) -> str:
    """
    Submit prompt to Claude.ai, wait for response, return text.
    Raises on session expiry, blocked access, or timeout.
    """
    playwright, browser, context = await get_browser_context("claude")
    try:
        page = await context.new_page()

        logger.info(f"[Claude] Opening {CLAUDE_URL}")
        await page.goto(CLAUDE_URL, wait_until="networkidle", timeout=30_000)

        # Verify we're logged in (not on /login redirect)
        if "/login" in page.url or "claude.ai/login" in page.url:
            raise PermissionError(
                "Claude session expired. "
                "Re-export CLAUDE_COOKIES from Cookie-Editor and update Railway var."
            )

        # Type prompt
        input_box = page.locator(INPUT_SELECTOR).first
        await input_box.wait_for(state="visible", timeout=INPUT_TIMEOUT)
        await input_box.fill(prompt)
        await page.keyboard.press("Enter")

        # Wait for Stop button to appear (generation started)
        await page.wait_for_selector(STOP_BUTTON_SELECTOR, timeout=STOP_APPEAR_TIMEOUT)
        logger.info("[Claude] Generation started...")

        # Wait for Stop button to disappear (generation complete)
        await page.wait_for_selector(
            STOP_BUTTON_SELECTOR, state="hidden", timeout=STOP_DISAPPEAR_TIMEOUT
        )
        logger.info("[Claude] Generation complete.")

        # Extract the last assistant message
        messages = await page.locator(RESPONSE_SELECTOR).all()
        if not messages:
            raise RuntimeError("No response message found from Claude. Page may have changed structure.")

        response_text = await messages[-1].inner_text()
        return response_text.strip()

    finally:
        await browser.close()
        await playwright.stop()


async def health_check_claude() -> dict:
    """
    Lightweight health check — opens claude.ai and verifies we're logged in.
    Used by the session health job in Phase 6.
    Returns {"healthy": bool, "error": str | None}
    """
    try:
        playwright, browser, context = await get_browser_context("claude")
        page = await context.new_page()
        await page.goto(CLAUDE_URL, wait_until="networkidle", timeout=20_000)
        is_logged_in = "/login" not in page.url
        await browser.close()
        await playwright.stop()
        return {"healthy": is_logged_in, "error": None if is_logged_in else "Session expired — redirected to login"}
    except Exception as e:
        return {"healthy": False, "error": str(e)}
