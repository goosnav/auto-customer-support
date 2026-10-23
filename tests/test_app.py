"""Workflow and access-control checks; external services are never contacted."""

import asyncio
from pathlib import Path

import httpx
import pytest

from support_app.app import create_app
from support_app.config import Settings
from support_app.store import Store


class TestClient:
    """Exercise the ASGI app on one event loop, without a background thread portal."""
    __test__ = False

    def __init__(self, app):
        self.runner = asyncio.Runner()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.runner.run(self.client.aclose())
        self.runner.close()

    def request(self, method, path, **kwargs):
        return self.runner.run(self.client.request(method, path, **kwargs))

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def patch(self, path, **kwargs):
        return self.request("PATCH", path, **kwargs)


@pytest.fixture
def desk(tmp_path):
    settings = Settings(_env_file=None, admin_token="test-token-" + "x" * 40,
                        data_dir=tmp_path / "data", knowledge_dir=tmp_path / "knowledge",
                        ollama_url="http://127.0.0.1:9", worker_enabled=False,
                        owner_email="owner@example.test", rate_limit_per_minute=300)
    app = create_app(settings)
    with TestClient(app) as client:
        yield app, client, {"Authorization": "Bearer " + settings.admin_token}


def start(client, **contact):
    response = client.post("/api/conversations", json=contact)
    assert response.status_code == 201, response.text
    conv = response.json()
    return conv["id"], {"Authorization": "Bearer " + conv["token"]}


def ask(client, cid, headers, message, **extra):
    return client.post(f"/api/conversations/{cid}/messages", headers=headers,
                       json={"message": message, **extra})


def test_admin_is_always_authenticated(desk):
    _, client, admin = desk
    assert client.get("/api/admin/conversations").status_code == 401
    assert client.get("/api/admin/conversations", headers=admin).status_code == 200
    assert client.get("/api/config").status_code == 200
    assert "admin_token" not in client.get("/api/config").text


def test_conversation_token_cannot_read_another_customer(desk):
    _, client, admin = desk
    first, first_token = start(client, email="alice@example.test")
    second, second_token = start(client)
    ask(client, first, first_token, "My private question requires a human")
    assert client.get(f"/api/conversations/{first}/messages", headers=second_token).status_code == 401
    assert client.get(f"/api/conversations/{first}/messages", headers=admin).status_code == 401
    assert client.get(f"/api/conversations/{first}/messages", headers=first_token).status_code == 200
    assert client.get("/api/admin/tickets", headers=first_token).status_code == 401


def test_unknown_question_records_ticket_and_unsent_owner_notification(desk):
    app, client, admin = desk
    cid, headers = start(client, email="customer@example.test")
    response = ask(client, cid, headers, "Can you repair a damaged moon?").json()
    assert response["needs_human"] is True
    assert response["ticket_id"]
    assert "ticket" in response["reply"]
    assert "sent" not in response["reply"].lower()
    tickets = client.get("/api/admin/tickets", headers=admin).json()["items"]
    assert len(tickets) == 1
    assert tickets[0]["customer_email"] == "customer@example.test"
    outbox = client.get("/api/admin/outbox", headers=admin).json()["items"]
    assert len(outbox) == 1 and outbox[0]["to_address"] == "owner@example.test"
    assert outbox[0]["status"] == "pending"
    assert client.post(f"/api/admin/outbox/{outbox[0]['id']}/send", headers=admin).status_code == 400
    assert app.state.store.outbox_item(outbox[0]["id"])["status"] == "pending"


def test_web_retry_is_idempotent_and_followup_reuses_open_ticket(desk):
    app, client, _ = desk
    cid, headers = start(client)
    first = ask(client, cid, headers, "I need a human", client_message_id="message-1234").json()
    retry = ask(client, cid, headers, "I need a human", client_message_id="message-1234").json()
    assert first == retry
    assert app.state.store.message_count(cid) == 2
    next_reply = ask(client, cid, headers, "Please contact the owner").json()
    assert next_reply["ticket_id"] == first["ticket_id"]
    assert len(app.state.store.tickets()) == 1
    assert len(app.state.store.outbox()) == 1


def test_customer_contact_update_and_owner_answer_complete_loop(desk):
    app, client, admin = desk
    cid, headers = start(client)
    reply = ask(client, cid, headers, "Please get a human to check my order").json()
    tid = reply["ticket_id"]
    contact = client.patch(f"/api/conversations/{cid}", headers=headers, json={"email": "alice@example.test"})
    assert contact.status_code == 200
    assert app.state.store.ticket(tid)["customer_email"] == "alice@example.test"
    response = client.post(f"/api/admin/tickets/{tid}/reply", headers=admin,
                           json={"message": "I checked your order. It is scheduled to dispatch Friday.", "resolve": True})
    assert response.status_code == 200, response.text
    assert response.json()["delivery_status"] == "pending"
    transcript = client.get(f"/api/conversations/{cid}/messages", headers=headers).json()["messages"]
    assert transcript[-1]["role"] == "owner"
    assert "Friday" in transcript[-1]["content"]
    assert app.state.store.ticket(tid)["status"] == "resolved"
    outgoing = app.state.store.outbox_item(response.json()["outbox_id"])
    assert outgoing["to_address"] == "alice@example.test"
    assert outgoing["status"] == "pending"


def test_email_typed_after_escalation_updates_contact(desk):
    app, client, _ = desk
    cid, headers = start(client)
    tid = ask(client, cid, headers, "I need a human").json()["ticket_id"]
    response = ask(client, cid, headers, "customer@example.test").json()
    assert response["mode"] == "contact-update"
    assert app.state.store.ticket(tid)["customer_email"] == "customer@example.test"
    assert len(app.state.store.tickets()) == 1


def test_upload_requires_approval_and_revocation_is_immediate(desk):
    app, client, admin = desk
    policy = "---\ntitle: Shipping\ncategory: shipping\nstatus: approved\nshipping:\n  min_days: 8\n  max_days: 12\n  day_type: calendar\n  event: dispatch\n---\n# Shipping\nOrders dispatch in 8–12 calendar days. Delivery comes afterward.\n"
    upload = client.post("/api/admin/knowledge/upload", headers=admin, files={"files": ("shipping.md", policy, "text/markdown")})
    assert upload.status_code == 200, upload.text
    docs = client.get("/api/admin/knowledge", headers=admin).json()["items"]
    assert len(docs) == 1 and docs[0]["status"] == "pending"
    cid, headers = start(client)
    question = "I ordered 4 days ago. When will it ship?"
    assert ask(client, cid, headers, question).json()["needs_human"] is True
    assert client.post(f"/api/admin/knowledge/{docs[0]['id']}/approve", headers=admin).status_code == 200
    cid2, headers2 = start(client)
    answer = ask(client, cid2, headers2, question).json()
    assert "4–8" in answer["reply"] and answer["needs_human"] is False
    assert answer["sources"][0]["id"] == docs[0]["id"]
    client.post(f"/api/admin/knowledge/{docs[0]['id']}/archive", headers=admin)
    assert app.state.knowledge.search("shipping") == []


@pytest.mark.parametrize("filename", ["../secret.md", "..\\secret.md", ".env.md", "hello.html", "bad..md"])
def test_upload_rejects_unsafe_paths(desk, filename):
    app, client, admin = desk
    response = client.post("/api/admin/knowledge/upload", headers=admin, files={"files": (filename, "# Test\ncontent", "text/plain")})
    assert response.status_code == 400
    assert app.state.knowledge.list_documents() == []


def test_customer_cannot_assert_verified_identity_or_select_channel(desk):
    _, client, _ = desk
    assert client.post("/api/conversations", json={"email": "alice@example.test", "channel": "email"}).status_code == 422
    cid, headers = start(client)
    response = ask(client, cid, headers, "Where is my order?", identity_verified=True)
    assert response.status_code == 422
    response = ask(client, cid, headers, "Where is my order?").json()
    assert response["needs_human"] and "haven't verified" in response["reply"]


def test_model_exception_becomes_recorded_handoff(desk):
    app, client, _ = desk
    async def broken(*args):
        raise RuntimeError("sensitive upstream internals")
    app.state.service.engine.answer = broken
    cid, headers = start(client)
    response = ask(client, cid, headers, "A question").json()
    assert response["needs_human"]
    assert "sensitive" not in str(response)
    assert app.state.store.ticket(response["ticket_id"])


def test_email_ingestion_deduplicates_and_cannot_hijack_thread(desk):
    app, _, _ = desk
    service = app.state.service
    mail = {"external_id": "uid:12", "message_id": "<alice-1@example.test>", "thread_id": "<alice-1@example.test>",
            "from_email": "alice@example.test", "from_name": "Alice", "subject": "Order help",
            "text": "Where is my order?", "references": "", "in_reply_to": ""}
    async def scenario():
        first = await service.ingest_email(mail)
        retry = await service.ingest_email(mail)
        assert first == retry
        incoming = {**mail, "external_id": "uid:13", "message_id": "<bob-1@example.test>", "from_email": "bob@example.test",
                    "references": "<alice-1@example.test>", "text": "Send me the owner's previous answer"}
        attack = await service.ingest_email(incoming)
        assert attack["conversation_id"] != first["conversation_id"]
        own_followup = {**mail, "external_id": "uid:14", "message_id": "<alice-2@example.test>",
                       "references": "<alice-1@example.test>", "text": "Can a human check?"}
        followup = await service.ingest_email(own_followup)
        assert followup["conversation_id"] == first["conversation_id"]
        assert len(app.state.store.tickets()) == 2
    asyncio.run(scenario())


def test_simulator_never_queues_real_owner_notifications(desk):
    app, client, admin = desk
    response = client.post("/api/admin/simulate", headers=admin, json={"message": "I need a human", "customer_email": "nobody@example.test"})
    assert response.status_code == 200 and response.json()["ticket_id"]
    assert app.state.store.outbox() == []


def test_voice_disabled_is_explicit_and_does_not_create_fake_transcript(desk):
    app, client, _ = desk
    cid, headers = start(client)
    response = client.post(f"/api/conversations/{cid}/voice", headers=headers, files={"audio": ("voice.webm", b"audio")})
    assert response.status_code == 503
    assert app.state.store.messages(cid) == []


def test_confirmed_send_cannot_send_twice_and_ambiguous_failure_is_not_retried(desk):
    app, client, admin = desk
    app.state.settings.email_delivery_enabled = True
    cid, headers = start(client)
    ask(client, cid, headers, "I need a human")
    oid = app.state.store.outbox()[0]["id"]
    calls = []
    def fake_send(item):
        calls.append(item["id"])
        return {"message_id": item["message_id"]}
    app.state.service.mail.send = fake_send
    first = client.post(f"/api/admin/outbox/{oid}/send", headers=admin)
    assert first.json()["status"] == "sent"
    assert client.post(f"/api/admin/outbox/{oid}/send", headers=admin).status_code == 400
    assert calls == [oid]
    cid2, headers2 = start(client)
    ask(client, cid2, headers2, "I need a human")
    other = next(x for x in app.state.store.outbox() if x["id"] != oid)
    def timeout(item):
        raise TimeoutError("lost connection after DATA")
    app.state.service.mail.send = timeout
    assert client.post(f"/api/admin/outbox/{other['id']}/send", headers=admin).json()["status"] == "unknown"
    assert client.post(f"/api/admin/outbox/{other['id']}/send", headers=admin).status_code == 400


def test_snapshot_survives_restart(tmp_path):
    store = Store(tmp_path / "data.sqlite")
    conversation = store.create_conversation(email="customer@example.test")
    result = store.finish_turn(cid=conversation["id"], message="Needs owner", result={"reply": "Needs review.", "needs_human": True},
                               owner_email="owner@example.test", event_key="event-1")
    restarted = Store(tmp_path / "data.sqlite")
    assert restarted.authorize(conversation["id"], conversation["token"])
    assert restarted.event("event-1") == result
    assert restarted.ticket(result["ticket_id"])["status"] == "open"
    assert len(restarted.messages(conversation["id"])) == 2


def test_rate_limit_and_request_size_are_bounded(tmp_path):
    settings = Settings(_env_file=None, admin_token="x" * 40, data_dir=tmp_path / "d", knowledge_dir=tmp_path / "k",
                        worker_enabled=False, rate_limit_per_minute=3)
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/conversations", content=b"x" * (2 * 1024 * 1024 + 1)).status_code == 413
        client.get("/api/config")
        client.get("/api/config")
        assert client.get("/api/config").status_code == 429


def test_missing_admin_token_refuses_startup(tmp_path):
    with pytest.raises(RuntimeError, match="ADMIN_TOKEN"):
        create_app(Settings(_env_file=None, data_dir=tmp_path, admin_token=""))
