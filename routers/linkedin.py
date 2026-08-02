"""
LinkedIn OAuth2 callback and posting endpoint.

OAuth2 flow:
  1. Visit the authorization URL (Section 8.2 in master doc) in your browser
  2. LinkedIn redirects to /auth/linkedin/callback?code=XXX
  3. This handler exchanges the code for an access token
  4. Token is printed to logs — copy it to Railway env var LINKEDIN_ACCESS_TOKEN

After initial setup, the callback endpoint is only needed for token refresh (every 60 days).
"""
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from config import settings

router = APIRouter(tags=["linkedin"])

LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"


@router.get("/auth/linkedin/callback")
async def linkedin_callback(code: str, request: Request):
    """
    Exchange authorization code for access token.
    Called automatically by LinkedIn after OAuth2 consent.
    Copy the returned access_token to Railway env LINKEDIN_ACCESS_TOKEN.
    Copy the returned sub to Railway env LINKEDIN_PERSON_URN.
    """
    redirect_uri = str(request.url_for("linkedin_callback"))

    async with httpx.AsyncClient() as client:
        # Exchange code for token
        token_resp = await client.post(
            LINKEDIN_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": settings.LINKEDIN_CLIENT_ID,
                "client_secret": settings.LINKEDIN_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_data = token_resp.json()

    if "access_token" not in token_data:
        return JSONResponse(status_code=400, content={
            "error": "token_exchange_failed",
            "detail": token_data,
        })

    access_token = token_data["access_token"]

    # Get person URN
    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            LINKEDIN_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        userinfo = userinfo_resp.json()

    person_urn = userinfo.get("sub", "")

    return JSONResponse(content={
        "message": "OAuth2 complete. Copy these values to Railway environment variables.",
        "LINKEDIN_ACCESS_TOKEN": access_token,
        "LINKEDIN_PERSON_URN": f"urn:li:person:{person_urn}" if person_urn else "not_found",
        "expires_in_seconds": token_data.get("expires_in"),
        "scope": token_data.get("scope"),
    })


@router.get("/auth/linkedin/url")
async def get_auth_url(request: Request):
    """Returns the LinkedIn OAuth2 authorization URL to open in your browser."""
    redirect_uri = str(request.base_url) + "auth/linkedin/callback"
    url = (
        "https://www.linkedin.com/oauth/v2/authorization"
        f"?response_type=code"
        f"&client_id={settings.LINKEDIN_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope=openid%20profile%20email%20w_member_social"
    )
    return {"auth_url": url, "redirect_uri": redirect_uri}
