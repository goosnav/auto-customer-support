"""Portable Markdown knowledge with explicit, content-bound approval and local BM25.

Markdown is the source of truth. The manifest tracks approval, never customer data.
Uploading a document cannot approve it, even when its YAML claims otherwise.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import uuid
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

CATEGORIES = {"business", "products", "shipping", "returns", "faq", "support"}
MAX_DOCUMENT_BYTES = 128 * 1024
MAX_DOCUMENTS = 1000
_STOPWORDS = set("a an and are as at be been but by can could do does for from had has have how i if in is it its me my of on or our please so that the their them there these they this to us was we what when where which who will with would you your about any tell know want need thanks thank hi hello just some all more may get getting got am im ive id now ago days day".split())
_SYNONYMS = {
    "ships": "ship", "shipping": "ship", "shipped": "ship", "dispatch": "ship", "dispatches": "ship", "dispatched": "ship",
    "arrival": "delivery", "arrive": "delivery", "arrives": "delivery", "delivered": "delivery", "deliver": "delivery",
    "returns": "return", "returning": "return", "returned": "return", "refunds": "refund", "refunded": "refund",
    "ordered": "order", "ordering": "order", "orders": "order", "products": "product", "items": "item",
    "costs": "cost", "prices": "price", "hours": "hour", "policies": "policy", "damaged": "damage", "broken": "damage",
}


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [_SYNONYMS.get(t, t) for t in tokens if t not in _STOPWORDS and len(t) > 1]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(raw: str) -> tuple[dict[str, Any], str]:
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    metadata: dict[str, Any] = {}
    body = raw
    if raw.startswith("---\n") or raw.startswith("---\r\n"):
        pieces = re.split(r"^---\s*$", raw, maxsplit=2, flags=re.MULTILINE)
        if len(pieces) < 3:
            raise ValueError("YAML frontmatter is missing its closing --- line")
        if any(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(pieces[1])):
            raise ValueError("YAML aliases are not supported")
        loaded = yaml.safe_load(pieces[1])
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError("YAML frontmatter must be a mapping")
        metadata = loaded or {}
        body = pieces[2].lstrip("\r\n")
    if not body.strip():
        raise ValueError("The Markdown document has no body text")
    if any(not isinstance(key, str) for key in metadata):
        raise ValueError("Frontmatter keys must be strings")
    # Normalize YAML dates to JSON-compatible strings and reject unusual objects.
    metadata = json.loads(json.dumps(metadata, default=lambda obj: obj.isoformat() if isinstance(obj, (date, datetime)) else str(obj)))
    return metadata, body


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class KnowledgeBase:
    def __init__(self, root: Path | str):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise ValueError("Knowledge directory cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        self._lock = threading.RLock()
        for directory in ("inbox", "pending", "approved", "archive"):
            path = self.root / directory
            if path.is_symlink():
                raise ValueError("Knowledge directories cannot be symlinks")
            path.mkdir(exist_ok=True)
        self.inbox = self.root / "inbox"
        self._manifest_path = self.root / ".manifest.json"
        if self._manifest_path.is_symlink():
            raise ValueError("Manifest cannot be a symlink")
        if not self._manifest_path.exists():
            self._save({"version": 1, "documents": {}})

    def _load(self) -> dict[str, Any]:
        if self._manifest_path.is_symlink():
            raise ValueError("Manifest cannot be a symlink")
        manifest = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        if manifest.get("version") != 1 or not isinstance(manifest.get("documents"), dict):
            raise ValueError("Invalid knowledge manifest; restore it from backup")
        return manifest

    def _save(self, data: dict[str, Any]) -> None:
        temporary = self.root / (".manifest-" + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._manifest_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _safe_path(self, relative: str, *, exists: bool = True) -> Path:
        candidate = self.root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Invalid document path")
        current = self.root
        for piece in Path(relative).parts:
            current = current / piece
            if current.is_symlink():
                raise ValueError("Symlink documents and directories are not allowed")
        if not candidate.resolve().is_relative_to(self.root):
            raise ValueError("Document path leaves the knowledge directory")
        if exists and not candidate.is_file():
            raise FileNotFoundError("Document is missing")
        return candidate

    def _document(self, doc_id: str, record: dict[str, Any], *, content: bool = False) -> dict[str, Any]:
        result = {key: record.get(key) for key in ("title", "category", "source", "updated_at")}
        result["id"] = doc_id
        result["status"] = "pending"
        try:
            path = self._safe_path(record["path"])
            if path.stat().st_size > MAX_DOCUMENT_BYTES:
                raise ValueError("Document is too large")
            raw_bytes = path.read_bytes()
            metadata, body = _parse(raw_bytes.decode("utf-8"))
            digest = _digest(raw_bytes)
            title = str(metadata.get("title") or next((line.lstrip("# ") for line in body.splitlines() if line.startswith("# ")), record["title"]))[:180]
            result.update(title=title, source=record["path"], updated_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())
            if record.get("archived"):
                result["status"] = "archived"
            elif record.get("approved_hash") == digest and record["path"].startswith("approved/"):
                result["status"] = "approved"
            elif record.get("approved_hash"):
                result["status"] = "changed"
            expiry = metadata.get("valid_until")
            if expiry:
                try:
                    expired = date.fromisoformat(str(expiry)) < datetime.now(timezone.utc).date()
                except ValueError:
                    result["status"] = "invalid"
                    result["error"] = "valid_until must use YYYY-MM-DD"
                else:
                    if expired:
                        result["status"] = "expired"
            if str(metadata.get("audience", metadata.get("visibility", "customer"))).lower() in {"internal", "private", "staff", "owner"}:
                result["status"] = "internal"
            if content:
                result.update(content=body, raw=raw_bytes.decode("utf-8"), metadata=metadata)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            result["status"] = "invalid"
            result["error"] = str(exc)
            if content:
                result.update(content="", raw="", metadata={})
        return result

    def list_documents(self) -> list[dict[str, Any]]:
        with self._lock:
            manifest = self._load()
            return sorted((self._document(key, val) for key, val in manifest["documents"].items()), key=lambda doc: (doc["category"] or "", doc["title"] or ""))

    def read_document(self, doc_id: str) -> dict[str, Any]:
        with self._lock:
            manifest = self._load()
            if doc_id not in manifest["documents"]:
                raise KeyError("Unknown knowledge document")
            return self._document(doc_id, manifest["documents"][doc_id], content=True)

    def ingest(self) -> dict[str, Any]:
        report: dict[str, Any] = {"imported": [], "skipped": [], "errors": []}
        with self._lock:
            manifest = self._load()
            known_hashes = {item.get("import_hash") for item in manifest["documents"].values()}
            # os.walk does not follow directory symlinks. Depth and file count are bounded.
            candidates: list[Path] = []
            for directory, directories, filenames in os.walk(self.inbox, followlinks=False):
                depth = len(Path(directory).relative_to(self.inbox).parts)
                directories[:] = sorted(d for d in directories if not d.startswith(".") and not (Path(directory) / d).is_symlink() and depth < 4)
                for filename in sorted(filenames):
                    if filename.startswith(".") or not filename.lower().endswith(".md"):
                        continue
                    candidates.append(Path(directory) / filename)
                    if len(candidates) >= MAX_DOCUMENTS:
                        break
                if len(candidates) >= MAX_DOCUMENTS:
                    break
            for path in candidates:
                label = str(path.relative_to(self.inbox))
                try:
                    if len(manifest["documents"]) >= MAX_DOCUMENTS:
                        raise ValueError("Knowledge capacity reached (1000 documents)")
                    self._safe_path(str(path.relative_to(self.root)))
                    if path.stat().st_size > MAX_DOCUMENT_BYTES:
                        raise ValueError("Document exceeds 128 KiB")
                    raw_bytes = path.read_bytes()
                    raw = raw_bytes.decode("utf-8")
                    metadata, body = _parse(raw)
                    digest = _digest(raw_bytes)
                    if digest in known_hashes:
                        report["skipped"].append({"file": label, "reason": "Identical document already imported"})
                        continue
                    category = str(metadata.get("category") or self._infer_category(path.name + " " + body)).lower()
                    if category not in CATEGORIES:
                        raise ValueError("category must be business, products, shipping, returns, faq, or support")
                    title = str(metadata.get("title") or next((line[2:] for line in body.splitlines() if line.startswith("# ")), path.stem))[:180]
                    doc_id = uuid.uuid4().hex[:16]
                    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", path.stem).strip("-")[:70] or "document"
                    target_dir = self.root / "pending" / category
                    self._safe_path(str(target_dir.relative_to(self.root)), exists=False)
                    target_dir.mkdir(exist_ok=True)
                    target = target_dir / f"{stem}-{doc_id}.md"
                    with target.open("xb") as stream:
                        stream.write(raw_bytes)
                    manifest["documents"][doc_id] = {"title": title, "category": category, "source": label, "path": str(target.relative_to(self.root)), "updated_at": _now(), "import_hash": digest, "approved_hash": None, "archived": False}
                    self._save(manifest)
                    # Only remove the source after the manifest and destination are durable.
                    path.unlink()
                    known_hashes.add(digest)
                    report["imported"].append({"id": doc_id, "title": title, "category": category, "status": "pending"})
                except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
                    report["errors"].append({"file": label, "error": str(exc)})
        return report

    @staticmethod
    def _infer_category(text: str) -> str:
        text = text.lower()
        for category, words in (("shipping", ("shipping", "dispatch", "delivery")), ("returns", ("refund", "return policy", "returns")), ("products", ("product", "catalog")), ("support", ("contact", "escalation", "support")), ("business", ("about", "business", "opening hours"))):
            if any(word in text for word in words):
                return category
        return "faq"

    def approve(self, doc_id: str) -> dict[str, Any]:
        with self._lock:
            manifest = self._load()
            record = manifest["documents"].get(doc_id)
            if not record:
                raise KeyError("Unknown knowledge document")
            doc = self._document(doc_id, record, content=True)
            if doc["status"] in {"invalid", "expired", "archived", "internal"}:
                raise ValueError("Fix or replace an invalid, expired, archived, or internal document before customer approval")
            source = self._safe_path(record["path"])
            target_dir = self.root / "approved" / record["category"]
            self._safe_path(str(target_dir.relative_to(self.root)), exists=False)
            target_dir.mkdir(exist_ok=True)
            target = target_dir / source.name
            if target != source:
                if target.exists():
                    raise ValueError("Destination exists; refusing to overwrite an approved document")
                source.rename(target)
            record.update(path=str(target.relative_to(self.root)), approved_hash=_digest(target.read_bytes()), updated_at=_now())
            self._save(manifest)
            return self._document(doc_id, record)

    def archive(self, doc_id: str) -> dict[str, Any]:
        """Revoke a document immediately; archived text is never retrieved."""
        with self._lock:
            manifest = self._load()
            record = manifest["documents"].get(doc_id)
            if not record:
                raise KeyError("Unknown knowledge document")
            source = self._safe_path(record["path"])
            target = self.root / "archive" / source.name
            self._safe_path(str(target.relative_to(self.root)), exists=False)
            if target.exists() and target != source:
                raise ValueError("Archive destination already exists")
            if target != source:
                source.rename(target)
            record.update(path=str(target.relative_to(self.root)), approved_hash=None, archived=True, updated_at=_now())
            self._save(manifest)
            return self._document(doc_id, record)

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query_terms = set(tokenize(query))
        if not query_terms:
            return []
        with self._lock:
            manifest = self._load()
            documents = [self._document(key, record, content=True) for key, record in manifest["documents"].items()]
        documents = [doc for doc in documents if doc["status"] == "approved"]
        if not documents:
            return []
        counters = [Counter(tokenize(f"{doc['title']} {doc['title']} {doc['content']}")) for doc in documents]
        average_length = sum(sum(counter.values()) for counter in counters) / len(counters) or 1
        frequency = {term: sum(term in counter for counter in counters) for term in query_terms}
        scored = []
        for doc, counter in zip(documents, counters):
            length = sum(counter.values())
            score = 0.0
            matched = set()
            for term in query_terms:
                count = counter.get(term, 0)
                if not count:
                    continue
                matched.add(term)
                inverse = math.log(1 + (len(documents) - frequency[term] + 0.5) / (frequency[term] + 0.5))
                score += inverse * count * 2.5 / (count + 1.5 * (0.25 + 0.75 * length / average_length))
            if score:
                doc.pop("raw", None)
                doc.update(score=round(score, 5), coverage=len(matched) / len(query_terms), matched_terms=sorted(matched))
                scored.append(doc)
        return sorted(scored, key=lambda doc: (-doc["score"], doc["id"]))[:max(1, min(limit, 20))]
