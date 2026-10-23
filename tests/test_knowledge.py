from datetime import date, timedelta
from pathlib import Path

import pytest

from support_app.knowledge import KnowledgeBase


@pytest.fixture
def knowledge(tmp_path):
    return KnowledgeBase(tmp_path / "knowledge")


def upload(knowledge, content, filename="policy.md", approve=False):
    (knowledge.inbox / filename).write_text(content, encoding="utf-8")
    report = knowledge.ingest()
    assert not report["errors"], report
    doc_id = report["imported"][0]["id"]
    if approve:
        knowledge.approve(doc_id)
    return doc_id


def test_upload_cannot_self_approve(knowledge):
    doc_id = upload(knowledge, "---\ntitle: Shipping\ncategory: shipping\nstatus: approved\n---\n# Shipping\nOrders ship in 8–12 calendar days.")
    doc = knowledge.read_document(doc_id)
    assert doc["status"] == "pending"
    assert doc["source"].startswith("pending/shipping/")
    assert knowledge.search("shipping") == []
    knowledge.approve(doc_id)
    assert knowledge.search("shipping")[0]["id"] == doc_id


def test_editing_a_source_revokes_exact_content_approval(knowledge):
    doc_id = upload(knowledge, "# Returns\nOur return policy allows returns within 30 days.", approve=True)
    path = knowledge.root / knowledge.read_document(doc_id)["source"]
    path.write_text("# Returns\nWe refund a million dollars for every order.")
    assert knowledge.read_document(doc_id)["status"] == "changed"
    assert knowledge.search("return refund") == []
    knowledge.approve(doc_id)
    assert knowledge.search("refund")[0]["id"] == doc_id


def test_deleted_or_archived_sources_are_not_retrieved(knowledge):
    doc_id = upload(knowledge, "# Shipping\nShipping takes twelve days.", approve=True)
    knowledge.archive(doc_id)
    assert knowledge.read_document(doc_id)["status"] == "archived"
    assert knowledge.search("shipping") == []
    with pytest.raises(ValueError):
        knowledge.approve(doc_id)
    another = upload(knowledge, "# Returns\nReturn an unopened book within thirty days.", "returns.md", approve=True)
    (knowledge.root / knowledge.read_document(another)["source"]).unlink()
    assert knowledge.search("return") == []


def test_expired_document_cannot_be_approved(knowledge):
    yesterday = date.today() - timedelta(days=1)
    doc_id = upload(knowledge, f"---\nvalid_until: {yesterday.isoformat()}\n---\n# Shipping\nShipping policy expires.")
    assert knowledge.read_document(doc_id)["status"] == "expired"
    with pytest.raises(ValueError):
        knowledge.approve(doc_id)
    assert knowledge.search("shipping") == []


def test_private_documents_cannot_enter_customer_retrieval(knowledge):
    doc_id = upload(knowledge, "---\naudience: internal\n---\n# Margin\nOur secret margin is 97 percent.")
    assert knowledge.read_document(doc_id)["status"] == "internal"
    with pytest.raises(ValueError):
        knowledge.approve(doc_id)
    assert knowledge.search("margin") == []


def test_invalid_yaml_and_aliases_are_rejected(knowledge):
    (knowledge.inbox / "evil.md").write_text("---\nx: !!python/object/apply:os.system ['echo no']\n---\nHello")
    (knowledge.inbox / "aliases.md").write_text("---\nx: &x [one, two]\ny: *x\n---\nHello")
    report = knowledge.ingest()
    assert len(report["errors"]) == 2
    assert report["imported"] == []


def test_symlink_is_never_followed(knowledge, tmp_path):
    external = tmp_path / "private.md"
    external.write_text("# Private\nDo not ingest private credentials")
    (knowledge.inbox / "link.md").symlink_to(external)
    result = knowledge.ingest()
    assert result["errors"]
    assert not result["imported"]
    assert external.exists()
    with pytest.raises(ValueError):
        knowledge._safe_path("../private.md")


def test_identical_upload_is_idempotent(knowledge):
    content = "# Shipping\nOrders dispatch within twelve days."
    upload(knowledge, content)
    (knowledge.inbox / "duplicate.md").write_text(content)
    result = knowledge.ingest()
    assert not result["imported"]
    assert len(result["skipped"]) == 1
    assert len(knowledge.list_documents()) == 1


def test_local_ranking_and_unknown_query(knowledge):
    shipping = upload(knowledge, "# Shipping policy\nOrders ship after processing. Delivery is separate.", "shipping.md", approve=True)
    upload(knowledge, "# Returns policy\nContact us about a damaged book before returning it.", "returns.md", approve=True)
    assert knowledge.search("When will my package ship?")[0]["id"] == shipping
    assert knowledge.search("Explain nuclear reactor neutron cross sections") == []


def test_manifest_survives_restart(knowledge):
    doc_id = upload(knowledge, "# Opening hours\nOpening hours are Monday to Friday.", approve=True)
    other = KnowledgeBase(knowledge.root)
    assert other.search("opening hours")[0]["id"] == doc_id
