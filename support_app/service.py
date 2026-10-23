"""One support workflow shared by web chat, local voice, email, and phone."""

from __future__ import annotations

import asyncio
import re
import smtplib

from .engine import AssistantEngine
from .knowledge import KnowledgeBase
from .mail import MailAdapter
from .store import Store


EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63})(?![\w.-])")


def valid_email(value: str) -> bool:
    return not value or (len(value) <= 254 and EMAIL_RE.fullmatch(value) is not None)


class SupportService:
    def __init__(self, settings, store: Store, knowledge: KnowledgeBase):
        self.settings = settings
        self.store = store
        self.knowledge = knowledge
        self.engine = AssistantEngine(knowledge, settings)
        self.mail = MailAdapter(settings)
        # One process, one writer. A complete turn is persisted atomically.
        self._turn_lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()

    @property
    def mail_domain(self):
        address = self.settings.smtp_from
        return address.split("@")[-1].rstrip(">") if "@" in address else "support.local"

    async def respond(self, cid, message, *, ordered_days_ago=None, event_key=None, email_meta=None,
                      queue_email=False, forced_result=None):
        async with self._turn_lock:
            if event_key:
                previous = self.store.event(event_key)
                if previous:
                    return previous
                if email_meta and email_meta.get("message_id"):
                    previous = self.store.email_event(email_meta["sender"], email_meta["message_id"])
                    if previous:
                        self.store.alias_event(event_key, previous, email_meta)
                        return previous
            conversation = self.store.conversation(cid)
            if not conversation:
                raise KeyError(cid)
            if self.store.message_count(cid) >= self.settings.max_conversation_messages:
                raise ValueError("This conversation has reached its message limit. Please start a new conversation.")
            message = message.strip()
            if not message or len(message) > self.settings.max_message_chars:
                raise ValueError("Please send a shorter, non-empty message.")
            # Contact collection does not establish identity or grant access to order data.
            found = EMAIL_RE.findall(message)
            contact_only = len(found) == 1 and bool(re.fullmatch(
                r"(?:my (?:email|email address) is\s+|(?:please )?(?:use|contact me at|email me at)\s+)?" + re.escape(found[0]) + r"[.!\s]*",
                message, re.I))
            if len(found) == 1 and (not conversation["customer_email"] or
                    (contact_only and conversation["channel"] in {"web", "simulation", "phone"})):
                self.store.update_contact(cid, email=found[0])
                conversation = self.store.conversation(cid)
            history = self.store.messages(cid, limit=12)
            context = {"channel": conversation["channel"], "identity_verified": False,
                       "customer_email": conversation["customer_email"], "ordered_days_ago": ordered_days_ago}
            if forced_result is not None:
                result = forced_result
            elif self.store.active_ticket(cid) and contact_only and found[0].lower() == conversation["customer_email"]:
                result = {"answer": "Thank you. I've recorded that email address on your support request.",
                          "sources": [], "needs_human": False, "reason": None, "mode": "contact-update"}
            else:
                try:
                    result = await self.engine.answer(message, history, context)
                except Exception:
                    # The fallback records the real question; no fabricated policy or silent failure.
                    result = {"answer": "I couldn't retrieve a reliable answer just now. The team needs to review your question.",
                              "sources": [], "needs_human": True, "reason": "Answer service unavailable", "mode": "handoff"}
            result = {"reply": result.get("answer", result.get("reply", "The team needs to review your question.")),
                      "sources": result.get("sources", []), "needs_human": bool(result.get("needs_human", True)),
                      "reason": result.get("reason"), "mode": result.get("mode", "handoff")}
            return self.store.finish_turn(cid=cid, message=message, result=result,
                owner_email="" if conversation["channel"] == "simulation" else self.settings.owner_email,
                followup_hours=self.settings.followup_hours, event_key=event_key, email_meta=email_meta,
                mail_domain=self.mail_domain, queue_email=queue_email, auto_send=self.settings.email_auto_send)

    async def phone_turn(self, call_sid, speech, caller, event_key):
        conversation = self.store.create_conversation(channel="phone", phone=caller[:64], external_key=f"phone:{call_sid}")
        # Keep a PSTN webhook below the provider timeout. A slow model becomes a durable ticket.
        task = asyncio.create_task(self.respond(conversation["id"], speech, event_key=f"phone:{event_key}"))
        try:
            return await asyncio.wait_for(task, timeout=self.settings.phone_turn_timeout_seconds)
        except TimeoutError:
            return self.store.finish_turn(cid=conversation["id"], message=speech,
                event_key=f"phone:{event_key}", owner_email=self.settings.owner_email,
                followup_hours=self.settings.followup_hours, mail_domain=self.mail_domain,
                auto_send=self.settings.email_auto_send, result={
                    "reply": "I need the team to check that question.", "sources": [], "needs_human": True,
                    "reason": "Phone answer exceeded response time budget", "mode": "handoff"})

    async def ingest_email(self, item):
        key = "email:" + item["external_id"]
        previous = self.store.event(key)
        if previous:
            return previous
        sender = item["from_email"].lower()
        if not valid_email(sender) or not sender:
            raise ValueError("Inbound email has no valid sender")
        references = re.findall(r"<[^<>\s]+>", " ".join(str(item.get(k, "")) for k in ("references", "in_reply_to", "thread_id")))
        conversation = self.store.email_conversation(sender, references)
        if not conversation:
            conversation = self.store.create_conversation(channel="email", name=item.get("from_name", "")[:100], email=sender,
                subject=item.get("subject", "Support request")[:200], external_key="email:" + sender + ":" + item.get("thread_id", item["external_id"]))
        message_id = str(item.get("message_id", ""))
        if len(message_id) > 998 or not re.fullmatch(r"<[^<>\s]+>", message_id):
            message_id = ""
        # Preserve whole identifiers. Slicing the tail of a long References
        # header can cut an ID in half and make every subsequent SMTP reply fail.
        selected, length = [], 0
        for reference in reversed(references + ([message_id] if message_id else [])):
            if reference in selected or len(reference) > 998:
                continue
            if length + len(reference) + 1 > 2000 or len(selected) >= 20:
                break
            selected.append(reference)
            length += len(reference) + 1
        refs = " ".join(reversed(selected))
        body = item.get("text", "").strip() or item.get("subject", "Support request")
        if len(body) > self.settings.max_message_chars:
            body = body[:self.settings.max_message_chars - 25] + "\n[message truncated]"
        return await self.respond(conversation["id"], body, event_key=key, queue_email=True,
            email_meta={"message_id": message_id, "references": refs, "sender": sender})

    async def send_outbox(self, oid):
        if not self.settings.email_delivery_enabled:
            raise ValueError("Email delivery is disabled. Configure SMTP and set EMAIL_DELIVERY_ENABLED=true first.")
        async with self._delivery_lock:
            item = self.store.claim_delivery(oid)
            if item is None:
                current = self.store.outbox_item(oid)
                if current is None:
                    raise KeyError(oid)
                raise ValueError(f"This message is {current['status']}; it cannot be sent again automatically.")
            try:
                await asyncio.to_thread(self.mail.send, item)
            except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                    smtplib.SMTPDataError, ValueError):
                return self.store.finish_delivery(oid, "failed", "SMTP rejected delivery. Check sender, recipient, and authentication settings.")
            except Exception:
                # The server may have accepted DATA before the connection failed.
                return self.store.finish_delivery(oid, "unknown", "Delivery could not be confirmed. Check the mailbox/provider before sending another message.")
            self.store.audit("email_sent", oid)
            return self.store.finish_delivery(oid, "sent")

    async def worker_once(self):
        result = {"ingest": await asyncio.to_thread(self.knowledge.ingest), "emails_processed": 0, "emails_failed": 0, "sent": 0}
        if self.settings.imap_host:
            with self.store.connection() as db:
                seen = {r[0].removeprefix("email:") for r in db.execute("SELECT external_id FROM events WHERE external_id LIKE 'email:%'")}
            messages = await asyncio.to_thread(self.mail.fetch_inbound, seen)
            for item in messages:
                try:
                    await self.ingest_email(item)
                    result["emails_processed"] += 1
                except Exception:
                    result["emails_failed"] += 1
        result["reminders_due"] = self.store.reminders(self.settings.owner_email, self.settings.email_auto_send, self.mail_domain)
        if self.settings.email_auto_send and self.settings.email_delivery_enabled:
            for item in reversed(self.store.outbox()):
                # Failed/ambiguous attempts always need operator review; never blind retry.
                if item["status"] == "pending" and item["auto_send"]:
                    sent = await self.send_outbox(item["id"])
                    result["sent"] += int(sent["status"] == "sent")
        return result
