"""
ChatGPT image generator — Phase 4 component.
Hits chatgpt.com with session cookies, submits image prompt, downloads result.
"""
import asyncio
import logging
import httpx
from playwright.session_loader import get_browser_context

logger = logging.getLogger(__name__)

CHATGPT_URL = "https://chatgpt.com/"
PROMPT_SELECTOR = "#prompt-textarea"
IMAGE_SELECTOR = 'img[alt*="Generated"], img[alt*="generated"]'
IMAGE_TIMEOUT_MS = 90_000


async def generate_image_chatgpt(prompt: str, save_path: str) -> str:
    """
    Submit image prompt to ChatGPT, wait for image, download to save_path.
    Returns save_path on success.
    Phase 4 — not called until Phase 4 deployment.
    """
    playwright, browser, context = await get_browser_context("chatgpt")
    try:
        page = await context.new_page()
        logger.info("[ChatGPT] Opening chatgpt.com")
        await page.goto(CHATGPT_URL, wait_until="networkidle", timeout=30_000)

        if "auth" in page.url or "login" in page.url:
            raise PermissionError(
                "ChatGPT session expired. "
                "Re-export CHATGPT_COOKIES and update Railway var."
            )

        # Submit prompt
        prompt_box = page.locator(PROMPT_SELECTOR).first
        await prompt_box.wait_for(state="visible", timeout=10_000)
        await prompt_box.fill(f"Generate an image: {prompt}")
        await page.keyboard.press("Enter")

        # Wait for generated image
        img_locator = page.locator(IMAGE_SELECTOR).first
        await img_locator.wait_for(state="visible", timeout=IMAGE_TIMEOUT_MS)
        img_url = await img_locator.get_attribute("src")

        if not img_url:
            raise RuntimeError("Could not extract image URL from ChatGPT response")

        # Download image
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(img_url)
            resp.raise_for_status()
            with open(save_path, "wb") as f:
                f.write(resp.content)

        logger.info(f"[ChatGPT] Image saved to {save_path}")
        return save_path

    finally:
        await browser.close()
        await playwright.stop()
