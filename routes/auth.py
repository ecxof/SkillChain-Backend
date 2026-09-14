import os
import httpx

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from db.database import get_db
from models.user import User
from services.auth_service import (
    create_access_token,
    get_current_user,
    get_or_create_user_google,
    get_or_create_user_github,
)

router = APIRouter()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")


# ── Schemas ────────────────────────────────────────────────────────────────────

class OAuthCode(BaseModel):
    code: str


# ── Google ─────────────────────────────────────────────────────────────────────

@router.get("/google/url")
def google_login_url():
    """Step 1: Frontend opens this URL to start Google login."""
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        "&response_type=code"
        "&scope=openid email profile"
        f"&redirect_uri={FRONTEND_URL}/auth/google/callback"
        "&access_type=offline"
    )
    return {"url": url}

@router.post("/google/callback")
async def google_callback(body: OAuthCode, db: Session = Depends(get_db)):
    """Step 2: Frontend sends the code it received from Google."""
    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": body.code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": f"{FRONTEND_URL}/auth/google/callback",
                "grant_type": "authorization_code",
            }
        )
        token_data = token_res.json()
        if "error" in token_data:
            raise HTTPException(status_code=400, detail=token_data.get("error_description", "Google auth failed"))

        user_res = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"}
        )
        google_user = user_res.json()

    user = get_or_create_user_google(google_user, db)
    token = create_access_token({"sub": user.id})
    return {"access_token": token, "token_type": "bearer"}


# ── GitHub ─────────────────────────────────────────────────────────────────────

@router.get("/github/url")
def github_login_url():
    """Step 1: Frontend opens this URL to start GitHub login."""
    url = (
        "https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        "&scope=read:user user:email"
        f"&redirect_uri={FRONTEND_URL}/auth/github/callback"
    )
    return {"url": url}

@router.post("/github/callback")
async def github_callback(body: OAuthCode, db: Session = Depends(get_db)):
    """Step 2: Frontend sends the code it received from GitHub."""
    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": body.code,
                "redirect_uri": f"{FRONTEND_URL}/auth/github/callback",
            },
            headers={"Accept": "application/json"}
        )
        token_data = token_res.json()
        if "error" in token_data:
            raise HTTPException(status_code=400, detail=token_data.get("error_description", "GitHub auth failed"))

        access_token = token_data["access_token"]

        user_res = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        github_user = user_res.json()

        # Fetch primary email if not public on profile
        if not github_user.get("email"):
            email_res = await client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            emails = email_res.json()
            primary = next((e["email"] for e in emails if e.get("primary")), None)
            github_user["email"] = primary

    user = get_or_create_user_github(github_user, access_token, db)
    token = create_access_token({"sub": user.id})
    return {"access_token": token, "token_type": "bearer"}


# ── Me ─────────────────────────────────────────────────────────────────────────

@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    """Returns the currently logged-in user's profile."""
    return {
        "id": current_user.id,
        "email": current_user.email,
        "display_name": current_user.display_name,
        "avatar_url": current_user.avatar_url,
        "github_linked": current_user.github_id is not None,
        "google_linked": current_user.google_id is not None,
        "created_at": current_user.created_at,
    }