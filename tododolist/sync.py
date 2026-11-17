from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .clients import BridgeError, CommandRejected, RateLimited

LOG = logging.getLogger(__name__)
DEFAULT_LABEL = "Google Tasks"
# Incremental Google reads look back a little further than strictly needed, so a
# small clock difference between this machine and Google cannot hide a change.
CLOCK_MARGIN = timedelta(minutes=2)


def active(task):
    return (
        task.get("status") == "needsAction" and not task.get("deleted") and not task.get("hidden")
    )


def snapshot(task):
    # Exclude ETag/updated/status: completing a child can change those on a parent.
    return {key: task.get(key) for key in ("title", "notes", "due", "parent", "links")}


def source_key(inbox_id, list_id, task_id):
    # The Inbox ID identifies the Todoist account, so changing the destination
    # project never makes already-transferred tasks look new.
    return json.dumps([inbox_id, list_id, task_id], separators=(",", ":"))


def item_fields(task, tasklist):
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
    fields = {"content": title[:500], "description": "\n\n".join(filter(None, sections))}
    if task.get("due"):
        # The API's date is already a day, NOT a midnight instant to convert to local time.
        day = date.fromisoformat(task["due"][:10]).isoformat()
        fields["due"] = {"date": day, "is_recurring": False}
    return fields


def make_command(task, tasklist, project_id, label=None, parent_id=None):
    args = item_fields(task, tasklist)
    args.update(project_id=project_id, auto_parse_labels=False)
    if label:
        args["labels"] = [label]
    if parent_id:
        args["parent_id"] = parent_id
    return {
        "type": "item_add",
        "uuid": str(uuid.uuid4()),
        "temp_id": str(uuid.uuid4()),
        "args": args,
    }


def update_command(task, tasklist, todoist_id):
    args = {"id": todoist_id, **item_fields(task, tasklist)}
    args.setdefault("due", None)  # A date removed in Google is removed in Todoist too.
    return {"type": "item_update", "uuid": str(uuid.uuid4()), "args": args}


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


def rfc3339(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass
class Summary:
    copied: int = 0
    updated: int = 0
    completed: int = 0
    pending: int = 0
    errors: int = 0

    def changed(self):
        return bool(self.copied or self.updated or self.completed or self.errors)


def parent_link(state, inbox_id, list_id, task, tasks):
    """Return (ready, todoist_parent_id) for nesting a subtask under its parent's copy."""
    parent = task.get("parent")
    if parent not in tasks:
        return True, None  # No open parent in this list: copy as a top-level task.
    row = state.get(source_key(inbox_id, list_id, parent))
    if not row or not row["accepted"]:
        return False, None  # Wait one pass for the parent's copy rather than lose the nesting.
    if row["completed"] or not row["todoist_id"]:
        return True, None
    return True, row["todoist_id"]


def sync_once(google, todoist, state, selection="@default", dry_run=False, label=DEFAULT_LABEL):
    summary = Summary()
    inbox_id = todoist.inbox()
    project_id = todoist.destination()
    if label and not dry_run:
        todoist.ensure_label(label)
    for tasklist in google.resolve_lists(selection):
        list_id = tasklist["id"]
        tasks = {t["id"]: t for t in google.tasks(list_id) if active(t)}
        ancestors = {tid: ancestry(tid, tasks) for tid in tasks}
        copied = set()
        # Parents before children, so each child can be nested under its parent's copy.
        for tid in sorted(tasks, key=lambda t: len(ancestors[t])):
            task = tasks[tid]
            key = source_key(inbox_id, list_id, tid)
            row = state.get(key)
            if row and row["completed"]:
                # One-way transfer: reopening an already-transferred task is not a new copy.
                if not row["flagged"]:
                    LOG.warning(
                        "Google task %s was reopened after transfer; leaving it open and "
                        "not copying it again (reported once)",
                        tid,
                    )
                    state.flag(key)
                summary.pending += 1
                continue
            if row and row["flagged"]:
                summary.pending += 1  # Already reported as needing manual review.
                continue
            if dry_run:
                make_command(task, tasklist, project_id)  # Validate dates without writes.
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
                    ready, parent_id = parent_link(state, inbox_id, list_id, task, tasks)
                    if not ready:
                        LOG.info("Waiting for parent of Google task %s to be copied first", tid)
                        summary.pending += 1
                        continue
                    command = make_command(task, tasklist, project_id, label, parent_id)
                    row = state.prepare(key, snapshot(task), command)
                if not row["accepted"]:
                    state.accepted(key, todoist.execute(json.loads(row["command_json"])))
                    summary.copied += 1
                    row = state.get(key)
                if json.loads(row["source_json"]) != snapshot(task):
                    if not update_copy(todoist, state, key, row, task, tasklist):
                        summary.pending += 1
                        continue
                    summary.updated += 1
                copied.add(tid)
            except RateLimited:
                raise
            except (BridgeError, ValueError) as exc:
                LOG.error("Could not copy Google task %s: %s", tid, exc)
                summary.errors += 1

        if dry_run or not copied:
            continue
        complete_copied(google, state, inbox_id, list_id, tasks, ancestors, copied, summary)
    return summary


def update_copy(todoist, state, key, row, task, tasklist):
    """Bring the Todoist copy up to date after a Google edit. False means leave it open."""
    tid = task["id"]
    if not row["todoist_id"]:
        LOG.warning(
            "Google task %s was edited after copying, but its Todoist copy cannot be identified "
            "to update. Leaving it open for review (reported once)",
            tid,
        )
        state.flag(key)
        return False
    try:
        todoist.execute(update_command(task, tasklist, row["todoist_id"]))
    except CommandRejected:
        LOG.warning(
            "Google task %s was edited, but Todoist would not update its copy (it may have been "
            "deleted). Leaving it open for review (reported once)",
            tid,
        )
        state.flag(key)
        return False
    state.updated(key, snapshot(task))
    LOG.info("Updated the Todoist copy of edited Google task %s", tid)
    return True


def complete_copied(google, state, inbox_id, list_id, tasks, ancestors, copied, summary):
    # Re-read after copying: newly added or edited children must not be silently
    # completed when their parent is completed. One full read, then only the
    # changes since the previous read before each completion.
    mark = datetime.now(UTC) - CLOCK_MARGIN
    fresh = {t["id"]: t for t in google.tasks(list_id) if active(t)}
    # Handle children before parents.
    for tid in sorted(copied, key=lambda t: len(ancestors[t]), reverse=True):
        key = source_key(inbox_id, list_id, tid)
        next_mark = datetime.now(UTC) - CLOCK_MARGIN
        for item in google.tasks(list_id, updated_min=rfc3339(mark)):
            if active(item):
                fresh[item["id"]] = item
            else:
                fresh.pop(item["id"], None)
        mark = next_mark
        descendants = [child for child in fresh if child != tid and tid in ancestry(child, fresh)]
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
                LOG.info("Google task %s changed during sync; it will be updated next pass", tid)
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


ALERT_KIND = "google-auth"


def alert_once(state, todoist, reason):
    """Put one high-priority reminder in Todoist when Google access is lost."""
    row = state.alert(ALERT_KIND)
    if row and row["sent"]:
        return
    try:
        if row is None:
            command = {
                "type": "item_add",
                "uuid": str(uuid.uuid4()),
                "temp_id": str(uuid.uuid4()),
                "args": {
                    "content": "Tododolist needs re-authorising with Google",
                    "description": (
                        "The Google Tasks to Todoist bridge has lost access to Google, so new "
                        "Google tasks are not being moved.\n\nRun `python -m tododolist auth`, "
                        "then put the new data/google-token.json on the server (see the README)."
                        "\n\nReason logged: " + reason
                    ),
                    "project_id": todoist.destination(),
                    "priority": 4,
                    "due": {"string": "today"},
                    "auto_parse_labels": False,
                },
            }
            row = state.prepare_alert(ALERT_KIND, command)
        todoist.execute(json.loads(row["command_json"]))
        state.alert_sent(ALERT_KIND)
        LOG.warning("Added a re-authorisation reminder to Todoist")
    except BridgeError as exc:
        LOG.error("Could not add the re-authorisation reminder to Todoist: %s", exc)
