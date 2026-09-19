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
