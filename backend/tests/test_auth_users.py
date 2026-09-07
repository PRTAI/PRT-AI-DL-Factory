"""Auth + account management."""

from __future__ import annotations


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"


def test_login_rejects_bad_password(client):
    response = client.post(
        "/api/auth/login", json={"email": "admin@prt.local", "password": "wrong-password"}
    )
    assert response.status_code == 401


def test_me_requires_token(client):
    assert client.get("/api/auth/me").status_code == 401


def test_admin_sees_admin_role(client, admin_headers):
    body = client.get("/api/auth/me", headers=admin_headers).json()
    assert body["role"] == "admin"
    assert body["must_change_password"] is True


def test_member_cannot_list_users(client, member_headers):
    assert client.get("/api/users", headers=member_headers).status_code == 403


def test_admin_can_list_users(client, admin_headers, member_headers):
    del member_headers
    emails = {u["email"] for u in client.get("/api/users", headers=admin_headers).json()}
    assert {"admin@prt.local", "dev@prt.local"} <= emails


def test_duplicate_email_rejected(client, admin_headers):
    response = client.post(
        "/api/users",
        headers=admin_headers,
        json={"email": "dev@prt.local", "password": "another-pass-1", "role": "user"},
    )
    assert response.status_code == 409


def test_internal_tld_email_accepted(client, admin_headers):
    response = client.post(
        "/api/users",
        headers=admin_headers,
        json={"email": "Ops@Corp.Internal", "password": "ops-password-1", "role": "user"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["email"] == "ops@corp.internal"


def test_change_own_password(client, admin_headers):
    created = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "email": "rotate@prt.local",
            "password": "first-password-1",
            "role": "user",
            "must_change_password": True,
        },
    )
    assert created.status_code == 201
    login = client.post(
        "/api/auth/login", json={"email": "rotate@prt.local", "password": "first-password-1"}
    ).json()
    headers = {"Authorization": f"Bearer {login['access_token']}"}
    assert login["user"]["must_change_password"] is True

    changed = client.post(
        "/api/auth/change-password",
        headers=headers,
        json={"current_password": "first-password-1", "new_password": "second-password-2"},
    )
    assert changed.status_code == 200
    assert changed.json()["must_change_password"] is False
    assert (
        client.post(
            "/api/auth/login", json={"email": "rotate@prt.local", "password": "second-password-2"}
        ).status_code
        == 200
    )


def test_admin_cannot_delete_self(client, admin_headers):
    me = client.get("/api/auth/me", headers=admin_headers).json()
    assert client.delete(f"/api/users/{me['id']}", headers=admin_headers).status_code == 400


def test_audit_trail_records_login(client, admin_headers):
    events = client.get("/api/audit?limit=200", headers=admin_headers).json()
    assert any(event["action"] == "auth.login" for event in events)
