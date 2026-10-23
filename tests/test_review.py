"""Regression coverage for confirmed handoff, threading, and stale-source defects."""

import json
import re
from types import SimpleNamespace

import httpx
import pytest

from support_app.config import Settings
from support_app.engine import AssistantEngine
from support_app.knowledge import KnowledgeBase
from support_app.service import SupportService
from support_app.store import Store


@pytest.fixture
def desk(tmp_path):
    settings = Settings(_env_file=None, admin_token="x" * 40,
                        data_dir=tmp_path / "data", knowledge_dir=tmp_path / "knowledge",
                        worker_enabled=False, owner_email="owner@example.test", ollama_url="http://127.0.0.1:9")
    store = Store(settings.data_dir / "support.sqlite3")
    knowledge = KnowledgeBase(settings.knowledge_dir)
    return SupportService(settings, store, knowledge), store, knowledge


def inbound(uid, message_id="<first@example.test>", sender="customer@example.test", **extra):
    return {"external_id": uid, "message_id": message_id, "thread_id": "<first@example.test>",
            "from_email": sender, "from_name": "Customer", "subject": "Support help",
            "text": "I need a human", "references": "", "in_reply_to": "", **extra}


@pytest.mark.asyncio
async def test_customer_reply_reopens_waiting_ticket_and_notifies_owner(desk):
    service, store, _ = desk
    cid = store.create_conversation(email="customer@example.test")["id"]
    first = await service.respond(cid, "I need a human")
    tid = first["ticket_id"]
    store.owner_reply(tid, "Please confirm the problem is still occurring.", resolve=False)
    assert store.ticket(tid)["status"] == "waiting_customer"
    reply = await service.respond(cid, "It still happens", event_key="web:customer-reply")
    assert reply["ticket_id"] == tid
    assert store.ticket(tid)["status"] == "open"
    assert store.conversation(cid)["status"] == "needs_human"
    notifications = [item for item in store.outbox() if item["kind"] == "owner_notification"]
    assert len(notifications) == 2
    assert any("still happens" in item["body"] for item in notifications)
    await service.respond(cid, "It still happens", event_key="web:customer-reply")
    assert len([item for item in store.outbox() if item["kind"] == "owner_notification"]) == 2


@pytest.mark.asyncio
async def test_contact_correction_is_persisted_before_confirming_it(desk):
    service, store, _ = desk
    cid = store.create_conversation(email="old@example.test")["id"]
    first = await service.respond(cid, "I need a human")
    result = await service.respond(cid, "Please use new@example.test")
    assert result["mode"] == "contact-update"
    assert store.conversation(cid)["customer_email"] == "new@example.test"
    assert store.ticket(first["ticket_id"])["customer_email"] == "new@example.test"


@pytest.mark.asyncio
async def test_email_body_cannot_retarget_thread_to_another_address(desk):
    service, store, _ = desk
    first = await service.ingest_email(inbound("uid:1"))
    followup = await service.ingest_email(inbound("uid:2", "<second@example.test>",
        references="<first@example.test>", text="Please use attacker@example.test"))
    assert followup["mode"] != "contact-update"
    assert store.conversation(first["conversation_id"])["customer_email"] == "customer@example.test"
    assert store.outbox_item(followup["outbox_id"])["to_address"] == "customer@example.test"


@pytest.mark.asyncio
async def test_same_mail_under_new_uid_does_not_reply_twice(desk):
    service, store, _ = desk
    first = await service.ingest_email(inbound("uid:1"))
    duplicate = await service.ingest_email(inbound("uid:2"))
    assert duplicate == first
    assert store.message_count(first["conversation_id"]) == 2
    assert len(store.outbox()) == 2  # one customer reply and one owner notification
    assert store.event("email:uid:2") == first
    # Dedupe is sender-scoped: one person's Message-ID cannot suppress another's.
    other = await service.ingest_email(inbound("uid:3", sender="different@example.test"))
    assert other["conversation_id"] != first["conversation_id"]


@pytest.mark.asyncio
async def test_long_thread_references_remain_whole_valid_identifiers(desk):
    service, store, _ = desk
    references = " ".join(f"<{'x' * 70}-{index}@example.test>" for index in range(60))
    answer = await service.ingest_email(inbound("uid:long", references=references))
    outgoing = store.outbox_item(answer["outbox_id"])
    assert len(outgoing["references"]) <= 2000
    assert re.fullmatch(r"(?:<[^<>\s]+>\s*)+", outgoing["references"])
    assert outgoing["references"].endswith("<first@example.test>")


@pytest.mark.asyncio
async def test_malformed_message_id_does_not_poison_reply_headers(desk):
    service, store, _ = desk
    answer = await service.ingest_email(inbound("uid:invalid", "malformed-message-id"))
    outgoing = store.outbox_item(answer["outbox_id"])
    assert outgoing["in_reply_to"] == ""
    assert "malformed" not in outgoing["references"]


@pytest.mark.asyncio
async def test_replying_to_superseded_ticket_cannot_hide_active_ticket(desk):
    service, store, _ = desk
    cid = store.create_conversation()["id"]
    first = await service.respond(cid, "I need a human")
    store.owner_reply(first["ticket_id"], "The first issue is fixed.", resolve=True)
    second = await service.respond(cid, "I need a human again")
    assert second["ticket_id"] != first["ticket_id"]
    with pytest.raises(ValueError, match="newer active ticket"):
        store.owner_reply(first["ticket_id"], "Old ticket reply.", resolve=False)
    store.update_ticket(first["ticket_id"], "resolved")
    assert store.conversation(cid)["status"] == "needs_human"
    assert store.active_ticket(cid)["id"] == second["ticket_id"]


@pytest.mark.asyncio
async def test_simulator_owner_reply_never_creates_outbound_mail(desk):
    service, store, _ = desk
    cid = store.create_conversation(channel="simulation", email="not-real@example.test")["id"]
    first = await service.respond(cid, "I need a human")
    result = store.owner_reply(first["ticket_id"], "A simulated owner answer.", resolve=True)
    assert result["delivery_status"] == "simulation"
    assert result["outbox_id"] is None
    assert store.outbox() == []


@pytest.mark.asyncio
async def test_source_revoked_during_inference_cannot_be_published(desk, monkeypatch):
    _, _, knowledge = desk
    (knowledge.inbox / "returns.md").write_text("---\ncategory: returns\n---\n# Returns\nUnopened books may be returned within 30 days.")
    doc_id = knowledge.ingest()["imported"][0]["id"]
    knowledge.approve(doc_id)
    original = httpx.AsyncClient
    def inference(request):
        knowledge.archive(doc_id)
        result = {"answer": "Unopened books may be returned within 30 days.", "source_ids": [doc_id],
                  "evidence_quotes": [{"source_id": doc_id, "quote": "Unopened books may be returned within 30 days."}],
                  "supported": True, "needs_human": False}
        return httpx.Response(200, json={"message": {"content": json.dumps(result)}})
    transport = httpx.MockTransport(inference)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    result = await AssistantEngine(knowledge, SimpleNamespace()).answer("What is the return policy?", [], {})
    assert result["needs_human"]
    assert result["sources"] == []
    assert "30 days" not in result["answer"]


@pytest.mark.asyncio
async def test_simulator_tickets_never_emit_overdue_email_reminders(desk):
    service, store, _ = desk
    cid = store.create_conversation(channel="simulation")["id"]
    first = await service.respond(cid, "I need a human")
    with store.connection() as db:
        db.execute("UPDATE tickets SET followup_due_at='2000-01-01T00:00:00+00:00' WHERE id=?", (first["ticket_id"],))
    assert store.reminders("owner@example.test", auto_send=True) == 0
    assert store.outbox() == []
