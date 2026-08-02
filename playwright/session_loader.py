"""
Session loader — loads browser context with cookies for a given platform.
Used by every Playwright script. Never called directly.

platform: "claude" | "chatgpt" | "gemini"
Cookie values come from Railway env vars (JSON arrays from Cookie-Editor).
"""
import json
import os
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext


async def get_browser_context(
    platform: str,
) -> tuple[Playwright, Browser, BrowserContext]:
    cookie_env_map = {
        "claude": "CLAUDE_COOKIES",
        "chatgpt": "CHATGPT_COOKIES",
        "gemini": "GEMINI_COOKIES",
    }

    env_var = cookie_env_map.get(platform)
    if not env_var:
        raise ValueError(f"Unknown platform: {platform}. Must be claude | chatgpt | gemini")

    cookies_json = os.environ.get(env_var)
    if not cookies_json:
        raise ValueError(
            f"Environment variable {env_var} is not set. "
            f"Export cookies from {platform} using Cookie-Editor and paste into Railway Variables."
        )

    try:
        cookies = json.loads(cookies_json)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"{env_var} contains invalid JSON: {e}. "
            f"Re-export cookies from Cookie-Editor and paste again."
        )

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    )
    await context.add_cookies(cookies)

    return playwright, browser, context
