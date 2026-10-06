"""Brain: wake word, context, memory, honest no-LLM fallback, rate limiting."""

from __future__ import annotations

from tests.conftest import auth_headers


def chat(client, session, message):
    resp = client.post(
        "/api/v1/brain/chat",
        headers=auth_headers(session["token"]),
        json={"message": message},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_wake_word_is_stripped(client, student_session):
    body = chat(client, student_session, "Hey Jarvis, what time is it?")
    assert body["text"]
    assert "Couldn't find" not in body["text"]


def test_greeting_and_help(client, student_session):
    assert "Hello" in chat(client, student_session, "hi")["text"]
    assert "remind me" in chat(client, student_session, "help")["text"]


def test_remember_and_recall_roundtrip(client, student_session):
    stored = chat(client, student_session, "remember course = BBA first year")
    assert "Remembered 'course'" in stored["text"]

    recalled = chat(client, student_session, "what do you remember about course")
    assert "BBA first year" in recalled["text"]


def test_remember_secret_is_refused_by_brain(client, student_session):
    body = chat(client, student_session, "remember wifi = password secret123")
    assert "can't store that" in body["text"]
    # And nothing was stored.
    empty = chat(client, student_session, "what do you remember about wifi")
    assert "couldn't find" in empty["text"].lower()


def test_context_is_maintained_across_turns(client, student_session):
    chat(client, student_session, "remember project = ALYO platform")
    body = chat(client, student_session, "what is my project?")
    assert "ALYO platform" in body["text"]
    assert body["context_used"] >= 1


def test_history_endpoint_returns_turns(client, student_session):
    chat(client, student_session, "hello")
    h = auth_headers(student_session["token"])
    turns = client.get("/api/v1/brain/history", headers=h).json()["turns"]
    assert len(turns) >= 2
    assert turns[0]["role"] == "user"
    assert turns[1]["role"] == "assistant"


def test_disabling_history_stops_retention(client, student_session):
    h = auth_headers(student_session["token"])
    chat(client, student_session, "hello")
    client.delete("/api/v1/brain/history", headers=h)
    client.put(
        "/api/v1/privacy/conversation-history", headers=h, json={"enabled": False}
    )
    chat(client, student_session, "second message")
    turns = client.get("/api/v1/brain/history", headers=h).json()["turns"]
    assert turns == []


def test_fallback_is_honest_about_no_llm(client, student_session):
    """With no provider configured, ALYO lists real capabilities, never invents."""
    body = chat(client, student_session, "write me a poem about the sea")
    assert body["source"] == "rules"
    assert "no AI model is configured" in body["text"]
    assert "ALYO_OPENAI_API_KEY" in body["text"]


def test_system_status_via_chat_is_real(client, student_session):
    body = chat(client, student_session, "system status")
    assert "CPU" in body["text"]
    assert body["tools"][0]["verified"] is True
    # Numbers come from psutil: plausible percentages.
    import re

    cpu = re.search(r"CPU (\d+(?:\.\d+)?)%", body["text"])
    assert cpu and 0 <= float(cpu.group(1)) <= 100


def test_memory_recalled_through_chat_does_not_leak_others(client, admin_session, student_session):
    chat(client, admin_session, "remember salary = 999999")
    body = chat(client, student_session, "what do you remember about salary")
    assert "999999" not in body["text"]


def test_chat_rate_limit_engages(client, student_session):
    h = auth_headers(student_session["token"])
    last = None
    for _ in range(35):
        last = client.post("/api/v1/brain/chat", headers=h, json={"message": "ping"})
    assert last is not None
    if last.status_code == 200 and "Too many messages" in last.json()["text"]:
        return  # limit reached inside handler
    # Otherwise the handler response should be the limit notice eventually.
    resp = client.post("/api/v1/brain/chat", headers=h, json={"message": "ping"})
    assert "Too many messages" in resp.json()["text"] or resp.status_code == 429


def test_empty_message_rejected(client, student_session):
    h = auth_headers(student_session["token"])
    resp = client.post("/api/v1/brain/chat", headers=h, json={"message": ""})
    assert resp.status_code == 422


def test_tool_catalog_exposes_permission_semantics(client, student_session):
    h = auth_headers(student_session["token"])
    body = client.get("/api/v1/brain/tools", headers=h).json()
    tools = {t["name"]: t for t in body["tools"]}
    assert "task.delete" in tools
    assert tools["task.delete"]["permission_level"] == "CONFIRMATION_REQUIRED"
    assert tools["task.delete"]["your_role_allowed"] is False  # STUDENT lacks task:delete
    assert tools["task.create"]["your_role_allowed"] is True
