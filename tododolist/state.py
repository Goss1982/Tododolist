"""Durable copy receipts and a cross-platform single-process lock."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .clients import BridgeError


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = sqlite3.connect(str(path) + ".lock", timeout=0)
        try:
            # A separate SQLite database holds an OS-backed lock. Crashes release it.
            self.lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            self.lock.close()
            raise BridgeError("Another sync is already using this state file") from None
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS transfers (
            source_key TEXT PRIMARY KEY, source_json TEXT NOT NULL,
            command_json TEXT NOT NULL, accepted INTEGER NOT NULL DEFAULT 0,
            todoist_id TEXT, completed INTEGER NOT NULL DEFAULT 0
        )""")
        self.db.commit()

    def get(self, key):
        return self.db.execute("SELECT * FROM transfers WHERE source_key=?", (key,)).fetchone()

    def prepare(self, key, snapshot, command):
        self.db.execute(
            "INSERT OR IGNORE INTO transfers(source_key,source_json,command_json) VALUES (?,?,?)",
            (key, json.dumps(snapshot, sort_keys=True), json.dumps(command, sort_keys=True)),
        )
        self.db.commit()  # Persist the EXACT UUID and payload before the network write.
        return self.get(key)

    def accepted(self, key, task_id):
        self.db.execute(
            "UPDATE transfers SET accepted=1, todoist_id=? WHERE source_key=?", (task_id, key)
        )
        self.db.commit()

    def completed(self, key):
        self.db.execute("UPDATE transfers SET completed=1 WHERE source_key=?", (key,))
        self.db.commit()

    def close(self):
        self.db.close()
        self.lock.rollback()
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
