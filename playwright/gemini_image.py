"""
Gemini image generator — Phase 4 fallback component.
Hits gemini.google.com with session cookies, submits image prompt, downloads result.
Used when ChatGPT fails or is unavailable.
"""
import asyncio
import logging
import httpx
from playwright.session_loader import get_browser_context

logger = logging.getLogger(__name__)

GEMINI_URL = "https://gemini.google.com/"
IMAGE_TIMEOUT_MS = 90_000


async def generate_image_gemini(prompt: str, save_path: str) -> str:
    """
    Submit image prompt to Gemini, wait for image, download to save_path.
    Returns save_path on success.
    Phase 4 — not called until Phase 4 deployment.
    Note: Google has stronger bot detection. May need more frequent cookie refreshes.
    """
    playwright, browser, context = await get_browser_context("gemini")
    try:
        page = await context.new_page()
        logger.info("[Gemini] Opening gemini.google.com")
        await page.goto(GEMINI_URL, wait_until="networkidle", timeout=30_000)

        if "accounts.google.com" in page.url:
            raise PermissionError(
                "Gemini session expired or 2FA triggered. "
                "Re-export GEMINI_COOKIES and update Railway var."
            )

        # Find the prompt input (Gemini's selector varies — try rich-textarea first)
        input_selectors = [
            'div[contenteditable="true"]',
            'rich-textarea div[contenteditable="true"]',
            'textarea[aria-label="Enter a prompt here"]',
        ]
        input_box = None
        for selector in input_selectors:
            try:
                loc = page.locator(selector).first
                await loc.wait_for(state="visible", timeout=5_000)
                input_box = loc
                break
            except Exception:
                continue

        if not input_box:
            raise RuntimeError("Could not find Gemini prompt input. Page structure may have changed.")

        await input_box.fill(f"Generate an image: {prompt}")
        await page.keyboard.press("Enter")

        # Wait for an img tag that looks like a generated image
        img_locator = page.locator('img[src*="generativelanguage"], img[src*="blob:"], img.generated-image').first
        await img_locator.wait_for(state="visible", timeout=IMAGE_TIMEOUT_MS)
        img_url = await img_locator.get_attribute("src")

        if not img_url or img_url.startswith("blob:"):
            # Blob URLs require special handling — screenshot fallback
            logger.warning("[Gemini] Image is a blob URL, using screenshot fallback")
            await img_locator.screenshot(path=save_path)
            return save_path

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(img_url)
            resp.raise_for_status()
            with open(save_path, "wb") as f:
                f.write(resp.content)

        logger.info(f"[Gemini] Image saved to {save_path}")
        return save_path

    finally:
        await browser.close()
        await playwright.stop()
