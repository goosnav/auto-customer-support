# Deployment and operations

## Local workstation

Use the README's virtual-environment setup. The server binds to `127.0.0.1` by default. Its background worker runs only while the server runs. Keep the machine awake if customers depend on it; a sleeping laptop cannot provide continuous support.

Run **one process** per `DATA_DIR`/`KNOWLEDGE_DIR`. Do not add Uvicorn workers or run a second server against the same directories. Knowledge locks, voice admission control, the limiter, and temporary audio cache are process-local. For a small installation, the application deliberately serializes support turns; concurrency and latency need measurement before advertising a busy phone line.

`.env` holds the admin token and optional provider credentials. The init command creates it with restrictive permissions. All business/customer content is excluded from Git by default. The source repository does not contain your live knowledge, configuration, or database.

## Public customer access with private operator access

Put a TLS reverse proxy or tunnel in front of the local server. Set `PUBLIC_BASE_URL` to its exact HTTPS origin, without a path suffix. Set `ALLOWED_ORIGINS` to the sites permitted to embed the widget.

Expose the necessary customer paths:

- `/widget`, `/widget.js`, `/static/widget.js`, `/static/widget-ui.js`, `/static/widget.css`
- `/api/config`, `/api/conversations` and its child paths
- `/api/audio/*` for short-lived generated speech
- `/api/phone/twilio` only when the phone adapter is enabled

Keep `/` and `/api/admin/*` behind your VPN or gateway access control where practical. They also require the application admin token. Do not expose SQLite, `.env`, the knowledge directories, Ollama, or the TTS service as public static resources. The application serves only its bundled static folder.

Configure the gateway for request-size/rate limits and appropriate timeout budgets. The internal limiter uses the socket peer and deliberately does not trust arbitrary forwarded-IP headers. Behind one proxy, customers share that peer's application limit; enforce per-client limiting at the gateway and tune `RATE_LIMIT_PER_MINUTE` accordingly. Authenticated operator calls are exempt from the general peer limit; invalid authentication attempts are not.

Do not cache API responses or log Authorization headers and temporary audio bearer URLs. Microphone access requires localhost or HTTPS. The host website's CSP must allow the widget script/frame, and its microphone Permissions Policy may need to delegate to the support origin. Test the actual embed on that site.

For telephone, point the provider at the exact configured HTTPS callback URL. Signed requests use this fixed origin, not caller-controlled forwarded host headers. Configure a provider fallback route to a real support contact before publishing the number. Nothing in the repository purchases a number, configures DNS, starts a tunnel, or deploys your website automatically.

## Docker alternative

The Docker files package the core web/email service. They have not been validated in a Docker daemon during this implementation. The native Python install and browser workflow were exercised.

Create `.env` using `support-desk init` from the native setup, then:

```sh
docker compose up --build -d
docker compose logs -f support
```

Port 8000 is bound to loopback. Named volumes persist the SQLite database and knowledge tree. Upload policies through the console. `docker compose down` preserves the volumes; **`docker compose down -v` deletes them**.

Ollama is external to this container. `DOCKER_OLLAMA_URL` defaults to `http://host.docker.internal:11434`; it must point to a reachable local service. Host Ollama bound only to loopback may be unreachable from a Linux container. Use an isolated container network or a host interface permitted only from the support container; do not publish Ollama openly. For the least setup on a workstation, use the native install.

Optional phone and voice dependencies are not installed by the core image. Add the relevant extras to a derived image if needed. For example, add `pip install --no-cache-dir 'twilio>=9.4,<10'` before switching to the non-root user for telephone. Local voice needs the voice extra, cached model volume, adequate memory, and a separately configured Kokoro service; the core Compose 1 GiB limit is not a voice-model hardware recommendation. Follow [voice setup](voice.md).

## Knowledge maintenance

Use the dashboard to upload, inspect, approve, and archive documents while the server runs. If editing approved Markdown directly, its hash changes and customer retrieval excludes it until approved again. The console displays that change. A dropped file is staged, never automatically trusted as policy.

Use one authoritative document per policy. Archive superseded versions; do not leave two conflicting shipping windows approved. `valid_until` can expire temporary information. Facts that change per customer or transaction belong in an identity-verified connector, not in general support Markdown.

## Backup and restore

Stop the server before copying a coherent database/knowledge snapshot. From the activated environment:

```sh
support-desk backup /path/to/private-backups/support-2026-10-23
```

The backup contains `support.sqlite3` and the complete knowledge tree/approval manifest. It contains private customer information. Back up `.env` separately in a suitable secret store; it is intentionally not copied by the command. Choose an outside backup location and a new directory each time.

To restore, stop the server, preserve the current data as a separate fallback, restore the database into `DATA_DIR/support.sqlite3`, restore the knowledge tree to `KNOWLEDGE_DIR`, and restore the matching configuration/secrets. Do not retain stale SQLite `-wal`/`-shm` sidecars from a different database. Restart and verify transcript, ticket, and knowledge counts plus a synthetic conversation.

Container users should export named volumes using their normal Docker backup process with the service stopped. Backups are useful only after a restore has been tested.

## Delivery and follow-up

Start with `EMAIL_DELIVERY_ENABLED=false`. Incoming email becomes drafts; ticket notifications and reminders also stay in the outbox. Configure an existing dedicated mailbox, review outputs, enable explicit sending, then opt into automatic new email answers if appropriate. The worker processes unread mail, so avoid marking new messages read before ingestion. Enabling intake can process existing unread mail; start with an empty dedicated folder or deliberately review that backlog in draft-only mode.

`sent` means SMTP acceptance. Inspect `failed` and `unknown` rows. Unknown sends cannot be casually retried through the UI because the server might already have accepted them. Check the mailbox/provider, then use a new deliberate owner reply if needed. A queued owner notification is not proof anyone read it; monitor the console.

The MVP stores transcripts until you remove them. There is no automated retention policy, per-customer export/deletion UI, disk encryption, distributed job queue, or external monitoring system. Use full-disk encryption and private backups if appropriate for your deployment. Select and implement retention before collecting real customer data at scale.

## Before making customer promises

Exercise your actual policies, product exceptions, noisy audio, an unavailable model, missing order data, duplicated callbacks, and owner response timing. Measure on the intended hardware. Check inbox delivery with your mailbox provider and call quality through the real number. The code/tests do not substitute for those provider and hardware checks.
