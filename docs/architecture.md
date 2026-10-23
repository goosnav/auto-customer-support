# Architecture and extension contracts

## One business per installation

Each instance owns its `.env`, knowledge tree, SQLite database, admin token, model configuration, and mailbox. Multi-tenant hosting and per-operator accounts are outside v0.1. Run one process per data directory.

FastAPI serves the operator console, public widget, and channel APIs. SQLite stores conversations, messages, tickets, outgoing mail, input receipts, and audit events. The knowledge manifest tracks category, provenance, exact content hashes, approval, and archive status. Local BM25 retrieval requires no external index.

## Authority

| Input | Role |
|---|---|
| Owner `.env` | Fixed services, secrets, destinations, sending mode, and bounds; never sent to the model. |
| Uploaded Markdown | Unreviewed data until explicit approval. Embedded instructions gain no tool authority. |
| Approved customer policy | Evidence for answers; edits revoke approval until re-reviewed. |
| Customer message/email/caller ID | Untrusted statements and contact metadata; no private-order authentication. |
| Ollama output | Proposed wording checked for citations, quoted evidence, quantities, and unsupported action claims. |
| Owner console reply | Authenticated customer-facing text, stored with separate email delivery state. |
| Order connector result | A live fact only after trusted identity and ownership checks. |

## Answer and handoff

`SupportService.respond` serializes turns. It collects contact details without granting account access, reads bounded history, and calls `AssistantEngine.answer`. Human requests and private order questions escalate. Typed approved fields drive dispatch arithmetic. Other questions use local retrieval, optional structured Ollama output, or a quoted passage.

`Store.finish_turn` saves transcript, ticket, notification draft, optional email reply, and processed-event receipt in one transaction. Web clients supply `client_message_id`; phone callbacks use call/turn; IMAP uses account/mailbox/UIDVALIDITY/UID plus sender-scoped Message-ID deduplication.

One active ticket exists per conversation. Customer replies reopen waiting tickets. The owner replies and optionally resolves through the console; email delivery remains a separate operation. Owner notification emails are informative, not commands.

## Public API

| Endpoint | Authentication | Purpose |
|---|---|---|
| `GET /api/config` | Public | Business greeting and browser voice availability. |
| `POST /api/conversations` | Public, rate limited | Create a web thread; return its random token once. |
| `PATCH /api/conversations/{id}` | Conversation token | Update name/email without identity verification. |
| `POST /api/conversations/{id}/messages` | Conversation token | Send text and optional customer-reported elapsed days. |
| `GET /api/conversations/{id}/messages` | Conversation token | Poll that transcript, including owner replies. |
| `POST /api/conversations/{id}/voice` | Conversation token | Upload one bounded local speech turn. |
| `GET /api/audio/{token}` | Short-lived random bearer URL | Fetch a generated WAV cached in memory. |
| `POST /api/phone/twilio` | Valid Twilio signature | Process a numbered telephone turn. |
| `/api/admin/*` | Admin token | Review knowledge, tickets, conversations, status, and delivery. |

Conversation tokens are hashed in SQLite and held in browser session storage. An admin token is not accepted as a customer token. Audio URLs expire after five minutes; audio is not stored in SQLite. Reverse proxies should not log Authorization headers or audio bearer URLs.

## Add a store/MCP integration

`orders.py` defines a narrow read-only contract rather than an unrestricted tool registry:

```python
from support_app.orders import HttpOrderConnector, VerifiedIdentity

# These values must come from a trusted sign-in or verification flow,
# never from customer-supplied chatbot fields.
identity = VerifiedIdentity(customer_id=authenticated_customer_id,
                            email=verified_account_email)
connector = HttpOrderConnector(configured_gateway_url, configured_service_token)
result = await connector.lookup(order_id, identity)
```

The HTTP gateway receives `order_id` and `customer_id` at a fixed configured URL. Responses must match requested order ID, verified customer ID, and verified email. Output is restricted to fulfillment state, relevant dates, tracking number, carrier, and update timestamp. A local JSON export connector enforces the same ownership boundary.

To activate live order answers, implement trusted authentication, bind identity to the conversation on the server, map an allowlisted read-only MCP/store operation to `lookup`, enforce ownership upstream, and test cross-customer and outage cases. The model must not choose arbitrary URLs, credentials, or write operations.

`ORDERS_*` environment variables reserve configuration names. Setting them alone does not enable live retrieval. There is no Shopify/MCP client, account verifier, refund, payment, or order mutation in this MVP. Public questions requiring live order facts are handed to the business owner.

## Failure behavior

No local model: retrieve approved text and calculate supported timing. Insufficient evidence: open a ticket. Database failure: do not claim a ticket was saved. Slow phone inference: record a fallback ticket within the call time budget when storage is healthy.

The outbox separates `pending`, `sending`, `sent`, `failed`, and `unknown`. Atomic claiming prevents concurrent sends. Explicit SMTP rejections permit owner-reviewed retry. A disconnect may follow successful acceptance, so ambiguous sends are held as `unknown`. Startup similarly holds interrupted sends. This does not claim exactly-once SMTP delivery.

SQLite supplies transactional durability. Knowledge writes use atomic manifest replacement and in-process locking. Multiple processes/hosts need shared locks, job queues, stronger resource controls, and additional identity management.

Evidence checking is not a semantic proof. Keyword retrieval may miss paraphrases, and plausible generated answers can omit conditions. Evaluate representative questions, ambiguous policies, exceptions, and source applicability before unattended use. Keep speech improvements downstream of the same support and ticket boundaries.
