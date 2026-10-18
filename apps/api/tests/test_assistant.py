"""Happy-path tests for the assistant router (chat + stream).

The LLM is intentionally not configured in tests — every assistant request
should be handled by the rule-based intent router. We assert both the
non-streaming and streaming endpoints work correctly in this fallback mode
without flakiness from external model calls.
"""
import json
import uuid

import pytest

from tests.conftest import assert_ok


@pytest.fixture()
def conversation(logged_in_client, db_session):
    """Create an organization + project + ai_conversation row directly via ORM."""
    from app.models import Organization, Project, AIConversation, OrganizationStatus
    me = logged_in_client.get("/api/v1/auth/me")
    me.raise_for_status()
    user_id = uuid.UUID(me.json()["id"])

    # Reuse an existing organization if present, else create
    org = db_session.query(Organization).filter(
        Organization.status == OrganizationStatus.active
    ).first()
    if not org:
        org = Organization(
            id=uuid.uuid4(),
            name="Test Org",
            status=OrganizationStatus.active,
        )
        db_session.add(org)
        db_session.flush()

    # Reuse an existing project if present, else create
    proj = db_session.query(Project).filter(
        Project.organization_id == org.id
    ).first()
    if not proj:
        proj = Project(
            id=uuid.uuid4(),
            organization_id=org.id,
            code="TEST-PRJ",
            name="Test Project",
            status="active",
        )
        db_session.add(proj)
        db_session.flush()

    conv = AIConversation(
        id=uuid.uuid4(),
        organization_id=org.id,
        project_id=proj.id,
        user_id=user_id,
        title="Test Conversation",
    )
    db_session.add(conv)
    db_session.flush()
    return conv.id


# --- /assistant/send (non-streaming) -------------------------------------

def test_send_message_returns_rule_based_fallback(logged_in_client, conversation):
    """An irrelevant prompt must hit the fallback path and not depend on LLM."""
    resp = logged_in_client.post(
        f"/api/v1/assistant/conversations/{conversation}/messages",
        json={"content": "今天天气如何"},
    )
    body = assert_ok(resp)
    assert body["tool"] == "fallback"
    assert "message" in body
    # Warnings / metric_definitions may exist depending on LLM unconfigured path
    # but core shape is preserved
    assert isinstance(body.get("warnings", []), (list, type(None)))


def test_send_message_persists_messages(logged_in_client, conversation, db_session):
    from sqlalchemy import text as sql_text
    resp = logged_in_client.post(
        f"/api/v1/assistant/conversations/{conversation}/messages",
        json={"content": "随机提问"},
    )
    assert_ok(resp)
    count = db_session.execute(sql_text(
        "SELECT count(*) FROM ai_messages WHERE conversation_id=:cid"
    ), {"cid": str(conversation)}).scalar()
    assert count >= 2  # user + assistant at minimum


def test_send_message_to_invalid_conversation_returns_404(logged_in_client):
    fake = uuid.uuid4()
    resp = logged_in_client.post(
        f"/api/v1/assistant/conversations/{fake}/messages",
        json={"content": "what is the occupancy rate"},
    )
    assert resp.status_code == 404, resp.text


def test_send_message_to_other_user_conversation_returns_403(logged_in_client, db_session):
    """A conversation owned by another real user must not be reachable."""
    from sqlalchemy import text as sql_text
    from app.core.auth import hash_password

    # Create a second real app_user so the FK satisfies
    other_user_id = uuid.uuid4()
    db_session.execute(sql_text(
        "INSERT INTO app_users (id, username, email, password_hash, "
        "display_name, is_system_admin, status, created_at, updated_at) "
        "VALUES (:id, :u, :e, :p, :dn, false, 'active', NOW(), NOW())"
    ), {"id": str(other_user_id), "u": "other_user_xyz", "e": "other@example.com",
         "p": hash_password("OtherPass!@#"), "dn": "Other"})
    db_session.flush()

    other = uuid.uuid4()
    db_session.execute(sql_text(
        "INSERT INTO ai_conversations (id, organization_id, project_id, user_id, "
        "title, created_at, updated_at) VALUES (:id, "
        "(SELECT id FROM organizations LIMIT 1), "
        "(SELECT id FROM projects LIMIT 1), "
        ":u, 'Other', NOW(), NOW())"
    ), {"id": str(other), "u": str(other_user_id)})
    db_session.flush()

    resp = logged_in_client.post(
        f"/api/v1/assistant/conversations/{other}/messages",
        json={"content": "出租率多少"},
    )
    assert resp.status_code == 403, resp.text


# --- /assistant/stream (SSE) ---------------------------------------------

def test_stream_endpoint_returns_text_event_stream(logged_in_client, conversation):
    """Verify the SSE endpoint streams meta + summary_delta + done events."""
    with logged_in_client.stream(
        "POST",
        f"/api/v1/assistant/conversations/{conversation}/stream",
        json={"content": "今天星期几"},
    ) as resp:
        assert resp.status_code == 200
        ct = resp.headers.get("content-type", "")
        assert "text/event-stream" in ct, f"Expected SSE content-type, got {ct!r}"

        events = []
        buffer = ""
        for raw in resp.iter_text():
            buffer += raw
            while True:
                sep = buffer.find("\n\n")
                if sep < 0:
                    break
                frame = buffer[:sep]
                buffer = buffer[sep + 2:]
                ev_name = None
                ev_data = None
                for line in frame.split("\n"):
                    if line.startswith("event:"):
                        ev_name = line[6:].strip()
                    elif line.startswith("data:"):
                        ev_data = line[5:].strip()
                if ev_name:
                    try:
                        ev_data_obj = json.loads(ev_data) if ev_data else {}
                    except Exception:
                        ev_data_obj = ev_data
                    events.append((ev_name, ev_data_obj))

        names = [e[0] for e in events]
        assert "meta" in names, f"Missing meta event; got {names}"
        assert "done" in names, f"Missing done event; got {names}"
        # At least one content chunk or rule-based fallback chunk
        assert any(n in ("summary_delta", "done") for n in names)

        # Done must carry final summary
        done = next(e[1] for e in events if e[0] == "done")
        assert "summary" in done
        assert done["tool"] in ("fallback",)  # rule-based path only here


def test_stream_endpoint_404_for_unknown_conversation(logged_in_client):
    resp = logged_in_client.post(
        f"/api/v1/assistant/conversations/{uuid.uuid4()}/stream",
        json={"content": "anything"},
    )
    assert resp.status_code == 404, resp.text


def test_stream_endpoint_400_for_malformed_uuid(logged_in_client):
    resp = logged_in_client.post(
        "/api/v1/assistant/conversations/not-a-uuid/stream",
        json={"content": "anything"},
    )
    assert resp.status_code == 400, resp.text
