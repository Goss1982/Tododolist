from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import threading
from pathlib import Path

from dotenv import load_dotenv

from .auth import authenticate, google_session
from .clients import BridgeError, GoogleTasks, RateLimited, Todoist
from .state import State
from .sync import sync_once

LOG = logging.getLogger(__name__)


def parser():
    result = argparse.ArgumentParser(
        description="Copy Google Tasks into Todoist, then complete Google."
    )
    commands = result.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="Connect Google using a browser (one-time setup)")
    auth.add_argument(
        "--no-browser", action="store_true", help="Print a login URL; use an SSH tunnel"
    )
    auth.add_argument(
        "--port", type=int, default=8765, help="Local OAuth callback port (default 8765)"
    )
    commands.add_parser("lists", help="List Google task-list names and IDs")
    for name in ("sync", "watch"):
        command = commands.add_parser(
            name, help="Run once" if name == "sync" else "Run continuously"
        )
        command.add_argument("--list", dest="selection", help="Google list ID, @default, or '*'")
        command.add_argument("--dry-run", action="store_true", help="Preview with no remote writes")
    return result


def run(args):
    token_file = Path(os.getenv("GOOGLE_TOKEN_FILE", "./data/google-token.json"))
    if args.command == "auth":
        authenticate(
            Path(os.getenv("GOOGLE_CLIENT_SECRET_FILE", "./secrets/google-client-secret.json")),
            token_file,
            args.port,
            not args.no_browser,
        )
        print("Google connected. Next: python -m tododolist lists")
        return 0

    if args.command == "lists":
        session = google_session(token_file)
        try:
            for item in GoogleTasks(session).lists():
                print(f"{item['title']}\t{item['id']}")
        finally:
            session.close()
        return 0

    token = os.getenv("TODOIST_API_TOKEN", "").strip()
    if not token:
        raise BridgeError("Set TODOIST_API_TOKEN in .env or your server's environment")
    selection = args.selection or os.getenv("GOOGLE_TASK_LIST_ID", "@default").strip()
    if not selection:
        raise BridgeError("GOOGLE_TASK_LIST_ID must be @default, a list ID, or *")
    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    if interval < 15:
        raise BridgeError("POLL_INTERVAL_SECONDS must be at least 15")
    stopping = threading.Event()
    if args.command == "watch":
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
    todoist = Todoist(token)
    with State(Path(os.getenv("STATE_FILE", "./data/sync.sqlite3"))) as state:
        try:
            while not stopping.is_set():
                wait = interval
                session = None
                try:
                    session = google_session(token_file)
                    summary = sync_once(
                        GoogleTasks(session), todoist, state, selection, args.dry_run
                    )
                    LOG.info(
                        "%s: copied=%d completed=%d pending=%d errors=%d",
                        "Preview" if args.dry_run else "Sync",
                        summary.copied,
                        summary.completed,
                        summary.pending,
                        summary.errors,
                    )
                    if args.command == "sync":
                        return 1 if summary.errors else 0
                except RateLimited as exc:
                    wait = max(interval, exc.delay)
                    if args.command == "sync":
                        raise
                    LOG.warning("%s", exc)
                except BridgeError as exc:
                    if args.command == "sync":
                        raise
                    LOG.error("%s; retrying in %ds", exc, interval)
                finally:
                    if session is not None:
                        session.close()
                stopping.wait(wait)
        finally:
            todoist.session.close()
    return 0


def main():
    os.umask(0o077)
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    for name in ("oauthlib", "requests_oauthlib", "google.auth", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Avoid logging network bodies, task titles, notes or access tokens.
    try:
        return run(parser().parse_args())
    except (BridgeError, OSError, sqlite3.Error, ValueError) as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - redact unexpected authentication exception details
        LOG.error("Stopped after %s; check configuration and retry", type(exc).__name__)
        return 1
