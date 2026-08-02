"""
pw/gemini_image.py
-------------------
Generates an image via Gemini (gemini.google.com) using Playwright.
Uses GEMINI_COOKIES session from Railway Variables.

This is the P2 fallback when ChatGPT fails or is blocked.
Flow identical to chatgpt_image.py — different selectors and URL.

Called by: routers/telegram.py handle_generate_image
"""

import asyncio
import logging
import os
import time

import httpx
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .session_loader import get_browser_context

logger = logging.getLogger(__name__)

NAV_TIMEOUT = 120_000
IMAGE_WAIT_TIMEOUT = 120_000
DOWNLOAD_TIMEOUT = 30


async def generate_image_gemini(prompt: str, save_path: str | None = None) -> str:
    """
    Generate an image via Gemini and save to local file.

    Args:
        prompt: Full image generation prompt
        save_path: Where to save. Defaults to /tmp/libero_image_<timestamp>.png

    Returns:
        Local file path of the saved image.

    Raises:
        EnvironmentError: GEMINI_COOKIES not set
        RuntimeError: Generation failed or timed out
    """
    if save_path is None:
        save_path = f"/tmp/libero_image_{int(time.time())}.png"

    logger.info("[gemini_image] Starting image generation via Gemini")

    pw, browser, context = await get_browser_context("gemini")
    try:
        page = await context.new_page()

        # 1. Navigate to Gemini
        try:
            await page.goto(
                "https://gemini.google.com/",
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT,
            )
        except PlaywrightTimeoutError:
            raise RuntimeError(
                "gemini.google.com did not load within 120s on Railway. "
                "GEMINI_COOKIES are likely not set or expired. "
                "Export cookies from gemini.google.com using Cookie-Editor and add to Railway Variables."
            )

        current_url = page.url
        if any(kw in current_url for kw in ["accounts.google", "signin", "login"]):
            raise RuntimeError(
                f"Redirected to {current_url} — GEMINI_COOKIES are expired. "
                "Re-export from gemini.google.com and update Railway Variables."
            )

        await asyncio.sleep(4)  # Gemini SPA needs more time to render

        # 2. Find input
        input_selectors = [
            'div[contenteditable="true"]',
            'rich-textarea div[contenteditable="true"]',
            '[data-testid="chat-input"]',
            'p[data-placeholder]',
            'textarea',
        ]

        input_box = None
        for selector in input_selectors:
            try:
                await page.wait_for_selector(selector, timeout=5_000)
                input_box = page.locator(selector).first
                logger.info(f"[gemini_image] Found input: {selector}")
                break
            except PlaywrightTimeoutError:
                continue

        if input_box is None:
            page_body = (await page.inner_text("body"))[:500]
            logger.error(f"[gemini_image] Page body: {page_body}")
            raise RuntimeError(
                "Could not find Gemini input box. "
                "Session may be expired or UI changed. Check Railway logs."
            )

        # 3. Submit prompt — wrap in "Generate an image:" prefix for Gemini
        full_prompt = f"Generate an image: {prompt}"
        await input_box.click()
        await asyncio.sleep(0.3)
        await input_box.fill(full_prompt)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Enter")
        logger.info("[gemini_image] Prompt submitted, waiting for image...")

        # 4. Wait for image
        image_selectors = [
            'img[src*="generativelanguage"]',
            'img[src*="googleusercontent"]',
            '.response-container img',
            '[data-testid="image-container"] img',
            'model-response img',
            '.image-generation-result img',
        ]

        img_element = None
        deadline = time.time() + (IMAGE_WAIT_TIMEOUT / 1000)

        while time.time() < deadline:
            for selector in image_selectors:
                try:
                    await page.wait_for_selector(selector, timeout=5_000)
                    candidate = page.locator(selector).first
                    src = await candidate.get_attribute("src")
                    # Skip tiny icons/avatars — real generated images are large
                    if src and not any(skip in src for skip in ["icon", "avatar", "logo", "favicon"]):
                        img_element = candidate
                        logger.info(f"[gemini_image] Image found: {selector}")
                        break
                except PlaywrightTimeoutError:
                    continue
            if img_element:
                break

            # Check for errors
            page_text = await page.inner_text("body")
            error_phrases = ["can't generate", "cannot generate", "unable to create", "I'm not able"]
            for phrase in error_phrases:
                if phrase.lower() in page_text.lower():
                    raise RuntimeError(
                        f"Gemini refused to generate image: '{phrase}' found in response."
                    )
            await asyncio.sleep(3)

        if img_element is None:
            raise RuntimeError(
                "Image did not appear within 90 seconds on Gemini. "
                "Try again or check GEMINI_COOKIES."
            )

        # 5. Download image
        img_url = await img_element.get_attribute("src")
        if not img_url:
            raise RuntimeError("Image element src is empty.")

        logger.info(f"[gemini_image] Downloading from: {img_url[:80]}...")

        cookies = await context.cookies()
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        async with httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT,
            follow_redirects=True,
            headers={"Cookie": cookie_header},
        ) as client:
            response = await client.get(img_url)

        if response.status_code != 200:
            raise RuntimeError(f"Image download failed: HTTP {response.status_code}")

        with open(save_path, "wb") as f:
            f.write(response.content)

        file_size = os.path.getsize(save_path)
        logger.info(f"[gemini_image] Saved: {save_path} ({file_size} bytes)")

        if file_size < 1000:
            raise RuntimeError(f"Downloaded file too small ({file_size} bytes).")

        return save_path

    finally:
        await browser.close()
        await pw.stop()
        logger.info("[gemini_image] Browser closed")
