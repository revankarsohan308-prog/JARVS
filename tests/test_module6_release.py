"""Module 6 tests: release gate truthfulness and deploy blocking."""

from __future__ import annotations

from core.release import gate
from core.release.gate import Check, run_gate
from tests.conftest import auth_headers


def test_secret_scan_is_clean_on_this_repo():
    report = run_gate(only=["secret_scan"])
    assert report["status"] == "PASS"
    assert report["deploy_blocked"] is False
    assert report["items"][0]["status"] == "PASS"


def test_secret_scan_detects_a_planted_secret(tmp_path):
    # Build the strings at runtime so this test file is not itself a finding.
    private_key = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
    aws_key = "AKIA" + "IOSFODNN7EXAMPLE"
    (tmp_path / "leak.py").write_text(
        f"PRIVATE = '{private_key}'\nAWS = '{aws_key}'\n", encoding="utf-8"
    )
    report = run_gate(only=["secret_scan"], repo_root=tmp_path)
    assert report["status"] == "FAIL"
    assert report["deploy_blocked"] is True  # secret scan is critical
    assert "leak.py" in report["items"][0]["detail"]
    # The matcher must never echo the value.
    assert aws_key not in report["items"][0]["detail"]


def test_keyword_secret_only_checked_in_config_files(tmp_path):
    (tmp_path / "code.py").write_text('token = "abcdefghijklmnop1234"\n', encoding="utf-8")
    (tmp_path / "creds.env").write_text('token = "abcdefghijklmnop1234"\n', encoding="utf-8")
    findings = gate._secret_scan(tmp_path)
    files = {f["file"] for f in findings}
    assert "creds.env" in files  # a real secret in a config file is caught
    assert "code.py" not in files  # source-code noise is not


def test_local_env_is_not_treated_as_release_content(tmp_path):
    (tmp_path / ".env").write_text('OPENAI_API_KEY = "abcdefghijklmnop1234"\n', encoding="utf-8")
    assert gate._secret_scan(tmp_path) == []


def test_migration_heads_pass_on_this_repo():
    report = run_gate(only=["migration_heads"])
    assert report["items"][0]["status"] == "PASS"


def test_migration_heads_detects_multiple_heads(tmp_path):
    versions = tmp_path / "database" / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "a.py").write_text("revision = 'a'\ndown_revision = None\n", encoding="utf-8")
    (versions / "b.py").write_text("revision = 'b'\ndown_revision = None\n", encoding="utf-8")
    report = run_gate(only=["migration_heads"], repo_root=tmp_path)
    assert report["items"][0]["status"] == "FAIL"
    # Non-critical: it reports but does not block the deploy alone.
    assert report["deploy_blocked"] is False


def test_not_configured_never_counts_as_pass_or_blocker(monkeypatch):
    def not_configured(_ctx):
        return gate._result(gate.NOT_CONFIGURED, "tool absent", critical=False)

    monkeypatch.setattr(gate, "CHECKS", (Check("x", "x", False, not_configured),))
    report = run_gate()
    assert report["status"] == "PASS"
    assert report["deploy_blocked"] is False
    assert report["items"][0]["status"] == "NOT_CONFIGURED"


def test_critical_failure_blocks_deploy():
    def failing_runner(_command, _cwd, _timeout):
        return 1, "1 failed, 5 passed"

    report = run_gate(only=["unit_tests"], runner=failing_runner)
    assert report["status"] == "FAIL"
    assert report["deploy_blocked"] is True
    assert "unit_tests" in report["blockers"]


def test_broken_check_is_a_failure_not_a_pass(monkeypatch):
    def boom(_ctx):
        raise RuntimeError("check exploded")

    monkeypatch.setattr(gate, "CHECKS", (Check("boom", "boom", True, boom),))
    report = run_gate()
    assert report["items"][0]["status"] == "FAIL"
    assert report["deploy_blocked"] is True


def test_skipped_is_reported_as_skipped_not_pass():
    report = run_gate(only=["secret_scan"], skip=["secret_scan"])
    assert report["items"][0]["status"] == "SKIPPED"


def test_release_status_requires_release_read(client, student_session):
    headers = auth_headers(student_session["token"])
    assert client.get("/api/v1/system/release", headers=headers).status_code == 403


def test_release_gate_api_runs_persists_and_audits(client, admin_session, db):
    headers = auth_headers(admin_session["token"])
    initial = client.get("/api/v1/system/release", headers=headers).json()
    assert initial["status"] == "NOT_RUN"

    resp = client.post(
        "/api/v1/system/release/run",
        headers=headers,
        json={"only": ["secret_scan", "migration_heads"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "PASS"
    assert body["deploy_blocked"] is False
    assert body.get("run_id")

    latest = client.get("/api/v1/system/release", headers=headers).json()
    assert latest["status"] == "PASS"
    assert latest["id"] == body["run_id"]

    from sqlalchemy import select

    from database.models import AuditLog, ReleaseRun

    assert db.scalar(select(ReleaseRun)) is not None
    assert db.scalar(select(AuditLog).where(AuditLog.event == "release_gate_run")) is not None
