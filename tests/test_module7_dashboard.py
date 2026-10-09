"""Module 7 tests: the Control Center surfaces real endpoints only.

The dashboard must be a real client of the live API — no hard-coded sample
data. These tests verify the served assets reference the actual endpoints and
that those endpoints exist.
"""

from __future__ import annotations


def test_web_app_serves_control_center(client):
    page = client.get("/")
    assert page.status_code == 200
    assert 'data-panel="control"' in page.text
    assert 'id="panel-control"' in page.text


def test_app_js_targets_real_endpoints(client):
    js = client.get("/app.js")
    assert js.status_code == 200
    body = js.text
    for endpoint in (
        "/system/health/components",
        "/missions",
        "/notifications",
        "/sync/state",
        "/sync/conflicts",
        "/system/release",
    ):
        assert endpoint in body, f"dashboard must call the real endpoint {endpoint}"
    # No fabricated telemetry constants.
    assert "Math.random" not in body


def test_dashboard_endpoints_exist_in_openapi(client):
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    for path in (
        "/api/v1/system/health/components",
        "/api/v1/missions",
        "/api/v1/notifications",
        "/api/v1/sync/state",
        "/api/v1/sync/conflicts",
        "/api/v1/sync/conflicts/resolve",
        "/api/v1/system/release",
        "/api/v1/system/release/run",
        "/api/v1/security/tools",
        "/api/v1/security/policy",
        "/api/v1/memory/conflicts",
        "/api/v1/memory/duplicates",
    ):
        assert path in paths, f"missing dashboard dependency {path}"


def test_student_can_reach_control_data_but_not_release(client, student_session):
    from tests.conftest import auth_headers

    h = auth_headers(student_session["token"])
    # Student-legitimate panels load.
    assert client.get("/api/v1/missions", headers=h).status_code == 200
    assert client.get("/api/v1/notifications", headers=h).status_code == 200
    assert client.get("/api/v1/sync/state", headers=h).status_code == 200
    assert client.get("/api/v1/system/health/components", headers=h).status_code == 200
    # Release data is admin-gated and the dashboard handles the 403 honestly.
    assert client.get("/api/v1/system/release", headers=h).status_code == 403
