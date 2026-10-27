"""Admin settings that control platform resources (currently task images)."""

from __future__ import annotations


def test_admin_can_set_and_read_task_image_retention(client, admin_headers):
    current = client.get("/api/system/settings", headers=admin_headers).json()
    original = current["keep_recent_task_images"]
    assert isinstance(original, int) and original >= 1
    try:
        updated = client.put(
            "/api/system/settings", headers=admin_headers,
            json={**current, "keep_recent_task_images": 7},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["keep_recent_task_images"] == 7
        assert client.get("/api/system/settings", headers=admin_headers).json()["keep_recent_task_images"] == 7
    finally:
        client.put(
            "/api/system/settings", headers=admin_headers,
            json={**current, "keep_recent_task_images": original},
        )


def test_only_admins_can_change_task_image_retention(client, member_headers):
    current = client.get("/api/system/settings", headers=member_headers).json()
    response = client.put(
        "/api/system/settings", headers=member_headers,
        json={**current, "keep_recent_task_images": 1},
    )
    assert response.status_code == 403
