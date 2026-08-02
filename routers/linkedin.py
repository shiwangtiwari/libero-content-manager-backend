"""
LinkedIn OAuth2 callback and posting endpoint.

OAuth2 flow:
  1. Visit /auth/linkedin/url to get the authorization URL
  2. Open that URL in your browser — LinkedIn asks you to approve
  3. LinkedIn redirects to /auth/linkedin/callback?code=XXX
  4. This handler exchanges the code for an access token
  5. Copy the returned values to Railway env vars

After initial setup, only needed again every 60 days when token expires.
"""
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from config import settings

router = APIRouter(tags=["linkedin"])

LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"

# Hardcoded because Railway internal network reports http but public URL is https
RAILWAY_PUBLIC_URL = "https://libero-content-manager-backend-production.up.railway.app"
REDIRECT_URI = f"{RAILWAY_PUBLIC_URL}/auth/linkedin/callback"


@router.get("/auth/linkedin/callback")
async def linkedin_callback(code: str, request: Request):
    """
    Exchange authorization code for access token.
    LinkedIn redirects here after OAuth2 consent.
    Copy the returned LINKEDIN_ACCESS_TOKEN and LINKEDIN_PERSON_URN to Railway Variables.
    """
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            LINKEDIN_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
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

    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            LINKEDIN_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        userinfo = userinfo_resp.json()

    person_urn = userinfo.get("sub", "")

    return JSONResponse(content={
        "message": "OAuth2 complete. Copy these two values to Railway Variables now.",
        "LINKEDIN_ACCESS_TOKEN": access_token,
        "LINKEDIN_PERSON_URN": f"urn:li:person:{person_urn}" if person_urn else "not_found",
        "expires_in_seconds": token_data.get("expires_in"),
        "scope": token_data.get("scope"),
    })


@router.get("/auth/linkedin/url")
async def get_auth_url():
    """Returns the LinkedIn OAuth2 authorization URL — open it in your browser to authorize."""
    url = (
        "https://www.linkedin.com/oauth/v2/authorization"
        f"?response_type=code"
        f"&client_id={settings.LINKEDIN_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope=openid%20profile%20email%20w_member_social"
    )
    return {"auth_url": url, "redirect_uri": REDIRECT_URI}
