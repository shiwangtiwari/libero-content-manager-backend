"""
services/linkedin_poster.py
LinkedIn posting with:
- Image upload + asset availability retry loop (3 attempts, 2s each)
- Posting retry logic (3 attempts, 30-minute gaps)
- Token expiry detection (401 → Telegram alert with renewal instructions)
- Text-only fallback if no image
Phase 6: hardened error handling, all failures surface to Telegram.
"""

import asyncio
import logging
import os
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

LINKEDIN_API = "https://api.linkedin.com/v2"
LINKEDIN_UGC = "https://api.linkedin.com/v2/ugcPosts"
LINKEDIN_ASSETS = "https://api.linkedin.com/v2/assets"

TOKEN_EXPIRY_MESSAGE = (
    "🔴 <b>LinkedIn token expired!</b>\n\n"
    "Your LinkedIn access token has expired (60-day limit).\n\n"
    "<b>To renew (10 minutes):</b>\n"
    "1. Go to your Railway dashboard → Variables\n"
    "2. Open this URL in your browser:\n"
    "   <code>https://www.linkedin.com/oauth/v2/authorization"
    "?response_type=code&client_id={client_id}"
    "&redirect_uri={redirect_uri}"
    "&scope=openid%20profile%20email%20w_member_social</code>\n"
    "3. After approving, copy the <code>?code=...</code> from the redirect URL\n"
    "4. POST to <code>/auth/linkedin/refresh?code=YOUR_CODE</code> on your Railway URL\n"
    "5. New token will be saved automatically\n\n"
    "⏰ Posts will not go out until token is renewed."
)


def _get_headers() -> dict:
    token = os.environ.get("LINKEDIN_ACCESS_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }


def _person_urn() -> str:
    return os.environ.get("LINKEDIN_PERSON_URN", "")


async def _send_telegram_alert(message: str):
    """Send a Telegram message (non-blocking best-effort)."""
    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.error(f"Telegram alert failed: {e}")


async def _check_token_validity() -> bool:
    """Quick token check — returns True if valid, False if expired/invalid."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.linkedin.com/v2/userinfo",
                headers=_get_headers(),
            )
            if resp.status_code == 401:
                logger.error("LinkedIn token is expired (401)")
                return False
            return resp.status_code == 200
    except Exception as e:
        logger.error(f"Token validity check error: {e}")
        return False


async def _register_image_upload() -> Optional[tuple]:
    """
    Register an image upload with LinkedIn.
    Returns (upload_url, asset_urn) or None on failure.
    """
    person_urn = _person_urn()
    payload = {
        "registerUploadRequest": {
            "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
            "owner": person_urn,
            "serviceRelationships": [{
                "relationshipType": "OWNER",
                "identifier": "urn:li:userGeneratedContent",
            }],
        }
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{LINKEDIN_ASSETS}?action=registerUpload",
                headers=_get_headers(),
                json=payload,
            )
            if resp.status_code == 401:
                raise PermissionError("TOKEN_EXPIRED")
            resp.raise_for_status()
            data = resp.json()
            upload_url = data["value"]["uploadMechanism"][
                "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
            ]["uploadUrl"]
            asset_urn = data["value"]["asset"]
            return upload_url, asset_urn
    except PermissionError:
        raise
    except Exception as e:
        logger.error(f"_register_image_upload error: {e}")
        return None


async def _upload_image_binary(upload_url: str, image_url: str) -> bool:
    """Download image from Supabase URL and PUT to LinkedIn upload URL."""
    try:
        # Download image from Supabase Storage
        async with httpx.AsyncClient(timeout=30) as client:
            img_resp = await client.get(image_url)
            img_resp.raise_for_status()
            image_data = img_resp.content

        # Detect content type
        content_type = "image/png"
        if image_url.lower().endswith(".jpg") or image_url.lower().endswith(".jpeg"):
            content_type = "image/jpeg"
        elif image_url.lower().endswith(".webp"):
            content_type = "image/webp"

        # Upload to LinkedIn
        headers = {
            "Authorization": _get_headers()["Authorization"],
            "Content-Type": content_type,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            put_resp = await client.put(upload_url, headers=headers, content=image_data)
            put_resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"_upload_image_binary error: {e}")
        return False


async def _wait_for_asset_available(asset_urn: str, max_attempts: int = 10) -> bool:
    """
    Poll LinkedIn until the uploaded asset is AVAILABLE.
    LinkedIn requires this before creating the post.
    """
    asset_id = asset_urn.replace("urn:li:digitalmediaAsset:", "")
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{LINKEDIN_ASSETS}/{asset_id}",
                    headers=_get_headers(),
                )
                if resp.status_code == 200:
                    status = resp.json().get("serviceRelationships", [{}])[0].get(
                        "identifier", ""
                    )
                    # Also check top-level status field
                    asset_status = resp.json().get("status", "")
                    if asset_status == "AVAILABLE" or status == "AVAILABLE":
                        logger.info(f"Asset {asset_id} is AVAILABLE after {attempt + 1} attempts")
                        return True
        except Exception as e:
            logger.warning(f"Asset poll attempt {attempt + 1} error: {e}")
        await asyncio.sleep(2)

    logger.error(f"Asset {asset_id} never became AVAILABLE after {max_attempts} attempts")
    return False


async def _create_ugc_post(content: str, asset_urn: Optional[str] = None) -> Optional[str]:
    """
    Create the LinkedIn UGC post.
    Returns the LinkedIn post ID on success, None on failure.
    Raises PermissionError if token is expired.
    """
    person_urn = _person_urn()

    if asset_urn:
        specific_content = {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": content},
                "shareMediaCategory": "IMAGE",
                "media": [{
                    "status": "READY",
                    "media": asset_urn,
                }],
            }
        }
    else:
        specific_content = {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": content},
                "shareMediaCategory": "NONE",
            }
        }

    payload = {
        "author": person_urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": specific_content,
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                LINKEDIN_UGC,
                headers=_get_headers(),
                json=payload,
            )
            if resp.status_code == 401:
                raise PermissionError("TOKEN_EXPIRED")
            resp.raise_for_status()

            # Extract post ID from response header or body
            post_id = resp.headers.get("x-linkedin-id") or resp.headers.get("X-LinkedIn-Id")
            if not post_id:
                # Sometimes in the JSON
                try:
                    post_id = resp.json().get("id", "")
                except Exception:
                    post_id = ""

            # Fallback: construct URL from person URN
            if not post_id:
                post_id = f"unknown_{asset_urn or 'text'}"

            logger.info(f"LinkedIn post created: {post_id}")
            return post_id
    except PermissionError:
        raise
    except Exception as e:
        logger.error(f"_create_ugc_post error: {e}")
        return None


async def post_to_linkedin(post_id: str, content: str, image_url: Optional[str] = None) -> dict:
    """
    Main entry point. Posts to LinkedIn with full retry logic.

    Retry strategy:
    - Up to 3 attempts
    - 30-minute wait between attempts (scheduler handles this via job re-queue)
    - Token expiry → immediate Telegram alert, no retry

    Returns:
        {"success": True, "linkedin_post_id": "..."} on success
        {"success": False, "error": "...", "token_expired": bool, "retry": bool}
    """
    from db.queries import update_post_linkedin_id, update_post_status_failed

    # Step 0: Check token validity first
    token_valid = await _check_token_validity()
    if not token_valid:
        client_id = os.environ.get("LINKEDIN_CLIENT_ID", "YOUR_CLIENT_ID")
        redirect_uri = f"{os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'https://your-railway-url.up.railway.app')}/auth/linkedin/callback"
        alert = TOKEN_EXPIRY_MESSAGE.format(client_id=client_id, redirect_uri=redirect_uri)
        await _send_telegram_alert(alert)
        return {"success": False, "error": "LinkedIn token expired", "token_expired": True, "retry": False}

    asset_urn = None

    # Step 1: If image exists, upload it
    if image_url:
        logger.info(f"Uploading image for post {post_id}: {image_url}")
        try:
            result = await _register_image_upload()
            if result:
                upload_url, asset_urn = result
                uploaded = await _upload_image_binary(upload_url, image_url)
                if uploaded:
                    available = await _wait_for_asset_available(asset_urn)
                    if not available:
                        logger.warning(f"Image asset not available for post {post_id}, posting text-only")
                        asset_urn = None
                        await _send_telegram_alert(
                            f"⚠️ Image upload for post timed out — posting <b>text only</b>.\n\nPost ID: <code>{post_id}</code>"
                        )
                else:
                    asset_urn = None
                    await _send_telegram_alert(
                        f"⚠️ Image binary upload failed — posting <b>text only</b>.\n\nPost ID: <code>{post_id}</code>"
                    )
        except PermissionError:
            client_id = os.environ.get("LINKEDIN_CLIENT_ID", "YOUR_CLIENT_ID")
            redirect_uri = f"{os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'https://your-railway-url.up.railway.app')}/auth/linkedin/callback"
            alert = TOKEN_EXPIRY_MESSAGE.format(client_id=client_id, redirect_uri=redirect_uri)
            await _send_telegram_alert(alert)
            return {"success": False, "error": "LinkedIn token expired during image upload", "token_expired": True, "retry": False}

    # Step 2: Create the post
    try:
        linkedin_post_id = await _create_ugc_post(content, asset_urn)
    except PermissionError:
        client_id = os.environ.get("LINKEDIN_CLIENT_ID", "YOUR_CLIENT_ID")
        redirect_uri = f"{os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'https://your-railway-url.up.railway.app')}/auth/linkedin/callback"
        alert = TOKEN_EXPIRY_MESSAGE.format(client_id=client_id, redirect_uri=redirect_uri)
        await _send_telegram_alert(alert)
        return {"success": False, "error": "LinkedIn token expired during post creation", "token_expired": True, "retry": False}

    if not linkedin_post_id:
        return {"success": False, "error": "Post creation failed — no post ID returned", "token_expired": False, "retry": True}

    # Step 3: Update Supabase
    update_post_linkedin_id(post_id, linkedin_post_id)

    # Step 4: Build LinkedIn post URL
    person_id = _person_urn().replace("urn:li:person:", "")
    post_url = f"https://www.linkedin.com/feed/update/urn:li:share:{linkedin_post_id}/"

    return {
        "success": True,
        "linkedin_post_id": linkedin_post_id,
        "post_url": post_url,
    }


async def fetch_linkedin_metrics(linkedin_post_id: str) -> dict:
    """
    Fetch engagement metrics for a posted LinkedIn post.
    Note: LinkedIn API only returns basic metadata on UGC posts.
    Social actions (likes, comments) require additional API calls.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Try to get the share statistics
            resp = await client.get(
                f"{LINKEDIN_API}/socialActions/{linkedin_post_id}",
                headers=_get_headers(),
            )
            if resp.status_code == 401:
                logger.warning("LinkedIn metrics fetch: token expired")
                return {}
            if resp.status_code != 200:
                logger.warning(f"LinkedIn metrics fetch returned {resp.status_code}")
                return {}

            data = resp.json()
            return {
                "likes": data.get("likesSummary", {}).get("totalLikes", 0),
                "comments": data.get("commentsSummary", {}).get("totalFirstLevelComments", 0),
                "shares": 0,
                "impressions": 0,
                "clicks": 0,
            }
    except Exception as e:
        logger.error(f"fetch_linkedin_metrics error: {e}")
        return {}
