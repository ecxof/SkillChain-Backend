from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class OAuthCode(BaseModel):
    """The authorization code the frontend received from Google or GitHub."""

    code: str = Field(min_length=1, max_length=512)


class AuthUrlOut(BaseModel):
    url: str


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class UserOut(BaseModel):
    """The signed-in user's own account. Never exposes the GitHub access token."""

    id: str
    email: str
    display_name: str | None
    avatar_url: str | None
    github_username: str | None
    github_linked: bool
    google_linked: bool
    profile_slug: str | None
    profile_is_public: bool
    created_at: datetime | None

    @classmethod
    def from_user(cls, user) -> "UserOut":
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            github_username=user.github_username,
            github_linked=user.github_id is not None,
            google_linked=user.google_id is not None,
            profile_slug=user.profile_slug,
            profile_is_public=bool(user.profile_is_public),
            created_at=user.created_at,
        )
