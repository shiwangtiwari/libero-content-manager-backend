"""
LinkedIn posting service.
Implements the full image upload + asset availability retry loop + UGC post creation.
This is the only place that touches LinkedIn API. Called by scheduler.py at posting time.
"""
import asyncio
import httpx
from datetime import datetime
import pytz
from config import settings
from db import queries

LINKEDIN_API = "https://api.linkedin.com/v2"


async def post_to_linkedin(post_id: str) -> dict:
    """
    Full posting flow for a post record from Supabase.
    Returns {"success": True, "linkedin_post_id": "..."} or raises.

    Phase 6: 401 responses send a Telegram alert with renewal instructions
    before raising, so Shiwang knows immediately why the post failed.
    """
    post = queries.get_post_by_id(post_id)
    if not post:
        raise ValueError(f"Post {post_id} not found")

    content = post["content"]
    image_url = post.get("image_url")

    headers = {
        "Authorization": f"Bearer {settings.LINKEDIN_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    person_urn = settings.LINKEDIN_PERSON_URN

    # Phase 6: quick token check before attempting the full flow.
    # Avoids uploading the image only to fail at the post step.
    token_ok = await _check_token(headers)
    if not token_ok:
        await _alert_token_expired()
        raise RuntimeError(
            "LinkedIn access token is expired or invalid. "
            "Renew it following Section 8 of the master doc. "
            "Post saved as failed — retry from dashboard after renewing."
        )

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            if image_url:
                asset_urn = await _upload_image(client, headers, person_urn, image_url)
                linkedin_post_id = await _create_ugc_post_with_image(client, headers, person_urn, content, asset_urn)
            else:
                linkedin_post_id = await _create_ugc_post_text_only(client, headers, person_urn, content)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            await _alert_token_expired()
            raise RuntimeError(
                "LinkedIn 401 during posting — token expired. "
                "Renew following Section 8 of master doc."
            ) from e
        raise  # Re-raise other HTTP errors unchanged

    ist = pytz.timezone("Asia/Kolkata")
    posted_time = datetime.now(ist).isoformat()
    queries.mark_post_posted(post_id, linkedin_post_id, posted_time)

    return {"success": True, "linkedin_post_id": linkedin_post_id}


async def _check_token(headers: dict) -> bool:
    """Quick LinkedIn token validity check. Returns True if valid."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{LINKEDIN_API}/userinfo",
                headers=headers,
            )
            return resp.status_code == 200
    except Exception as e:
        # Network error — let the main flow proceed rather than block posting
        logger.warning(f"[linkedin_poster] Token check request failed: {e}")
        return True


async def _alert_token_expired() -> None:
    """Send a Telegram alert with LinkedIn token renewal instructions."""
    import os
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return
    message = (
        "🔴 <b>LinkedIn token expired — post NOT sent</b>\n\n"
        "Your LinkedIn access token has hit the 60-day limit.\n\n"
        "<b>To renew (~10 minutes):</b>\n"
        "1. Open your Railway service → Variables tab\n"
        "2. Follow Section 8.2 of the master doc to get a new auth code\n"
        "3. Call the LinkedIn callback endpoint on your Railway URL\n"
        "4. New token saves automatically\n\n"
        "Your post is saved as <b>failed</b> in Supabase — retry from "
        "the dashboard once the token is renewed."
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.error(f"[linkedin_poster] Token expiry alert failed to send: {e}")


async def _upload_image(client: httpx.AsyncClient, headers: dict,
                        person_urn: str, image_url: str) -> str:
    """Register + upload image, wait for AVAILABLE, return asset URN."""

    # Step 1: Register upload
    register_resp = await client.post(
        f"{LINKEDIN_API}/assets?action=registerUpload",
        headers=headers,
        json={
            "registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                "owner": person_urn,
                "serviceRelationships": [{
                    "relationshipType": "OWNER",
                    "identifier": "urn:li:userGeneratedContent",
                }],
            }
        },
    )
    register_resp.raise_for_status()
    register_data = register_resp.json()

    upload_url = (
        register_data["value"]["uploadMechanism"]
        ["com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]["uploadUrl"]
    )
    asset_urn = register_data["value"]["asset"]

    # Step 2: Download local image bytes then upload to LinkedIn
    img_resp = await client.get(image_url)
    img_resp.raise_for_status()
    image_bytes = img_resp.content

    upload_resp = await client.put(
        upload_url,
        content=image_bytes,
        headers={
            "Authorization": f"Bearer {settings.LINKEDIN_ACCESS_TOKEN}",
            "Content-Type": "image/png",
        },
    )
    upload_resp.raise_for_status()

    # Step 3: Retry loop — wait for AVAILABLE status (critical, 2s × 10 attempts)
    asset_id = asset_urn.split(":")[-1]
    for attempt in range(10):
        await asyncio.sleep(2)
        status_resp = await client.get(
            f"{LINKEDIN_API}/assets/{asset_id}",
            headers=headers,
        )
        if status_resp.status_code == 200:
            status_data = status_resp.json()
            if status_data.get("status") == "AVAILABLE":
                return asset_urn
        # Keep retrying

    raise TimeoutError(f"LinkedIn image asset {asset_id} did not reach AVAILABLE status after 20 seconds")


async def _create_ugc_post_with_image(client: httpx.AsyncClient, headers: dict,
                                      person_urn: str, content: str,
                                      asset_urn: str) -> str:
    resp = await client.post(
        f"{LINKEDIN_API}/ugcPosts",
        headers=headers,
        json={
            "author": person_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": content},
                    "shareMediaCategory": "IMAGE",
                    "media": [{"status": "READY", "media": asset_urn}],
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        },
    )
    resp.raise_for_status()
    return resp.headers.get("x-restli-id", resp.json().get("id", "unknown"))


async def _create_ugc_post_text_only(client: httpx.AsyncClient, headers: dict,
                                     person_urn: str, content: str) -> str:
    resp = await client.post(
        f"{LINKEDIN_API}/ugcPosts",
        headers=headers,
        json={
            "author": person_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": content},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        },
    )
    resp.raise_for_status()
    return resp.headers.get("x-restli-id", resp.json().get("id", "unknown"))


async def send_test_post(content: str = "🤖 Libero Autonomous Edition — Phase 1 test post. Ignore.") -> dict:
    """Quick test to verify LinkedIn API credentials work. Called during Phase 1 setup."""
    headers = {
        "Authorization": f"Bearer {settings.LINKEDIN_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{LINKEDIN_API}/ugcPosts",
            headers=headers,
            json={
                "author": settings.LINKEDIN_PERSON_URN,
                "lifecycleState": "PUBLISHED",
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {
                        "shareCommentary": {"text": content},
                        "shareMediaCategory": "NONE",
                    }
                },
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
            },
        )
    if resp.status_code in (200, 201):
        post_id = resp.headers.get("x-restli-id", "sent")
        return {"success": True, "post_id": post_id}
    return {"success": False, "status_code": resp.status_code, "detail": resp.text}
