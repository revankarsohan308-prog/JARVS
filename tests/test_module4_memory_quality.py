"""Module 4 tests: memory quality metadata, conflicts, duplicates, merge.

Contradictions must never be silent: the prior value is preserved as a revision
and the memory is flagged CONFLICT until the user decides.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from core.memory import service as memory
from database.models import Memory, User
from tests.conftest import auth_headers


def _user(db, username: str) -> User:
    return db.scalar(select(User).where(User.username == username))


def test_store_sets_quality_metadata(client, db, student_session):
    user = _user(db, "sohan")
    row = memory.store(
        db, user.id, "gpa", "8.4", confidence=0.7, importance=4, source_ref="transcript",
    )
    assert row.confidence == 0.7
    assert row.importance == 4
    assert row.verification_state == "UNVERIFIED"
    assert row.source_ref == "transcript"
    assert row.retention_policy == "KEEP"

    h = auth_headers(student_session["token"])
    listed = client.get("/api/v1/memory", headers=h).json()
    item = next(m for m in listed["memories"] if m["key"] == "gpa")
    assert item["confidence"] == 0.7
    assert item["importance"] == 4
    assert item["verification_state"] == "UNVERIFIED"
    assert listed["stats"]["total"] >= 1


def test_overwrite_differing_value_is_flagged_not_silent(client, db, student_session):
    user = _user(db, "sohan")
    memory.store(db, user.id, "mood", "tired")
    updated = memory.store(db, user.id, "mood", "energetic")

    assert updated.verification_state == "CONFLICT"
    assert updated.content == "energetic"
    # The prior value survives as a revision.
    revs = memory.revisions(db, user.id, updated.id)
    assert revs and revs[0].content == "tired"
    # No duplicate row was created.
    assert len([m for m in memory.list_memories(db, user.id) if m.key == "mood"]) == 1

    h = auth_headers(student_session["token"])
    resp = client.post("/api/v1/memory", headers=h, json={"key": "mood", "content": "focused"})
    assert resp.status_code == 201
    assert resp.json()["conflict"] is True


def test_conflict_can_be_resolved_by_keeping_previous(client, db, student_session):
    user = _user(db, "sohan")
    memory.store(db, user.id, "city", "Pune")
    row = memory.store(db, user.id, "city", "Bengaluru")
    assert row.verification_state == "CONFLICT"

    h = auth_headers(student_session["token"])
    resp = client.post(
        f"/api/v1/memory/{row.id}/resolve-conflict", headers=h, json={"keep": "previous"}
    )
    assert resp.status_code == 200
    assert resp.json()["memory"]["content"] == "Pune"
    assert resp.json()["memory"]["verification_state"] == "USER_VERIFIED"
    assert memory.conflicts(db, user.id) == []


def test_correction_records_revision_and_verifies(client, db, student_session):
    user = _user(db, "sohan")
    row = memory.store(db, user.id, "phone", "12345")
    h = auth_headers(student_session["token"])
    resp = client.post(
        f"/api/v1/memory/{row.id}/correct", headers=h, json={"content": "99999"}
    )
    assert resp.status_code == 200
    assert resp.json()["memory"]["verification_state"] == "USER_VERIFIED"
    assert resp.json()["memory"]["confidence"] == 1.0

    revs = client.get(f"/api/v1/memory/{row.id}/revisions", headers=h).json()["revisions"]
    assert revs[0]["reason"] == "correction"
    assert revs[0]["content"] == "12345"


def test_revert_restores_a_previous_revision(client, db, student_session):
    user = _user(db, "sohan")
    row = memory.store(db, user.id, "lang", "Kannada")
    memory.update(db, user.id, row.id, content="Hindi")
    revs = memory.revisions(db, user.id, row.id)
    oldest = revs[-1]  # the original "Kannada"

    h = auth_headers(student_session["token"])
    resp = client.post(
        f"/api/v1/memory/{row.id}/revert", headers=h, json={"revision_id": oldest.id}
    )
    assert resp.status_code == 200
    assert resp.json()["memory"]["content"] == "Kannada"


def test_duplicate_detection_and_user_approved_merge(client, db, student_session):
    user = _user(db, "sohan")
    a = memory.store(db, user.id, "fav_food", "I really enjoy eating masala dosa a lot")
    b = memory.store(db, user.id, "dosa", "I really enjoy eating masala dosa a lot!!")
    memory.store(db, user.id, "unrelated", "totally different subject matter")

    h = auth_headers(student_session["token"])
    a_id, b_id = a.id, b.id
    groups = client.get("/api/v1/memory/duplicates", headers=h, params={"threshold": 0.8}).json()["groups"]
    assert groups, "near-duplicate pair should be detected"
    ids = {m["id"] for m in groups[0]["memories"]}
    assert {a_id, b_id} <= ids

    resp = client.post(
        "/api/v1/memory/merge",
        headers=h,
        json={"target_id": a_id, "source_ids": [b_id], "merged_content": "Loves masala dosa"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["memory"]["content"] == "Loves masala dosa"
    # The source was merged away but preserved as a revision.
    db.expire_all()
    assert db.scalar(select(Memory).where(Memory.id == b_id)) is None
    revs = memory.revisions(db, user.id, a_id)
    assert any("masala dosa" in r.content for r in revs)


def test_retention_purge_removes_only_expired_memories(client, db, student_session):
    from timeutil import utcnow

    user = _user(db, "sohan")
    keep = memory.store(db, user.id, "keepme", "stays")
    expired = memory.store(
        db, user.id, "temp", "goes", retention_policy="WINDOW",
        expires_at=utcnow() - timedelta(days=1),
    )

    removed = memory.purge_expired(db)
    assert removed == 1
    assert db.get(Memory, keep.id) is not None
    assert db.get(Memory, expired.id) is None


def test_quality_stats_report_conflicts_and_revisions(db, student_session):
    user = _user(db, "sohan")
    memory.store(db, user.id, "a", "one")
    memory.store(db, user.id, "a", "two")  # conflict + revision
    stats = memory.stats(db, user.id)
    assert stats["conflicts"] == 1
    assert stats["revisions"] >= 1
    assert stats["by_verification"].get("CONFLICT") == 1


def test_conflicts_and_duplicates_are_user_scoped(client, db, admin_session, student_session):
    admin = _user(db, "admin")
    sohan = _user(db, "sohan")
    memory.store(db, admin.id, "x", "admin only note")
    memory.store(db, sohan.id, "x", "student fact")

    assert all(m.user_id == sohan.id for m in memory.conflicts(db, sohan.id))
    groups = memory.find_duplicates(db, sohan.id, threshold=0.5)
    for group in groups:
        for m in group["memories"]:
            assert db.get(Memory, m["id"]).user_id == sohan.id


def test_secret_still_rejected_with_quality_fields(client, db, student_session):
    h = auth_headers(student_session["token"])
    resp = client.post(
        "/api/v1/memory",
        headers=h,
        json={"key": "wifi", "content": "password hunter2", "importance": 5},
    )
    assert resp.status_code == 422
    assert db.scalar(select(Memory).where(Memory.key == "wifi")) is None
