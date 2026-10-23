"""SQLite is the durable conversation, ticket, delivery, and idempotency ledger."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def identifier(prefix: str) -> str:
    return prefix + uuid4().hex


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, channel TEXT NOT NULL,
                    customer_name TEXT NOT NULL DEFAULT '', customer_email TEXT NOT NULL DEFAULT '',
                    customer_phone TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
                    external_key TEXT UNIQUE, subject TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    role TEXT NOT NULL, content TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation_id, created_at);
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    status TEXT NOT NULL DEFAULT 'open', subject TEXT NOT NULL, reason TEXT NOT NULL,
                    customer_email TEXT NOT NULL DEFAULT '', customer_phone TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, followup_due_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_ticket ON tickets(conversation_id)
                    WHERE status != 'resolved';
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, conversation_id TEXT REFERENCES conversations(id),
                    ticket_id TEXT REFERENCES tickets(id), to_address TEXT NOT NULL,
                    subject TEXT NOT NULL, body TEXT NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', message_id TEXT UNIQUE NOT NULL,
                    in_reply_to TEXT NOT NULL DEFAULT '', refs TEXT NOT NULL DEFAULT '',
                    dedupe_key TEXT UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    last_error TEXT, sent_at TEXT, auto_send INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    external_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    email_message_id TEXT, sender TEXT, result TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_email_id ON events(email_message_id);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, action TEXT NOT NULL, subject_id TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
            """)

    def recover_interrupted_deliveries(self):
        """Called only when the single server process starts, not during backups."""
        with self.connection() as db:
            # A process crash after SMTP acceptance is ambiguous, so never auto-retry it.
            db.execute("UPDATE outbox SET status='unknown', last_error='Interrupted delivery; check the mailbox before retrying.' WHERE status='sending'")

    @contextmanager
    def connection(self):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def audit(self, action: str, subject_id: str, detail: str = ""):
        with self.connection() as db:
            db.execute("INSERT INTO audit(action,subject_id,detail,created_at) VALUES(?,?,?,?)",
                       (action, subject_id, detail[:500], now()))

    @staticmethod
    def _conversation(row):
        if not row:
            return None
        result = dict(row)
        result.pop("token_hash", None)
        return result

    def create_conversation(self, channel="web", name="", email="", phone="", external_key=None, subject=""):
        token = secrets.token_urlsafe(32)
        cid = identifier("c_")
        created = now()
        with self.connection() as db:
            if external_key:
                existing = db.execute("SELECT * FROM conversations WHERE external_key=?", (external_key,)).fetchone()
                if existing:
                    return {**self._conversation(existing), "token": None}
            db.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (cid, token_hash(token), channel, name, email.lower(), phone, "active", external_key, subject, created, created))
        return {"id": cid, "token": token, "channel": channel, "customer_email": email.lower()}

    def authorize(self, cid: str, token: str) -> bool:
        with self.connection() as db:
            row = db.execute("SELECT token_hash FROM conversations WHERE id=? AND channel='web'", (cid,)).fetchone()
        return bool(row and token and secrets.compare_digest(row[0], token_hash(token)))

    def conversation(self, cid: str):
        with self.connection() as db:
            return self._conversation(db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone())

    def conversations(self):
        with self.connection() as db:
            rows = db.execute("""SELECT c.*, (SELECT content FROM messages WHERE conversation_id=c.id
                ORDER BY rowid DESC LIMIT 1) AS preview FROM conversations c ORDER BY updated_at DESC LIMIT 300""").fetchall()
        return [self._conversation(r) for r in rows]

    def messages(self, cid: str, limit=200):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid DESC LIMIT ?", (cid, limit)).fetchall()
        items = []
        for row in reversed(rows):
            item = dict(row)
            item["sources"] = json.loads(item["sources"])
            items.append(item)
        return items

    def message_count(self, cid: str):
        with self.connection() as db:
            return db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (cid,)).fetchone()[0]

    def update_contact(self, cid: str, name=None, email=None):
        with self.connection() as db:
            if name is not None:
                db.execute("UPDATE conversations SET customer_name=?,updated_at=? WHERE id=?", (name, now(), cid))
            if email is not None:
                db.execute("UPDATE conversations SET customer_email=?,updated_at=? WHERE id=?", (email.lower(), now(), cid))
                db.execute("UPDATE tickets SET customer_email=?,updated_at=? WHERE conversation_id=? AND status!='resolved'", (email.lower(), now(), cid))
        return self.conversation(cid)

    def ticket(self, tid: str):
        with self.connection() as db:
            row = db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else None

    def active_ticket(self, cid: str):
        with self.connection() as db:
            row = db.execute("SELECT * FROM tickets WHERE conversation_id=? AND status!='resolved'", (cid,)).fetchone()
        return dict(row) if row else None

    def tickets(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM tickets ORDER BY CASE status WHEN 'resolved' THEN 1 ELSE 0 END, created_at DESC LIMIT 300")]

    def event(self, key: str):
        with self.connection() as db:
            row = db.execute("SELECT result FROM events WHERE external_id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def email_event(self, sender: str, message_id: str):
        """A mailbox UID can change; Message-ID plus sender identifies a delivery."""
        if not message_id:
            return None
        with self.connection() as db:
            row = db.execute("SELECT result FROM events WHERE sender=? AND email_message_id=? ORDER BY rowid LIMIT 1",
                             (sender.lower(), message_id)).fetchone()
        return json.loads(row[0]) if row else None

    def alias_event(self, key: str, result: dict, email_meta: dict):
        """Remember a duplicate delivery's UID without creating another reply."""
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)",
                       (key, result["conversation_id"], email_meta.get("message_id"),
                        email_meta.get("sender"), json.dumps(result), now()))

    def email_conversation(self, sender: str, references: list[str]):
        with self.connection() as db:
            for ref in references[-20:]:
                row = db.execute("""SELECT c.* FROM conversations c JOIN outbox o ON c.id=o.conversation_id
                    WHERE o.message_id=? AND c.customer_email=? AND c.channel='email'""", (ref, sender.lower())).fetchone()
                if row:
                    return self._conversation(row)
                row = db.execute("""SELECT c.* FROM conversations c JOIN events e ON c.id=e.conversation_id
                    WHERE e.email_message_id=? AND c.customer_email=? AND c.channel='email'""", (ref, sender.lower())).fetchone()
                if row:
                    return self._conversation(row)
        return None

    @staticmethod
    def _add_outbox(db, *, to_address, subject, body, kind, conversation_id=None, ticket_id=None,
                    dedupe_key=None, in_reply_to="", references="", auto_send=False, mail_domain="support.local"):
        oid = identifier("o_")
        stamp = now()
        message_id = f"<{oid}@{mail_domain}>"
        db.execute("""INSERT OR IGNORE INTO outbox(id,conversation_id,ticket_id,to_address,subject,body,kind,
            message_id,in_reply_to,refs,dedupe_key,created_at,updated_at,auto_send) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (oid, conversation_id, ticket_id, to_address, subject[:250], body, kind, message_id,
             in_reply_to, references, dedupe_key, stamp, stamp, int(auto_send)))
        if db.execute("SELECT changes()").fetchone()[0] == 0:
            row = db.execute("SELECT id FROM outbox WHERE dedupe_key=?", (dedupe_key,)).fetchone()
            return row[0] if row else None
        return oid

    def finish_turn(self, *, cid, message, result, owner_email="", followup_hours=24, event_key=None,
                    email_meta=None, mail_domain="support.local", queue_email=False, auto_send=False):
        """Commit transcript + ticket + notification + dedupe receipt together, or none."""
        stamp = now()
        with self.connection() as db:
            if event_key:
                previous = db.execute("SELECT result FROM events WHERE external_id=?", (event_key,)).fetchone()
                if previous:
                    return json.loads(previous[0])
            conversation = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            if not conversation:
                raise KeyError(cid)
            ticket = db.execute("SELECT * FROM tickets WHERE conversation_id=? AND status!='resolved'", (cid,)).fetchone()
            new_ticket = False
            if ticket and ticket["status"] == "waiting_customer":
                # A customer reply is new work for the owner even when the
                # assistant can answer that individual message. Otherwise this
                # ticket would remain outside the open-ticket reminder queue.
                due = (datetime.now(timezone.utc) + timedelta(hours=followup_hours)).isoformat(timespec="seconds")
                db.execute("UPDATE tickets SET status='open',updated_at=?,followup_due_at=? WHERE id=?",
                           (stamp, due, ticket["id"]))
                if owner_email:
                    self._add_outbox(db, to_address=owner_email,
                        subject=f"Customer replied: {ticket['subject']}",
                        body=f"The customer replied to ticket {ticket['id']}.\n\n{message}\n\nReview the updated conversation in your support desk.",
                        kind="owner_notification", conversation_id=cid, ticket_id=ticket["id"],
                        dedupe_key=f"reopened:{ticket['id']}:{event_key or identifier('turn_')}",
                        auto_send=auto_send, mail_domain=mail_domain)
                ticket = db.execute("SELECT * FROM tickets WHERE id=?", (ticket["id"],)).fetchone()
            if result.get("needs_human") and not ticket:
                tid = identifier("t_")
                due = (datetime.now(timezone.utc) + timedelta(hours=followup_hours)).isoformat(timespec="seconds")
                subject = conversation["subject"] or message[:120]
                reason = result.get("reason") or "The customer needs an answer from the team."
                db.execute("INSERT INTO tickets VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (tid, cid, "open", subject, reason, conversation["customer_email"], conversation["customer_phone"], stamp, stamp, due))
                ticket = db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
                new_ticket = True
                if owner_email:
                    self._add_outbox(db, to_address=owner_email, subject=f"Support ticket {tid[-8:]}: {subject}",
                        body=f"A support ticket needs your attention.\n\nTicket: {tid}\nChannel: {conversation['channel']}\n"
                             f"Contact: {conversation['customer_email'] or conversation['customer_phone'] or 'Not provided'}\n"
                             f"Question: {message}\nReason: {reason}\n\nOpen the local support desk to review the transcript and reply.\n"
                             "This email is a notification; replying to it does not update the ticket.",
                        kind="owner_notification", conversation_id=cid, ticket_id=tid, dedupe_key=f"ticket:{tid}",
                        auto_send=auto_send, mail_domain=mail_domain)
            result = dict(result)
            result["conversation_id"] = cid
            result["ticket_id"] = ticket["id"] if ticket else None
            if result.get("needs_human"):
                contact = conversation["customer_email"] or conversation["customer_phone"]
                suffix = f" I've {'opened' if new_ticket else 'updated'} support ticket {ticket['id'][-8:]} for the team."
                if contact:
                    suffix += " Your contact details are recorded for follow-up."
                else:
                    suffix += " What email address should the team use for follow-up? You can also return to this conversation."
                result["reply"] = result["reply"].rstrip() + suffix
            for role, content, sources in [("user", message, []), ("assistant", result["reply"], result.get("sources", []))]:
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)", (identifier("m_"), cid, role, content, json.dumps(sources), stamp))
            state = "needs_human" if ticket else "active"
            db.execute("UPDATE conversations SET status=?,updated_at=? WHERE id=?", (state, stamp, cid))
            if ticket:
                db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (stamp, ticket["id"]))
            if queue_email and conversation["customer_email"]:
                meta = email_meta or {}
                oid = self._add_outbox(db, to_address=conversation["customer_email"],
                    subject="Re: " + (conversation["subject"].removeprefix("Re: ") or "Your support request"),
                    body=result["reply"], kind="customer_reply", conversation_id=cid, ticket_id=result["ticket_id"],
                    dedupe_key=f"reply:{event_key}" if event_key else None,
                    in_reply_to=meta.get("message_id", ""), references=meta.get("references", ""),
                    auto_send=auto_send, mail_domain=mail_domain)
                result["outbox_id"] = oid
                result["delivery_status"] = "pending"
            if event_key:
                meta = email_meta or {}
                db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (event_key, cid, meta.get("message_id"),
                    meta.get("sender"), json.dumps(result), stamp))
        return result

    def owner_reply(self, tid: str, message: str, resolve: bool, mail_domain="support.local"):
        stamp = now()
        with self.connection() as db:
            ticket = db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
            if not ticket:
                raise KeyError(tid)
            cid = ticket["conversation_id"]
            conversation = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            newer = db.execute("SELECT id FROM tickets WHERE conversation_id=? AND status!='resolved' AND id!=?", (cid, tid)).fetchone()
            if newer:
                raise ValueError("This conversation has a newer active ticket. Reply to that ticket instead.")
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)", (identifier("m_"), cid, "owner", message, "[]", stamp))
            state = "resolved" if resolve else "waiting_customer"
            db.execute("UPDATE tickets SET status=?,updated_at=? WHERE id=?", (state, stamp, tid))
            db.execute("UPDATE conversations SET status=?,updated_at=? WHERE id=?", (state, stamp, cid))
            oid = None
            if conversation["customer_email"] and conversation["channel"] != "simulation":
                incoming = db.execute("SELECT email_message_id FROM events WHERE conversation_id=? AND email_message_id IS NOT NULL ORDER BY rowid DESC LIMIT 1", (cid,)).fetchone()
                ref = incoming[0] if incoming else ""
                oid = self._add_outbox(db, to_address=conversation["customer_email"],
                    subject="Re: " + (conversation["subject"].removeprefix("Re: ") or "Your support request"),
                    body=message, kind="customer_reply", conversation_id=cid, ticket_id=tid,
                    in_reply_to=ref, references=ref, mail_domain=mail_domain)
            db.execute("INSERT INTO audit(action,subject_id,created_at) VALUES('owner_reply',?,?)", (tid, stamp))
        return {"ticket": self.ticket(tid), "outbox_id": oid,
                "delivery_status": "pending" if oid else ("simulation" if conversation["channel"] == "simulation" else "web_conversation" if conversation["channel"] == "web" else "manual_followup_required")}

    def update_ticket(self, tid: str, status: str):
        if status not in {"open", "waiting_customer", "resolved"}:
            raise ValueError("Invalid ticket status")
        with self.connection() as db:
            row = db.execute("SELECT conversation_id FROM tickets WHERE id=?", (tid,)).fetchone()
            if not row:
                raise KeyError(tid)
            try:
                db.execute("UPDATE tickets SET status=?,updated_at=? WHERE id=?", (status, now(), tid))
            except sqlite3.IntegrityError as exc:
                raise ValueError("Another active ticket already exists for this conversation") from exc
            active = db.execute("SELECT status FROM tickets WHERE conversation_id=? AND status!='resolved'", (row[0],)).fetchone()
            current_status = active[0] if active else status
            db.execute("UPDATE conversations SET status=?,updated_at=? WHERE id=?", ("needs_human" if current_status == "open" else current_status, now(), row[0]))
        self.audit("ticket_status", tid, status)
        return self.ticket(tid)

    def outbox(self):
        with self.connection() as db:
            return [self._mail_item(r) for r in db.execute("SELECT * FROM outbox ORDER BY created_at DESC LIMIT 300")]

    @staticmethod
    def _mail_item(row):
        item = dict(row)
        item["references"] = item.pop("refs")
        return item

    def outbox_item(self, oid):
        with self.connection() as db:
            row = db.execute("SELECT * FROM outbox WHERE id=?", (oid,)).fetchone()
        return self._mail_item(row) if row else None

    def claim_delivery(self, oid):
        with self.connection() as db:
            changed = db.execute("UPDATE outbox SET status='sending',updated_at=? WHERE id=? AND status IN ('pending','failed')", (now(), oid)).rowcount
        return self.outbox_item(oid) if changed else None

    def finish_delivery(self, oid, status, error=None):
        if status not in {"sent", "failed", "unknown"}:
            raise ValueError("Invalid delivery state")
        with self.connection() as db:
            db.execute("UPDATE outbox SET status=?,last_error=?,updated_at=?,sent_at=? WHERE id=? AND status='sending'",
                       (status, error, now(), now() if status == "sent" else None, oid))
        return self.outbox_item(oid)

    def reminders(self, owner_email, auto_send=False, mail_domain="support.local"):
        if not owner_email:
            return 0
        with self.connection() as db:
            rows = db.execute("""SELECT t.* FROM tickets t JOIN conversations c ON c.id=t.conversation_id
                WHERE t.status='open' AND t.followup_due_at < ? AND c.channel!='simulation'""", (now(),)).fetchall()
            for ticket in rows:
                self._add_outbox(db, to_address=owner_email, subject=f"Follow-up due: {ticket['subject']}",
                    body=f"Ticket {ticket['id']} still needs an answer.\n\n{ticket['reason']}\n\nReview it in your support desk.",
                    kind="owner_reminder", conversation_id=ticket["conversation_id"], ticket_id=ticket["id"],
                    dedupe_key=f"reminder:{ticket['id']}:{now()[:10]}", auto_send=auto_send, mail_domain=mail_domain)
        return len(rows)

    def counts(self):
        with self.connection() as db:
            return {
                "conversations": db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                "tickets_open": db.execute("SELECT COUNT(*) FROM tickets WHERE status!='resolved'").fetchone()[0],
                "outbox_pending": db.execute("SELECT COUNT(*) FROM outbox WHERE status IN ('pending','failed','unknown')").fetchone()[0],
            }

    def backup(self, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db, sqlite3.connect(destination) as target:
            db.backup(target)
