"""
pw/chatgpt_image.py
--------------------
Generates an image via ChatGPT (chatgpt.com) using Playwright.
Uses CHATGPT_COOKIES session from Railway Variables.

Flow:
  1. Open chatgpt.com with session cookies
  2. Submit image generation prompt
  3. Wait for image to appear (up to 90 seconds)
  4. Download image bytes and save to /tmp/libero_image_<timestamp>.png
  5. Return local file path

Called by: routers/telegram.py handle_generate_image
Image prompt template: master doc Section 11.4
"""

import asyncio
import logging
import os
import time

import httpx
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .session_loader import get_browser_context

logger = logging.getLogger(__name__)

# Timeouts (milliseconds)
NAV_TIMEOUT = 60_000
IMAGE_WAIT_TIMEOUT = 90_000   # ChatGPT image generation can take up to 90s
DOWNLOAD_TIMEOUT = 30         # seconds for httpx download


def build_image_prompt(topic: str, post_hook: str = "") -> str:
    """
    Build image generation prompt from master doc Section 11.4.
    post_hook: first line of the LinkedIn post, used as the headline.
    """
    headline = post_hook[:60] if post_hook else topic[:60]
    return (
        f"Generate a LinkedIn post image for this topic: {topic}\n\n"
        f"Style: Clean, professional, modern. Bold typography.\n"
        f"Color palette: Dark navy or deep blue background with white and light blue text.\n"
        f"Include: A short punchy headline (5-7 words max): \"{headline}\"\n"
        f"Format: 1200x627 pixels (LinkedIn optimal).\n"
        f"No stock photo people. No generic business imagery.\n"
        f"Typography-forward or minimal abstract graphic only."
    )


async def generate_image_chatgpt(prompt: str, save_path: str | None = None) -> str:
    """
    Generate an image via ChatGPT and save to local file.

    Args:
        prompt: Full image generation prompt
        save_path: Where to save the image. Defaults to /tmp/libero_image_<timestamp>.png

    Returns:
        Local file path of the saved image.

    Raises:
        EnvironmentError: CHATGPT_COOKIES not set
        RuntimeError: Generation failed or timed out
    """
    if save_path is None:
        save_path = f"/tmp/libero_image_{int(time.time())}.png"

    logger.info("[chatgpt_image] Starting image generation via ChatGPT")

    pw, browser, context = await get_browser_context("chatgpt")
    try:
        page = await context.new_page()

        # 1. Navigate to ChatGPT
        try:
            await page.goto(
                "https://chatgpt.com/",
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT,
            )
        except PlaywrightTimeoutError:
            raise RuntimeError("chatgpt.com did not load within 60s. Session may be expired.")

        # Check we're logged in
        current_url = page.url
        if any(kw in current_url for kw in ["login", "auth", "signin"]):
            raise RuntimeError(
                f"Redirected to {current_url} — CHATGPT_COOKIES are expired. "
                "Re-export from chatgpt.com and update Railway Variables."
            )

        # Wait for UI to render
        await asyncio.sleep(3)

        # 2. Find the input box
        input_selectors = [
            "#prompt-textarea",
            'div[contenteditable="true"]',
            'textarea[placeholder]',
            '[data-testid="composer-input"]',
        ]

        input_box = None
        for selector in input_selectors:
            try:
                await page.wait_for_selector(selector, timeout=5_000)
                input_box = page.locator(selector).first
                logger.info(f"[chatgpt_image] Found input: {selector}")
                break
            except PlaywrightTimeoutError:
                continue

        if input_box is None:
            page_body = (await page.inner_text("body"))[:500]
            logger.error(f"[chatgpt_image] Page body: {page_body}")
            raise RuntimeError(
                "Could not find ChatGPT input box. "
                "UI may have changed or session expired. Check Railway logs."
            )

        # 3. Submit the prompt
        await input_box.click()
        await asyncio.sleep(0.3)

        # Use type() for reliability with special characters in the prompt
        await input_box.fill(prompt)
        await asyncio.sleep(0.5)

        # Submit
        await page.keyboard.press("Enter")
        logger.info("[chatgpt_image] Prompt submitted, waiting for image...")

        # 4. Wait for image to appear
        # ChatGPT renders generated images as <img> tags with specific attributes
        image_selectors = [
            'img[alt*="Generated"]',
            'img[alt*="generated"]',
            'img[src*="oaidalleapiprodscus"]',  # DALL-E CDN URL pattern
            'img[src*="dalle"]',
            '.generated-image img',
            '[data-testid="image-generation-result"] img',
        ]

        img_element = None
        deadline = time.time() + (IMAGE_WAIT_TIMEOUT / 1000)

        while time.time() < deadline:
            for selector in image_selectors:
                try:
                    await page.wait_for_selector(selector, timeout=5_000)
                    img_element = page.locator(selector).first
                    logger.info(f"[chatgpt_image] Image found with selector: {selector}")
                    break
                except PlaywrightTimeoutError:
                    continue
            if img_element:
                break
            # Also check if generation failed
            error_texts = ["I'm not able to", "I cannot generate", "Error generating"]
            page_text = await page.inner_text("body")
            for err in error_texts:
                if err.lower() in page_text.lower():
                    raise RuntimeError(
                        f"ChatGPT refused to generate image: found '{err}' in response. "
                        "Try /generate_image gemini instead."
                    )
            await asyncio.sleep(3)

        if img_element is None:
            raise RuntimeError(
                "Image did not appear within 90 seconds. "
                "ChatGPT may be slow or have changed its UI. "
                "Try /generate_image gemini as fallback."
            )

        # 5. Get image URL and download
        img_url = await img_element.get_attribute("src")
        if not img_url:
            raise RuntimeError("Image element found but src attribute is empty.")

        logger.info(f"[chatgpt_image] Downloading image from: {img_url[:80]}...")

        # Get cookies for authenticated download
        cookies = await context.cookies()
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        async with httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT,
            follow_redirects=True,
            headers={"Cookie": cookie_header},
        ) as client:
            response = await client.get(img_url)

        if response.status_code != 200:
            raise RuntimeError(
                f"Image download failed: HTTP {response.status_code}. "
                "Image URL may require authentication or has expired."
            )

        # Save to disk
        with open(save_path, "wb") as f:
            f.write(response.content)

        file_size = os.path.getsize(save_path)
        logger.info(f"[chatgpt_image] Image saved: {save_path} ({file_size} bytes)")

        if file_size < 1000:
            raise RuntimeError(
                f"Downloaded file is too small ({file_size} bytes) — likely not a real image."
            )

        return save_path

    finally:
        await browser.close()
        await pw.stop()
        logger.info("[chatgpt_image] Browser closed")
