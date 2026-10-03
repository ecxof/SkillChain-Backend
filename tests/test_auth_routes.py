import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import respx

from models.user import User
from services import auth_service
from services.auth_service import create_access_token

NONCE = "browser-kept-nonce-0123456789"


def started(client, provider, nonce=NONCE, code="c"):
    """The callback body for a sign-in this browser started, as the frontend sends it."""
    url = client.get(f"/auth/{provider}/url", params={"nonce": nonce}).json()["url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    return {"code": code, "state": state, "nonce": nonce}


def test_login_urls_are_returned_in_the_documented_shape(client):
    for provider, host in [("google", "accounts.google.com"), ("github", "github.com")]:
        response = client.get(f"/auth/{provider}/url", params={"nonce": NONCE})
        assert response.status_code == 200
        assert list(response.json()) == ["url"]
        assert host in response.json()["url"]


def test_me_requires_a_token(client):
    assert client.get("/auth/me").status_code == 401


def test_me_returns_the_account_without_the_github_token(client, db_session):
    db_session.add(User(id="u1", email="octo@example.test", github_id="42",
                        github_username="octocat", github_access_token="gho_secret"))
    db_session.commit()
    token = create_access_token({"sub": "u1"})

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert body["github_username"] == "octocat"
    assert (body["github_linked"], body["google_linked"]) == (True, False)
    assert body["profile_is_public"] is False
    assert "gho_secret" not in response.text


def test_callback_rejects_an_empty_code(client):
    assert client.post("/auth/github/callback", json={"code": ""}).status_code == 422


@respx.mock
def test_github_callback_creates_the_user_and_issues_a_working_token(client, db_session):
    respx.post("https://github.com/login/oauth/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "gho_fresh"}))
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={
            "id": 583231, "login": "octocat", "name": "The Octocat",
            "email": None, "avatar_url": "https://example.test/octocat.png"}))
    respx.get("https://api.github.com/user/emails").mock(
        return_value=httpx.Response(200, json=[
            {"email": "other@example.test", "primary": False, "verified": True},
            {"email": "octocat@example.test", "primary": True, "verified": True}]))

    response = client.post("/auth/github/callback", json=started(client, "github", code="abc123"))

    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    user = db_session.query(User).one()
    assert (user.email, user.github_username, user.github_access_token) == (
        "octocat@example.test", "octocat", "gho_fresh")

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {response.json()['access_token']}"})
    assert me.json()["id"] == user.id


def test_openapi_documents_the_typed_auth_responses(client):
    paths = client.get("/openapi.json").json()["paths"]
    me_schema = paths["/auth/me"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert me_schema == {"$ref": "#/components/schemas/UserOut"}


# --- Linking by email --------------------------------------------------------

GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USER = "https://www.googleapis.com/oauth2/v2/userinfo"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_USER = "https://api.github.com/user"
GITHUB_EMAILS = "https://api.github.com/user/emails"


def google_says(**user):
    respx.post(GOOGLE_TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "ya29"}))
    respx.get(GOOGLE_USER).mock(return_value=httpx.Response(200, json={
        "id": "g-7", "name": "Ada Lovelace", "picture": None, **user}))


def github_says(emails, **user):
    respx.post(GITHUB_TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "gho_new"}))
    respx.get(GITHUB_USER).mock(return_value=httpx.Response(200, json={
        "id": 583231, "login": "ada", "name": "Ada Lovelace", "email": None, **user}))
    respx.get(GITHUB_EMAILS).mock(return_value=httpx.Response(200, json=emails))


def existing(db_session, **fields):
    record = User(id=str(uuid.uuid4()), **fields)
    db_session.add(record)
    db_session.commit()
    return record


def accounts(db_session):
    db_session.expire_all()
    return db_session.query(User).all()


@respx.mock
def test_a_verified_google_address_joins_the_account_that_holds_it(client, db_session):
    owner = existing(db_session, email="ada@example.test", github_id="583231")
    google_says(email="ada@example.test", verified_email=True)

    response = client.post("/auth/google/callback", json=started(client, "google"))

    assert response.status_code == 200
    [account] = accounts(db_session)
    assert (account.id, account.google_id) == (owner.id, "g-7")


@respx.mock
def test_an_unverified_google_address_never_joins_an_existing_account(client, db_session):
    existing(db_session, email="ada@example.test", github_id="583231")
    google_says(email="ada@example.test", verified_email=False)

    response = client.post("/auth/google/callback", json=started(client, "google"))

    # Anyone can type an address into a profile; linking on one that was
    # never verified would hand them the account that holds it.
    assert response.status_code == 403
    [account] = accounts(db_session)
    assert account.google_id is None


@respx.mock
def test_an_unverified_google_address_does_not_open_an_account_either(client, db_session):
    google_says(email="ada@example.test", verified_email=False)

    response = client.post("/auth/google/callback", json=started(client, "google"))

    # Otherwise the address could be claimed in advance, and its real owner
    # linked into that account the first time they signed in verified.
    assert response.status_code == 403
    assert accounts(db_session) == []


@respx.mock
def test_a_returning_google_user_is_known_by_their_google_id_alone(client, db_session):
    owner = existing(db_session, email="ada@example.test", google_id="g-7")
    google_says(email="ada@example.test", verified_email=False)

    response = client.post("/auth/google/callback", json=started(client, "google"))

    assert response.status_code == 200
    token = response.json()["access_token"]
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["id"] == owner.id


@respx.mock
def test_a_verified_github_address_joins_the_account_that_holds_it(client, db_session):
    owner = existing(db_session, email="ada@example.test", google_id="g-7")
    github_says([{"email": "ada@example.test", "primary": True, "verified": True}])

    response = client.post("/auth/github/callback", json=started(client, "github"))

    assert response.status_code == 200
    [account] = accounts(db_session)
    assert (account.id, account.github_id, account.github_access_token) == (
        owner.id, "583231", "gho_new")


@respx.mock
def test_an_unverified_github_address_never_joins_an_existing_account(client, db_session):
    existing(db_session, email="ada@example.test", google_id="g-7")
    github_says([{"email": "ada@example.test", "primary": True, "verified": False}])

    response = client.post("/auth/github/callback", json=started(client, "github"))

    assert response.status_code == 403
    [account] = accounts(db_session)
    assert (account.github_id, account.github_access_token) == (None, None)


@respx.mock
def test_a_public_github_address_is_checked_against_the_accounts_own_list(client, db_session):
    existing(db_session, email="ada@example.test", google_id="g-7")
    github_says([{"email": "ada@example.test", "primary": True, "verified": False}],
                email="ada@example.test")

    response = client.post("/auth/github/callback", json=started(client, "github"))

    # Showing an address on a profile says nothing about whether it was
    # verified; only the account's email list does.
    assert response.status_code == 403
    assert accounts(db_session)[0].github_id is None


@respx.mock
def test_a_github_account_with_no_verified_address_gets_no_stand_in_address(client, db_session):
    github_says([])

    response = client.post("/auth/github/callback", json=started(client, "github"))

    # A stand-in made from the login would be claimable by whoever takes the
    # login next, since GitHub lets names be changed and reused.
    assert response.status_code == 403
    assert accounts(db_session) == []


@respx.mock
def test_the_refusal_is_the_same_whether_or_not_the_address_has_an_account(
    client, db_session
):
    github_says([{"email": "ada@example.test", "primary": True, "verified": False}])
    without = client.post("/auth/github/callback", json=started(client, "github"))

    existing(db_session, email="ada@example.test", google_id="g-7")
    with_account = client.post("/auth/github/callback", json=started(client, "github"))

    # Anything else would let a caller probe which addresses hold accounts.
    assert (without.status_code, without.json()) == (with_account.status_code, with_account.json())


@respx.mock
def test_a_returning_github_user_is_known_by_their_github_id_alone(client, db_session):
    owner = existing(db_session, email="ada@example.test", github_id="583231",
                     github_access_token="gho_old")
    github_says([{"email": "ada@example.test", "primary": True, "verified": False}])

    response = client.post("/auth/github/callback", json=started(client, "github"))

    assert response.status_code == 200
    [account] = accounts(db_session)
    assert (account.id, account.github_access_token) == (owner.id, "gho_new")


# --- The OAuth state ---------------------------------------------------------

def test_a_sign_in_cannot_start_without_a_nonce_to_bind_it_to(client):
    for provider in ("google", "github"):
        assert client.get(f"/auth/{provider}/url").status_code == 422
        assert client.get(f"/auth/{provider}/url", params={"nonce": "short"}).status_code == 422


def test_the_state_carries_a_hash_of_the_nonce_never_the_nonce(client):
    state = started(client, "github")["state"]

    # The state passes through the provider and into URLs and logs; the nonce
    # stays in the browser that started the sign-in.
    assert NONCE not in jwt.decode(state, options={"verify_signature": False}).values()


def test_a_callback_without_a_state_is_refused(client):
    assert client.post("/auth/github/callback", json={"code": "c", "nonce": NONCE}).status_code == 422


@respx.mock
def test_a_state_planted_from_another_browser_is_refused_before_the_code_is_used(client):
    exchange = respx.post(GITHUB_TOKEN).mock(
        return_value=httpx.Response(200, json={"access_token": "gho_attacker"}))
    # The attacker starts a sign-in of their own and plants its state and their
    # code in a link; the victim's browser holds a different nonce.
    planted = started(client, "github", nonce="attackers-own-nonce-0000")
    planted["nonce"] = NONCE

    response = client.post("/auth/github/callback", json=planted)

    assert response.status_code == 400
    # Refused before GitHub was asked for anything.
    assert not exchange.called


def test_a_state_for_one_provider_is_refused_by_the_other(client):
    body = started(client, "google")

    assert client.post("/auth/github/callback", json=body).status_code == 400


def test_an_expired_state_is_refused(client, monkeypatch):
    monkeypatch.setattr(auth_service, "OAUTH_STATE_MINUTES", -1)
    body = started(client, "github")

    assert client.post("/auth/github/callback", json=body).status_code == 400


def test_a_state_signed_with_any_other_key_is_refused(client):
    body = started(client, "github")
    claims = jwt.decode(body["state"], options={"verify_signature": False})
    body["state"] = jwt.encode(claims, "a-different-key-that-is-32-bytes-long!", algorithm="HS256")

    assert client.post("/auth/github/callback", json=body).status_code == 400


def test_a_session_token_is_not_accepted_as_a_state(client):
    body = started(client, "github")
    body["state"] = create_access_token({"sub": "someone"})

    assert client.post("/auth/github/callback", json=body).status_code == 400


def test_a_state_is_not_accepted_as_a_session_token(client):
    state = started(client, "github")["state"]

    assert client.get("/auth/me", headers={"Authorization": f"Bearer {state}"}).status_code == 401
