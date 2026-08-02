
"""
playwright/session_loader.py
----------------------------
Loads a Playwright browser context pre-loaded with session cookies
for claude.ai, chatgpt.com, or gemini.google.com.

Every Playwright script in this project calls get_browser_context().
Never instantiate Playwright directly in the scripts.

Usage:
    pw, browser, context = await get_browser_context("claude")
    try:
        page = await context.new_page()
        ...
    finally:
        await browser.close()
        await pw.stop()
"""

import json
import logging
import os

from playwright.async_api import async_playwright, Browser, BrowserContext, Playwright

logger = logging.getLogger(__name__)

COOKIE_ENV_MAP = {
    "claude":   "CLAUDE_COOKIES",
    "chatgpt":  "CHATGPT_COOKIES",
    "gemini":   "GEMINI_COOKIES",
}

# Realistic desktop Chrome UA — minimises headless detection
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def get_browser_context(
    platform: str,
) -> tuple[Playwright, Browser, BrowserContext]:
    """
    Returns (playwright_instance, browser, context) with session cookies injected.
    Caller must close browser and stop playwright in a try/finally block.
    """
    if platform not in COOKIE_ENV_MAP:
        raise ValueError(
            f"Unknown platform '{platform}'. Must be one of: {list(COOKIE_ENV_MAP.keys())}"
        )

    env_var = COOKIE_ENV_MAP[platform]
    cookies_json = os.environ.get(env_var)

    if not cookies_json:
        raise EnvironmentError(
            f"Environment variable '{env_var}' is not set. "
            f"Export cookies using Cookie-Editor and paste the JSON array "
            f"into Railway → Variables → {env_var}."
        )

    try:
        cookies = json.loads(cookies_json)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"'{env_var}' contains invalid JSON. "
            f"Re-export cookies and paste the full JSON array (starts with '[', ends with ']'). "
            f"Parse error: {e}"
        )

    if not isinstance(cookies, list):
        raise ValueError(
            f"'{env_var}' must be a JSON array starting with '['. Got: {type(cookies).__name__}"
        )

    logger.info(f"[session_loader] platform={platform} cookies={len(cookies)}")

    pw = await async_playwright().start()

    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,800",
        ],
    )

    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        timezone_id="Asia/Kolkata",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )

    # Spoof navigator.webdriver = undefined before any page loads
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    # Sanitise sameSite values — Cookie-Editor sometimes exports non-standard values
# like "unspecified" that Playwright rejects. Normalise to "Lax" as safe default.
valid_same_site = {"Strict", "Lax", "None"}
for cookie in cookies:
    if cookie.get("sameSite") not in valid_same_site:
        cookie["sameSite"] = "Lax"

await context.add_cookies(cookies)

    logger.info(f"[session_loader] Browser context ready for platform={platform}")
    return pw, browser, context
