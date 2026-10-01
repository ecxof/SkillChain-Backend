from datetime import datetime, timedelta, timezone
import hashlib
import hmac
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
# Long enough to read a consent screen and sign in, short enough that a state
# lifted from a log or a browser history is useless by the time it is found.
OAUTH_STATE_MINUTES = 10
OAUTH_STATE_PURPOSE = "oauth-state"

security = HTTPBearer()


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


class InvalidOAuthState(Exception):
    """The sign-in being completed is not the one this browser started."""

    def __init__(self):
        super().__init__("This sign-in has expired or was not started from this browser. "
                         "Start signing in again.")


def _nonce_hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def issue_oauth_state(provider: str, nonce: str) -> str:
    """The OAuth ``state`` value for a sign-in this browser is starting.

    Signed and short-lived, so it cannot be forged or kept for later. Signing
    alone would not stop login CSRF, though: an attacker could ask for a state
    of their own and plant it, with their own code, in a link for the victim to
    follow. So the state also carries the hash of a nonce that only the browser
    which started the sign-in holds, and the callback must present that nonce.
    The hash rather than the nonce goes into the state, because the state passes
    through the provider and lands in URLs and logs.
    """
    claims = {
        "purpose": OAUTH_STATE_PURPOSE,
        "provider": provider,
        "nonce_hash": _nonce_hash(nonce),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=OAUTH_STATE_MINUTES),
    }
    return jwt.encode(claims, SECRET_KEY, algorithm=ALGORITHM)


def check_oauth_state(state: str, provider: str, nonce: str) -> None:
    """Accept a callback only for a sign-in this browser started, recently.

    Raises :class:`InvalidOAuthState` for a forged, expired or tampered state,
    one issued for the other provider, a session token passed off as a state,
    or a nonce that is not the one the state was issued for.
    """
    try:
        claims = jwt.decode(state, SECRET_KEY, algorithms=[ALGORITHM],
                            options={"require": ["exp"]})
    except jwt.PyJWTError as exc:
        raise InvalidOAuthState() from exc
    if (claims.get("purpose") != OAUTH_STATE_PURPOSE
            or claims.get("provider") != provider
            or not hmac.compare_digest(str(claims.get("nonce_hash", "")), _nonce_hash(nonce))):
        raise InvalidOAuthState()


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