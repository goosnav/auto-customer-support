# Email and follow-up

Email uses your existing mailbox over IMAP and SMTP. Knowledge retrieval, response generation, transcripts, tickets, and the outbox stay on the local server. The mailbox provider still transports and stores email. No paid language-model API is required.

## Configure a dedicated support mailbox

Start in draft-only mode. Put credentials in your local `.env`:

```dotenv
OWNER_EMAIL=owner@your-business.example
IMAP_HOST=imap.your-mail-provider.example
IMAP_PORT=993
IMAP_USERNAME=support@your-business.example
IMAP_PASSWORD=your-mailbox-app-password
IMAP_MAILBOX=INBOX
IMAP_BATCH_SIZE=30

SMTP_HOST=smtp.your-mail-provider.example
SMTP_PORT=465
SMTP_USERNAME=support@your-business.example
SMTP_PASSWORD=your-mailbox-app-password
SMTP_FROM=support@your-business.example
SMTP_SECURITY=ssl

EMAIL_DELIVERY_ENABLED=false
EMAIL_AUTO_SEND=false
WORKER_ENABLED=true
WORKER_INTERVAL_SECONDS=30
```

These hostnames are placeholders. Use the IMAP/SMTP settings supplied by your mailbox provider. This adapter uses username/password or provider-issued app passwords; OAuth-only providers require a future OAuth adapter or a compatible mailbox. Passwords are never read from customer Markdown documents.

For a provider requiring STARTTLS, use `SMTP_SECURITY=starttls` and its submission port, commonly 587. TLS certificate verification is enabled. Plaintext SMTP and downgrade-on-failure are not supported.

Restart `support-desk serve` after configuration changes. The server's background worker checks for unread mail while it is running. Turning the server off pauses polling and follow-up processing.

## What happens to an incoming email

1. The adapter reads unread messages using IMAP `BODY.PEEK[]` in a read-only mailbox. It does not mark mail read, move it, or delete it.
2. The account/mailbox, UIDVALIDITY, and UID identify a particular incoming item. Persisted application receipts prevent processing the same item twice.
3. The adapter extracts a text body, or strips HTML to text. Attachments are not ingested or executed. Large messages and automatic/bulk responses are skipped.
4. The shared support service retrieves approved knowledge and creates a reply. An unresolved question creates a ticket and owner notification in the outbox.
5. The reply remains pending until delivery is allowed and an owner sends it, or automatic delivery is explicitly enabled.

Replies are threaded using `Message-ID`, `In-Reply-To`, and `References`, scoped to the sender. A customer cannot access another customer's thread merely by supplying its message ID. `Reply-To` is retained as metadata; replies go to the validated sender address, not an arbitrary redirect requested in an email header.

Message headers and sender addresses are untrusted inputs. A claimed sender address is not proof that the sender owns an order. Private order lookups need the separate trusted identity/connector flow.

## Review and send

Use the console's Outbox to review a draft, then enable delivery when the mailbox configuration is ready:

```dotenv
EMAIL_DELIVERY_ENABLED=true
EMAIL_AUTO_SEND=false
```

Restart the server. Sending an outbox item is an explicit owner action. This is the recommended starting configuration for a new knowledge base.

When you have checked actual incoming questions and replies, opt into automatic dispatch:

```dotenv
EMAIL_DELIVERY_ENABLED=true
EMAIL_AUTO_SEND=true
```

Automatic dispatch applies to outbox items the support service creates with automatic sending enabled; changing the flag does not retroactively authorize every old draft. Existing drafts can still be reviewed and sent manually. The system suppresses automatic responses to automatic mail, bounces, lists, and its own messages to reduce reply loops. There is no outbound marketing or recipient discovery functionality.

## Owner resolution and customer follow-up

Open the ticket in the owner console, inspect the conversation, and enter the customer-facing answer. The application records the resolution and queues the follow-up. The owner can also send the customer an email outside this application, then update the ticket to match what actually happened.

An owner email notification alone does not close a case. **Replying to that notification by email is not a ticket-control command in this MVP.** Use the console to resolve the ticket and create the tracked response. Keeping internal resolution notes separate from the customer-facing text avoids accidentally exposing internal discussion.

The scheduled local worker also checks unresolved follow-up deadlines and queues owner reminders. A reminder is an internal follow-up task; it is not evidence that a customer has received an answer. Configure `OWNER_EMAIL` or monitor the Tickets console directly.

## Delivery states and operational limits

- **Pending:** queued locally; not sent.
- **Sending:** a send attempt has been claimed to prevent two workers sending it simultaneously.
- **Sent:** the SMTP server accepted the message. Inbox placement and delivery are not guaranteed by SMTP acceptance.
- **Failed:** an explicit rejection occurred. Correct the configuration or recipient, then review before making another attempt.
- **Unknown:** the connection failed around delivery or the process stopped during a send. Check the provider/mailbox before retrying; the server may already have accepted it.

SMTP does not provide a universally reliable exactly-once delivery primitive. Stable outbound message IDs help threading and deduplication, but cannot eliminate every duplicate after an ambiguous network failure. The worker does not blindly retry failed or unknown sends.

The mailbox adapter reads a bounded batch and rejects messages larger than 2 MiB before fetching their bodies. It records skipped IDs in memory for efficient repeated polling; that skip cache resets when the process restarts. Support messages and receipt IDs are persisted by the application. Imported text is bounded; the service may truncate long customer messages before processing. Attachments, mailbox OAuth, provider delivery webhooks, native Slack/WhatsApp/SMS adapters, and owner replies-as-commands are outside this MVP.

Keep `.env`, `data/`, and the populated `knowledge/` directory private. Back up transcripts and the SQLite database using the documented local backup workflow. A generalist source repository should contain example policies only, never live customer records or mailbox credentials.
