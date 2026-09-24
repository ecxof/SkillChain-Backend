import os
import httpx

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from db.database import get_db
from models.user import User
from schemas.auth import AuthUrlOut, OAuthCode, TokenOut, UserOut
from schemas.profiles import ProfileSettingsUpdate
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


# ── Google ─────────────────────────────────────────────────────────────────────

@router.get("/google/url", response_model=AuthUrlOut)
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
    return AuthUrlOut(url=url)

@router.post("/google/callback", response_model=TokenOut)
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
    return TokenOut(access_token=token)


# ── GitHub ─────────────────────────────────────────────────────────────────────

@router.get("/github/url", response_model=AuthUrlOut)
def github_login_url():
    """Step 1: Frontend opens this URL to start GitHub login."""
    url = (
        "https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        "&scope=read:user user:email"
        f"&redirect_uri={FRONTEND_URL}/auth/github/callback"
    )
    return AuthUrlOut(url=url)

@router.post("/github/callback", response_model=TokenOut)
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
    return TokenOut(access_token=token)


# ── Me ─────────────────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)):
    """Returns the currently logged-in user's profile."""
    return UserOut.from_user(current_user)


@router.patch("/me/profile", response_model=UserOut)
def update_profile_settings(
    body: ProfileSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Choose the public profile URL, and decide whether to publish it.

    Publishing is the one action that makes a developer's verified skills
    readable without an account, so it is always explicit and always
    reversible. Unpublishing keeps the slug reserved, so a profile that comes
    back later comes back at the same address.
    """
    settings = body.model_dump(exclude_unset=True)

    if "profile_slug" in settings:
        slug = settings["profile_slug"]
        taken = (
            db.query(User)
            .filter(User.profile_slug == slug, User.id != current_user.id)
            .first()
        )
        if taken is not None:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"The profile URL '{slug}' is already taken")
        current_user.profile_slug = slug

    if "profile_is_public" in settings:
        current_user.profile_is_public = settings["profile_is_public"]

    # A public profile with no slug has no address to be served at, so it
    # would silently be invisible. Refuse rather than accept a no-op.
    if current_user.profile_is_public and not current_user.profile_slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Choose a profile URL before making the profile public")

    db.commit()
    db.refresh(current_user)
    return UserOut.from_user(current_user)


@router.delete("/github/unlink", response_model=UserOut)
def unlink_github(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Forget the GitHub link and the stored access token.

    Refused when GitHub is the only way in, since removing it would lock the
    account out of itself. Analyses already run are unaffected; later ones
    fall back to unauthenticated access, which sees public repositories only
    and at a much lower rate limit.
    """
    if current_user.google_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "GitHub is the only way to sign in to this account; link Google first",
        )
    current_user.github_id = None
    current_user.github_username = None
    current_user.github_access_token = None
    db.commit()
    db.refresh(current_user)
    return UserOut.from_user(current_user)