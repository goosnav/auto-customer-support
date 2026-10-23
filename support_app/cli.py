"""Small local operations CLI. Never sends mail unless explicitly configured."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .knowledge import KnowledgeBase
from .store import Store

PROJECT = Path(__file__).resolve().parent.parent


def initialize(demo=False):
    env = Path(".env")
    if not env.exists():
        example = PROJECT / ".env.example"
        text = example.read_text() if example.exists() else "ADMIN_TOKEN=\nBUSINESS_NAME=Your business\n"
        token = secrets.token_urlsafe(36)
        text = text.replace("ADMIN_TOKEN=", "ADMIN_TOKEN=" + token, 1)
        descriptor = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
        print("Created .env with a unique admin token. Copy ADMIN_TOKEN into the console login when asked.")
    else:
        print("Kept your existing .env unchanged.")
    settings = Settings()
    knowledge = KnowledgeBase(settings.knowledge_dir)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        settings.data_dir.chmod(0o700)
        settings.knowledge_dir.chmod(0o700)
    except OSError:
        pass
    if demo:
        for path in (PROJECT / "examples" / "knowledge").glob("*.md"):
            destination = knowledge.inbox / path.name
            if not destination.exists():
                shutil.copyfile(path, destination)
        result = knowledge.ingest()
        print(json.dumps(result, indent=2, default=str))
        print("Demo policies are fictional. Review and approve them in Knowledge before using the simulator.")
    print("Start: support-desk serve\nConsole: http://127.0.0.1:8000\nCustomer widget: http://127.0.0.1:8000/widget")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local-first customer support")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create local configuration and private data folders")
    init.add_argument("--demo", action="store_true", help="Stage fictional example policies for review")
    serve = sub.add_parser("serve", help="Start the support desk and its background ingestion/email worker")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    sub.add_parser("ingest", help="Sort Markdown files from knowledge/inbox into the review queue")
    approve = sub.add_parser("approve", help="Approve a reviewed document by its ID")
    approve.add_argument("document_id")
    sub.add_parser("knowledge", help="List knowledge status and document IDs")
    backup = sub.add_parser("backup", help="Back up SQLite plus knowledge; contains private customer data")
    backup.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            initialize(args.demo)
            return
        settings = Settings()
        if args.command == "serve":
            import uvicorn
            from .app import create_app
            # No reload or multiple workers: local locks and audio cache are process-local.
            uvicorn.run(create_app(settings), host=args.host or settings.host, port=args.port or settings.port,
                        proxy_headers=False, access_log=False)
        elif args.command in {"ingest", "approve", "knowledge"}:
            knowledge = KnowledgeBase(settings.knowledge_dir)
            if args.command == "ingest":
                result = knowledge.ingest()
            elif args.command == "approve":
                result = knowledge.approve(args.document_id)
            else:
                result = knowledge.list_documents()
            print(json.dumps(result, indent=2, default=str))
        elif args.command == "backup":
            # Stop the server first so the knowledge files and DB share a consistent point in time.
            target = args.destination.resolve()
            if not (settings.data_dir / "support.sqlite3").is_file():
                raise ValueError("No support database exists in DATA_DIR. Start the server before backing up.")
            if target.exists():
                raise ValueError("Backup destination already exists; choose a new directory.")
            for private in (settings.data_dir.resolve(), settings.knowledge_dir.resolve()):
                if target == private or private in target.parents:
                    raise ValueError("Back up outside the data and knowledge directories.")
            target.mkdir(parents=True, mode=0o700)
            Store(settings.data_dir / "support.sqlite3").backup(target / "support.sqlite3")
            shutil.copytree(settings.knowledge_dir, target / "knowledge")
            print(f"Backup created: {target}. Keep it private; it contains customer transcripts and contact details.")
    except (ValueError, RuntimeError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
