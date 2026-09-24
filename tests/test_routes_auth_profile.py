import os

os.environ.setdefault("DATABASE_URL", "sqlite://")


# --- Choosing a profile URL ---------------------------------------------------

def test_a_slug_can_be_claimed(client, auth_headers):
    body = client.patch("/auth/me/profile", json={"profile_slug": "ada-lovelace"},
                        headers=auth_headers).json()

    assert body["profile_slug"] == "ada-lovelace"
    # Claiming an address is not publishing at it.
    assert body["profile_is_public"] is False


def test_a_slug_already_taken_is_refused(client, auth_headers, db_session, other_user):
    other_user.profile_slug = "ada-lovelace"
    db_session.commit()

    response = client.patch("/auth/me/profile", json={"profile_slug": "ada-lovelace"},
                            headers=auth_headers)

    assert response.status_code == 409


def test_keeping_your_own_slug_is_not_a_collision(client, auth_headers):
    client.patch("/auth/me/profile", json={"profile_slug": "ada"}, headers=auth_headers)

    response = client.patch("/auth/me/profile", json={"profile_slug": "ada"},
                            headers=auth_headers)

    assert response.status_code == 200


def test_a_reserved_slug_is_refused(client, auth_headers):
    response = client.patch("/auth/me/profile", json={"profile_slug": "settings"},
                            headers=auth_headers)

    # Reserved words would let a profile shadow a frontend page.
    assert response.status_code == 422


def test_a_malformed_slug_is_refused(client, auth_headers):
    response = client.patch("/auth/me/profile", json={"profile_slug": "Ada Lovelace!"},
                            headers=auth_headers)
    assert response.status_code == 422


# --- Publishing ---------------------------------------------------------------

def test_publishing_without_an_address_is_refused(client, auth_headers):
    response = client.patch("/auth/me/profile", json={"profile_is_public": True},
                            headers=auth_headers)

    # A public profile with no slug is served nowhere, so accepting this would
    # be a silent no-op.
    assert response.status_code == 400


def test_a_profile_can_be_published_and_taken_down_again(client, auth_headers):
    published = client.patch(
        "/auth/me/profile",
        json={"profile_slug": "ada", "profile_is_public": True},
        headers=auth_headers,
    ).json()
    assert published["profile_is_public"] is True

    withdrawn = client.patch("/auth/me/profile", json={"profile_is_public": False},
                             headers=auth_headers).json()

    assert withdrawn["profile_is_public"] is False
    # The address stays reserved, so a profile that returns returns in place.
    assert withdrawn["profile_slug"] == "ada"


def test_fields_left_out_are_left_alone(client, auth_headers):
    client.patch("/auth/me/profile",
                 json={"profile_slug": "ada", "profile_is_public": True},
                 headers=auth_headers)

    body = client.patch("/auth/me/profile", json={}, headers=auth_headers).json()

    assert body["profile_slug"] == "ada"
    assert body["profile_is_public"] is True


def test_anonymous_callers_cannot_change_profile_settings(client):
    response = client.patch("/auth/me/profile", json={"profile_slug": "ada"})
    assert response.status_code in (401, 403)


# --- Unlinking GitHub ---------------------------------------------------------

def test_unlinking_github_forgets_the_account_and_the_token(
    client, auth_headers, db_session, user
):
    user.google_id = "google-123"
    user.github_id = "github-456"
    user.github_access_token = "gho_secret"
    db_session.commit()

    body = client.delete("/auth/github/unlink", headers=auth_headers).json()

    assert body["github_linked"] is False
    assert body["github_username"] is None
    db_session.refresh(user)
    assert user.github_access_token is None


def test_unlinking_the_only_way_in_is_refused(client, auth_headers, db_session, user):
    user.github_id = "github-456"
    user.google_id = None
    db_session.commit()

    response = client.delete("/auth/github/unlink", headers=auth_headers)

    # Removing it would lock the account out of itself.
    assert response.status_code == 409
    db_session.refresh(user)
    assert user.github_id == "github-456"


def test_the_token_is_never_returned_by_the_account_endpoint(
    client, auth_headers, db_session, user
):
    user.github_access_token = "gho_secret"
    db_session.commit()

    body = client.get("/auth/me", headers=auth_headers).json()

    assert "github_access_token" not in body
    assert "gho_secret" not in str(body)
