"""
LinkedIn posting service.
Implements the full image upload + asset availability retry loop + UGC post creation.
This is the only place that touches LinkedIn API. Called by scheduler.py at posting time.

Fix (2026-08-04):
  - Asset availability retry loop: was 10 × 2s = 20 seconds. Extended to 30 × 3s = 90 seconds.
    LinkedIn documentation says assets can take up to 60 seconds to reach AVAILABLE status.
    The old 20-second limit was the direct cause of the post going out as text-only today.
  - Status field parsing made robust: LinkedIn's asset API can return status either at
    the top level (status_data["status"]) or nested inside recipes[0]["status"].
    We now check both so a format variation doesn't cause a false timeout.
  - httpx client timeout raised from 60s to 120s to accommodate the longer wait.
  - Image download uses a separate short-timeout client so a slow Supabase response
    doesn't eat into the LinkedIn API budget.
"""
import asyncio
import logging
import httpx
from datetime import datetime
import pytz
from config import settings
from db import queries

logger = logging.getLogger(__name__)

LINKEDIN_API = "https://api.linkedin.com/v2"

# How long to wait for LinkedIn to process the uploaded image asset.
# LinkedIn docs say up to 60s; we wait 90s to be safe (30 attempts × 3s).
_ASSET_POLL_ATTEMPTS = 30
_ASSET_POLL_INTERVAL = 3   # seconds between each status check


async def post_to_linkedin(post_id: str) -> dict:
    """
    Full posting flow for a post record from Supabase.
    Returns {"success": True, "linkedin_post_id": "...", "posted_with_image": bool} or raises.
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

    # Quick token check before attempting the full flow.
    token_ok = await _check_token(headers)
    if not token_ok:
        await _alert_token_expired()
        raise RuntimeError(
            "LinkedIn access token is expired or invalid. "
            "Renew it following Section 8 of the master doc. "
            "Post saved as failed — retry from dashboard after renewing."
        )

    posted_with_image = False
    # Use a longer timeout to accommodate the 90-second asset availability wait
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            if image_url and image_url.startswith("https://"):
                try:
                    asset_urn = await _upload_image(client, headers, person_urn, image_url)
                    linkedin_post_id = await _create_ugc_post_with_image(
                        client, headers, person_urn, content, asset_urn
                    )
                    posted_with_image = True
                except TimeoutError as te:
                    logger.warning(
                        "[linkedin_poster] Image availability timeout after %ds — posting text-only: %s",
                        _ASSET_POLL_ATTEMPTS * _ASSET_POLL_INTERVAL, te,
                    )
                    await _alert_image_fallback(str(te))
                    linkedin_post_id = await _create_ugc_post_text_only(
                        client, headers, person_urn, content
                    )
                except (httpx.HTTPStatusError, httpx.RequestError) as img_err:
                    logger.warning(
                        "[linkedin_poster] Image upload failed — posting text-only: %s", img_err
                    )
                    await _alert_image_fallback(str(img_err))
                    linkedin_post_id = await _create_ugc_post_text_only(
                        client, headers, person_urn, content
                    )
            else:
                # No image or bad URL (telegram://) — post text-only directly
                if image_url and not image_url.startswith("https://"):
                    logger.warning(
                        "[linkedin_poster] image_url is not a valid https URL (%s) — text-only",
                        image_url[:40],
                    )
                linkedin_post_id = await _create_ugc_post_text_only(
                    client, headers, person_urn, content
                )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            await _alert_token_expired()
            raise RuntimeError(
                "LinkedIn 401 during posting — token expired. "
                "Renew following Section 8 of master doc."
            ) from e
        raise

    ist = pytz.timezone("Asia/Kolkata")
    posted_time = datetime.now(ist).isoformat()
    queries.mark_post_posted(post_id, linkedin_post_id, posted_time)

    return {
        "success": True,
        "linkedin_post_id": linkedin_post_id,
        "posted_with_image": posted_with_image,
    }


async def _alert_image_fallback(reason: str) -> None:
    """Notify Shiwang that the image failed and the post went out as text-only."""
    import os
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return
    message = (
        f"<b>[IMAGE FAILED — TEXT ONLY]</b>\n\n"
        f"<code>REASON  {reason[:200]}</code>\n\n"
        f"Post went live as text-only. The image was not attached."
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.error("[linkedin_poster] Image fallback alert failed: %s", e)


async def _check_token(headers: dict) -> bool:
    """Quick LinkedIn token validity check. Returns True if valid."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{LINKEDIN_API}/userinfo", headers=headers)
            return resp.status_code == 200
    except Exception as e:
        logger.warning("[linkedin_poster] Token check request failed (proceeding anyway): %s", e)
        return True  # Network error — let the main flow proceed


async def _alert_token_expired() -> None:
    """Send a Telegram alert with LinkedIn token renewal instructions."""
    import os
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return
    message = (
        "<b>[LINKEDIN TOKEN EXPIRED]</b>\n\n"
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
        logger.error("[linkedin_poster] Token expiry alert failed to send: %s", e)


def _parse_asset_status(status_data: dict) -> str:
    """
    Extract the asset processing status from LinkedIn's asset API response.

    LinkedIn can return the status in two places depending on API version:
      1. Top-level:  status_data["status"]  → "AVAILABLE" | "PROCESSING" | "WAITING_UPLOAD"
      2. Nested:     status_data["recipes"][0]["status"]

    We check both so a response format variation doesn't cause a false timeout.
    Returns the status string, or empty string if not found.
    """
    # Top-level status field (most common)
    top = status_data.get("status", "")
    if top:
        return top

    # Nested inside recipes array
    recipes = status_data.get("recipes", [])
    if recipes and isinstance(recipes, list):
        nested = recipes[0].get("status", "")
        if nested:
            return nested

    # Some responses wrap everything in a "value" key
    value = status_data.get("value", {})
    if isinstance(value, dict):
        val_status = value.get("status", "")
        if val_status:
            return val_status
        val_recipes = value.get("recipes", [])
        if val_recipes and isinstance(val_recipes, list):
            return val_recipes[0].get("status", "")

    return ""


async def _upload_image(client: httpx.AsyncClient, headers: dict,
                        person_urn: str, image_url: str) -> str:
    """
    Register + upload image to LinkedIn, wait for AVAILABLE, return asset URN.

    Retry loop: _ASSET_POLL_ATTEMPTS × _ASSET_POLL_INTERVAL seconds.
    Default: 30 × 3s = 90 seconds total wait.
    LinkedIn docs say assets take up to 60s; 90s gives a comfortable buffer.
    """

    # Step 1: Register the upload slot with LinkedIn
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
    logger.info("[linkedin_poster] Asset URN: %s", asset_urn)

    # Step 2: Download image from Supabase Storage, then upload to LinkedIn
    # Use a separate short-timeout client for the download so it doesn't
    # eat into the LinkedIn API budget.
    async with httpx.AsyncClient(timeout=30) as dl_client:
        img_resp = await dl_client.get(image_url)
        img_resp.raise_for_status()
        image_bytes = img_resp.content

    logger.info(
        "[linkedin_poster] Downloaded image: %d bytes from %s",
        len(image_bytes), image_url[:60],
    )

    # Detect content type from the URL — LinkedIn accepts JPEG and PNG
    content_type = "image/jpeg" if image_url.lower().endswith(".jpg") or "jpeg" in image_url.lower() else "image/png"

    upload_resp = await client.put(
        upload_url,
        content=image_bytes,
        headers={
            "Authorization": f"Bearer {settings.LINKEDIN_ACCESS_TOKEN}",
            "Content-Type": content_type,
        },
    )
    upload_resp.raise_for_status()
    logger.info("[linkedin_poster] Image uploaded to LinkedIn. Waiting for AVAILABLE status...")

    # Step 3: Poll for AVAILABLE status
    # 30 attempts × 3 seconds = 90 seconds maximum wait
    asset_id = asset_urn.split(":")[-1]
    last_status = "UNKNOWN"

    for attempt in range(_ASSET_POLL_ATTEMPTS):
        await asyncio.sleep(_ASSET_POLL_INTERVAL)

        try:
            status_resp = await client.get(
                f"{LINKEDIN_API}/assets/{asset_id}",
                headers=headers,
            )
        except httpx.RequestError as e:
            logger.warning("[linkedin_poster] Asset status check failed (attempt %d): %s", attempt + 1, e)
            continue

        if status_resp.status_code == 200:
            status_data = status_resp.json()
            current_status = _parse_asset_status(status_data)
            last_status = current_status or last_status

            logger.debug(
                "[linkedin_poster] Asset status attempt %d/%d: %s",
                attempt + 1, _ASSET_POLL_ATTEMPTS, current_status,
            )

            if current_status == "AVAILABLE":
                logger.info(
                    "[linkedin_poster] Asset AVAILABLE after %ds",
                    (attempt + 1) * _ASSET_POLL_INTERVAL,
                )
                return asset_urn

            if current_status == "FAILED":
                raise RuntimeError(
                    f"LinkedIn image asset processing FAILED (asset: {asset_id}). "
                    f"Try generating a new image and sending it before approving."
                )
        else:
            logger.warning(
                "[linkedin_poster] Asset status check returned %d (attempt %d)",
                status_resp.status_code, attempt + 1,
            )

    total_wait = _ASSET_POLL_ATTEMPTS * _ASSET_POLL_INTERVAL
    raise TimeoutError(
        f"LinkedIn image asset {asset_id} did not reach AVAILABLE status after "
        f"{total_wait}s (last status: {last_status}). "
        f"Post will go out as text-only."
    )


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
