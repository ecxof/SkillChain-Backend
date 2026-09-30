import uuid

import httpx
import respx

from models.user import User
from services.auth_service import create_access_token


def test_login_urls_are_returned_in_the_documented_shape(client):
    for provider, host in [("google", "accounts.google.com"), ("github", "github.com")]:
        response = client.get(f"/auth/{provider}/url")
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

    response = client.post("/auth/github/callback", json={"code": "abc123"})

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

    response = client.post("/auth/google/callback", json={"code": "c"})

    assert response.status_code == 200
    [account] = accounts(db_session)
    assert (account.id, account.google_id) == (owner.id, "g-7")


@respx.mock
def test_an_unverified_google_address_never_joins_an_existing_account(client, db_session):
    existing(db_session, email="ada@example.test", github_id="583231")
    google_says(email="ada@example.test", verified_email=False)

    response = client.post("/auth/google/callback", json={"code": "c"})

    # Anyone can type an address into a profile; linking on one that was
    # never verified would hand them the account that holds it.
    assert response.status_code == 403
    [account] = accounts(db_session)
    assert account.google_id is None


@respx.mock
def test_an_unverified_google_address_does_not_open_an_account_either(client, db_session):
    google_says(email="ada@example.test", verified_email=False)

    response = client.post("/auth/google/callback", json={"code": "c"})

    # Otherwise the address could be claimed in advance, and its real owner
    # linked into that account the first time they signed in verified.
    assert response.status_code == 403
    assert accounts(db_session) == []


@respx.mock
def test_a_returning_google_user_is_known_by_their_google_id_alone(client, db_session):
    owner = existing(db_session, email="ada@example.test", google_id="g-7")
    google_says(email="ada@example.test", verified_email=False)

    response = client.post("/auth/google/callback", json={"code": "c"})

    assert response.status_code == 200
    token = response.json()["access_token"]
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["id"] == owner.id


@respx.mock
def test_a_verified_github_address_joins_the_account_that_holds_it(client, db_session):
    owner = existing(db_session, email="ada@example.test", google_id="g-7")
    github_says([{"email": "ada@example.test", "primary": True, "verified": True}])

    response = client.post("/auth/github/callback", json={"code": "c"})

    assert response.status_code == 200
    [account] = accounts(db_session)
    assert (account.id, account.github_id, account.github_access_token) == (
        owner.id, "583231", "gho_new")


@respx.mock
def test_an_unverified_github_address_never_joins_an_existing_account(client, db_session):
    existing(db_session, email="ada@example.test", google_id="g-7")
    github_says([{"email": "ada@example.test", "primary": True, "verified": False}])

    response = client.post("/auth/github/callback", json={"code": "c"})

    assert response.status_code == 403
    [account] = accounts(db_session)
    assert (account.github_id, account.github_access_token) == (None, None)


@respx.mock
def test_a_public_github_address_is_checked_against_the_accounts_own_list(client, db_session):
    existing(db_session, email="ada@example.test", google_id="g-7")
    github_says([{"email": "ada@example.test", "primary": True, "verified": False}],
                email="ada@example.test")

    response = client.post("/auth/github/callback", json={"code": "c"})

    # Showing an address on a profile says nothing about whether it was
    # verified; only the account's email list does.
    assert response.status_code == 403
    assert accounts(db_session)[0].github_id is None


@respx.mock
def test_a_github_account_with_no_verified_address_gets_no_stand_in_address(client, db_session):
    github_says([])

    response = client.post("/auth/github/callback", json={"code": "c"})

    # A stand-in made from the login would be claimable by whoever takes the
    # login next, since GitHub lets names be changed and reused.
    assert response.status_code == 403
    assert accounts(db_session) == []


@respx.mock
def test_the_refusal_is_the_same_whether_or_not_the_address_has_an_account(
    client, db_session
):
    github_says([{"email": "ada@example.test", "primary": True, "verified": False}])
    without = client.post("/auth/github/callback", json={"code": "c"})

    existing(db_session, email="ada@example.test", google_id="g-7")
    with_account = client.post("/auth/github/callback", json={"code": "c"})

    # Anything else would let a caller probe which addresses hold accounts.
    assert (without.status_code, without.json()) == (with_account.status_code, with_account.json())


@respx.mock
def test_a_returning_github_user_is_known_by_their_github_id_alone(client, db_session):
    owner = existing(db_session, email="ada@example.test", github_id="583231",
                     github_access_token="gho_old")
    github_says([{"email": "ada@example.test", "primary": True, "verified": False}])

    response = client.post("/auth/github/callback", json={"code": "c"})

    assert response.status_code == 200
    [account] = accounts(db_session)
    assert (account.id, account.github_access_token) == (owner.id, "gho_new")
