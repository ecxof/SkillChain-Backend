from datetime import datetime, timedelta, timezone
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Security
from sqlalchemy.orm import Session
from db.database import get_db
from models.user import User
import os, uuid

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60))

security = HTTPBearer()


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(security),
    db: Session = Depends(get_db)
) -> User:
    token = credentials.credentials
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception
    return user


class UnverifiedEmail(Exception):
    """The provider has not verified the address, so it cannot identify an account.

    An unverified address is only text someone typed into a profile. Linking on
    one would hand an existing account to whoever typed it; creating an account
    under one would let that person wait for the real owner to sign in with a
    provider that has verified it, and be linked into an account they control.
    """

    def __init__(self, provider: str):
        super().__init__(
            f"Signing in needs an email address that {provider} has verified. "
            f"Verify one with {provider}, then sign in again."
        )


def _verified_email(email: str | None, verified: bool | None, provider: str) -> str:
    """The address to match or create an account by, or UnverifiedEmail.

    Only reached for a provider identity SkillChain has not seen before; a
    returning user is found by the provider's own id and never gets here. The
    refusal does not depend on whether an account holds the address, so it
    tells a caller nothing about which addresses have accounts.
    """
    if not email or verified is not True:
        raise UnverifiedEmail(provider)
    return email


def get_or_create_user_google(google_data: dict, db: Session) -> User:
    google_id = google_data["id"]

    # Already has Google linked
    user = db.query(User).filter(User.google_id == google_id).first()
    if user:
        return user

    email = _verified_email(google_data.get("email"), google_data.get("verified_email"),
                            "Google")

    # Email exists — link Google to existing account
    user = db.query(User).filter(User.email == email).first()
    if user:
        user.google_id = google_id
        user.avatar_url = google_data.get("picture")
        db.commit()
        db.refresh(user)
        return user

    # Brand new user
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        google_id=google_id,
        display_name=google_data.get("name"),
        avatar_url=google_data.get("picture"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_or_create_user_github(github_data: dict, access_token: str, db: Session) -> User:
    github_id = str(github_data["id"])
    # Kept current on every login: a user can rename their GitHub account,
    # and the username is what repository URLs and public profiles show.
    github_username = github_data.get("login")

    # Already has GitHub linked — update token
    user = db.query(User).filter(User.github_id == github_id).first()
    if user:
        user.github_access_token = access_token
        user.github_username = github_username
        db.commit()
        db.refresh(user)
        return user

    email = _verified_email(github_data.get("email"), github_data.get("email_verified"),
                            "GitHub")

    # Email exists — link GitHub to existing account
    user = db.query(User).filter(User.email == email).first()
    if user:
        user.github_id = github_id
        user.github_access_token = access_token
        user.github_username = github_username
        db.commit()
        db.refresh(user)
        return user

    # Brand new user
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        github_id=github_id,
        github_username=github_username,
        github_access_token=access_token,
        display_name=github_data.get("name") or github_data.get("login"),
        avatar_url=github_data.get("avatar_url"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user