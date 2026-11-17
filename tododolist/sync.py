from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date

from .clients import BridgeError, RateLimited

LOG = logging.getLogger(__name__)


def active(task):
    return (
        task.get("status") == "needsAction" and not task.get("deleted") and not task.get("hidden")
    )


def snapshot(task):
    # Exclude ETag/updated/status: completing a child can change those on a parent.
    return {key: task.get(key) for key in ("title", "notes", "due", "parent", "links")}


def source_key(inbox_id, list_id, task_id):
    return json.dumps([inbox_id, list_id, task_id], separators=(",", ":"))


def make_command(task, tasklist, inbox_id):
    title = task.get("title") or "Untitled Google task"
    sections = [task.get("notes") or ""]
    if len(title) > 500:
        sections.append("Full Google task title:\n" + title)
    sections.append("Google Tasks list: " + tasklist.get("title", tasklist["id"]))
    if task.get("parent"):
        sections.append("Google parent task ID: " + task["parent"])
    for link in task.get("links") or []:
        if link.get("link"):
            sections.append((link.get("description") or "Google task link") + ": " + link["link"])
    # A marker makes the origin inspectable without modifying the original title.
    sections.append("Google task ID: " + task["id"])
    args = {
        "content": title[:500],
        "description": "\n\n".join(filter(None, sections)),
        "project_id": inbox_id,
        "auto_parse_labels": False,
    }
    if task.get("due"):
        # The API's date is already a day, NOT a midnight instant to convert to local time.
        day = date.fromisoformat(task["due"][:10]).isoformat()
        args["due"] = {"date": day, "is_recurring": False}
    return {
        "type": "item_add",
        "uuid": str(uuid.uuid4()),
        "temp_id": str(uuid.uuid4()),
        "args": args,
    }


def ancestry(task_id, tasks):
    parents = []
    seen = {task_id}
    parent = tasks[task_id].get("parent")
    while parent in tasks:
        if parent in seen:
            raise BridgeError("Google Tasks returned a cyclic subtask hierarchy")
        parents.append(parent)
        seen.add(parent)
        parent = tasks[parent].get("parent")
    return parents


@dataclass
class Summary:
    copied: int = 0
    completed: int = 0
    pending: int = 0
    errors: int = 0


def sync_once(google, todoist, state, selection="@default", dry_run=False):
    summary = Summary()
    inbox_id = todoist.inbox()
    for tasklist in google.resolve_lists(selection):
        list_id = tasklist["id"]
        tasks = {t["id"]: t for t in google.tasks(list_id) if active(t)}
        ancestors = {tid: ancestry(tid, tasks) for tid in tasks}
        copied = set()
        for tid, task in tasks.items():
            key = source_key(inbox_id, list_id, tid)
            row = state.get(key)
            if row and row["completed"]:
                # One-way transfer: reopening an already-transferred task is not a new copy.
                LOG.warning("Already transferred Google task %s was reopened; leaving it open", tid)
                summary.pending += 1
                continue
            if dry_run:
                make_command(task, tasklist, inbox_id)  # Validate dates without remote writes.
                LOG.info(
                    "Preview: task %s, due %s, already copied=%s",
                    tid,
                    task.get("due", "none"),
                    bool(row and row["accepted"]),
                )
                summary.pending += 1
                continue
            try:
                if row is None:
                    row = state.prepare(key, snapshot(task), make_command(task, tasklist, inbox_id))
                if not row["accepted"]:
                    task_id = todoist.execute(json.loads(row["command_json"]))
                    state.accepted(key, task_id)
                    summary.copied += 1
                if json.loads(row["source_json"]) != snapshot(task):
                    LOG.error(
                        "Google task %s changed after its copy was prepared; leaving it open. "
                        "See README: changed tasks",
                        tid,
                    )
                    summary.errors += 1
                    continue
                copied.add(tid)
            except RateLimited:
                raise
            except (BridgeError, ValueError) as exc:
                LOG.error("Could not copy Google task %s: %s", tid, exc)
                summary.errors += 1

        if dry_run:
            continue
        # Re-read after copying: newly added or edited children must not be silently
        # completed when their parent is completed. Handle children before parents.
        for tid in sorted(tasks, key=lambda t: len(ancestors[t]), reverse=True):
            if tid not in copied:
                continue
            key = source_key(inbox_id, list_id, tid)
            fresh = {t["id"]: t for t in google.tasks(list_id) if active(t)}
            fresh_ancestors = {child: ancestry(child, fresh) for child in fresh}
            descendants = [child for child in fresh if tid in fresh_ancestors[child]]
            if any(
                child not in copied or snapshot(fresh[child]) != snapshot(tasks[child])
                for child in descendants
            ):
                LOG.warning("Keeping parent %s open because a child is not safely copied", tid)
                summary.pending += 1
                continue
            try:
                current = google.get(list_id, tid)
                if current.get("deleted"):
                    copied.discard(tid)
                    summary.pending += 1
                    continue
                if current.get("status") == "completed":
                    state.completed(key)  # Previous completion succeeded but its reply was lost.
                    continue
                if snapshot(current) != snapshot(tasks[tid]):
                    copied.discard(tid)
                    LOG.warning("Google task %s changed during sync; keeping it open", tid)
                    summary.pending += 1
                    continue
                google.complete(list_id, current)
                state.completed(key)
                summary.completed += 1
                LOG.info("Copied and completed Google task %s", tid)
            except RateLimited:
                raise
            except BridgeError as exc:
                copied.discard(tid)
                LOG.error("Copy is safe; Google completion for %s will retry: %s", tid, exc)
                summary.errors += 1
    return summary
