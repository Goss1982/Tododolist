import copy
import tempfile
import unittest
from pathlib import Path

from tododolist.clients import BridgeError, CommandRejected, RateLimited
from tododolist.state import State
from tododolist.sync import alert_once, make_command, sync_once


def task(tid="one", **fields):
    return {"id": tid, "title": "Buy milk", "status": "needsAction", "etag": '"v1"', **fields}


class FakeGoogle:
    def __init__(self, tasks):
        self.data = {t["id"]: copy.deepcopy(t) for t in tasks}
        self.completed = []
        self.fail_complete = set()
        self.on_get = None
        self.reads = []

    def resolve_lists(self, selection):
        return [{"id": "real-list-id", "title": "My Tasks"}]

    def tasks(self, list_id, updated_min=None):
        self.reads.append(updated_min)
        return copy.deepcopy(list(self.data.values()))

    def get(self, list_id, tid):
        if self.on_get:
            self.on_get(tid)
        return copy.deepcopy(self.data[tid])

    def complete(self, list_id, item):
        if item["id"] in self.fail_complete:
            raise BridgeError("completion failed")
        self.completed.append(item["id"])
        self.data[item["id"]]["status"] = "completed"
        # Model Google's possible parent completion effect.
        for child in self.data.values():
            if child.get("parent") == item["id"]:
                child["status"] = "completed"


class FakeTodoist:
    def __init__(self):
        self.receipts = {}
        self.calls = []
        self.updates = []
        self.alerts = []
        self.labels = []
        self.fail_titles = set()
        self.reject_updates = False
        self.lose_reply = False
        self.on_execute = None
        self.project = "inbox-id"

    def inbox(self):
        return "inbox-id"

    def destination(self):
        return self.project

    def ensure_label(self, name):
        self.labels.append(name)

    def execute(self, command):
        if command["type"] == "item_update":
            if self.reject_updates:
                raise CommandRejected("item not found")
            self.updates.append(copy.deepcopy(command))
            return None
        if command["args"].get("priority") == 4:
            self.alerts.append(copy.deepcopy(command))
            return "alert"
        self.calls.append(copy.deepcopy(command))
        if self.on_execute:
            self.on_execute(command)
        if command["args"]["content"] in self.fail_titles:
            raise BridgeError("copy rejected")
        self.receipts.setdefault(command["uuid"], "todo-" + command["uuid"])
        if self.lose_reply:
            self.lose_reply = False
            raise BridgeError("response lost after successful copy")
        return self.receipts[command["uuid"]]


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "sync.sqlite3"
        self.state = State(self.path)
        self.todoist = FakeTodoist()

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def sync(self, google, **kwargs):
        return sync_once(google, self.todoist, self.state, **kwargs)

    def test_due_date_and_notes_preserved_in_inbox_then_completed(self):
        google = FakeGoogle([task(due="2026-11-18T00:00:00.000Z", notes="Whole milk")])
        result = self.sync(google)
        args = self.todoist.calls[0]["args"]
        self.assertEqual(args["project_id"], "inbox-id")
        self.assertEqual(args["due"], {"date": "2026-11-18", "is_recurring": False})
        self.assertIn("Whole milk", args["description"])
        self.assertEqual(google.completed, ["one"])
        self.assertEqual((result.copied, result.completed, result.errors), (1, 1, 0))

    def test_undated_task_stays_undated(self):
        self.sync(FakeGoogle([task()]))
        self.assertNotIn("due", self.todoist.calls[0]["args"])

    def test_completed_deleted_hidden_tasks_are_excluded(self):
        google = FakeGoogle(
            [
                task("done", status="completed"),
                task("deleted", deleted=True),
                task("hidden", hidden=True),
            ]
        )
        self.sync(google)
        self.assertEqual(self.todoist.calls, [])
        self.assertEqual(google.completed, [])

    def test_copy_failure_never_completes_google(self):
        self.todoist.fail_titles.add("Buy milk")
        google = FakeGoogle([task()])
        result = self.sync(google)
        self.assertEqual(result.errors, 1)
        self.assertEqual(google.completed, [])

    def test_google_failure_retries_without_a_second_todoist_copy(self):
        google = FakeGoogle([task()])
        google.fail_complete.add("one")
        self.sync(google)
        self.state.close()
        self.state = State(self.path)  # Simulate restart with the persisted receipt.
        google.fail_complete.clear()
        self.sync(google)
        self.assertEqual(len(self.todoist.calls), 1)
        self.assertEqual(google.completed, ["one"])

    def test_lost_copy_reply_replays_exact_uuid_and_payload(self):
        google = FakeGoogle([task()])
        self.todoist.lose_reply = True
        self.sync(google)
        self.assertEqual(google.completed, [])
        self.sync(google)
        self.assertEqual(self.todoist.calls[0], self.todoist.calls[1])
        self.assertEqual(len(self.todoist.receipts), 1)
        self.assertEqual(google.completed, ["one"])

    def test_dry_run_has_no_remote_writes_or_receipts(self):
        google = FakeGoogle([task()])
        result = self.sync(google, dry_run=True)
        self.assertEqual(result.pending, 1)
        self.assertEqual(self.todoist.calls, [])
        self.assertEqual(google.completed, [])
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM transfers").fetchone()[0], 0)

    def test_all_copies_happen_before_any_google_completion(self):
        google = FakeGoogle([task("parent"), task("child", parent="parent")])
        self.todoist.on_execute = lambda _: self.assertEqual(google.completed, [])
        self.sync(google)
        self.assertEqual(google.completed, ["child", "parent"])

    def test_failed_child_copy_keeps_parent_open(self):
        google = FakeGoogle([task("parent", title="Parent"), task("child", parent="parent")])
        self.todoist.fail_titles.add("Buy milk")
        self.sync(google)
        self.assertEqual(google.completed, [])

    def test_new_child_added_while_copying_keeps_parent_open(self):
        google = FakeGoogle([task("parent")])
        self.todoist.on_execute = lambda _: google.data.update(
            {"new-child": task("new-child", parent="parent")}
        )
        self.sync(google)
        self.assertEqual(google.completed, [])

    def test_edit_before_completion_keeps_source_and_parent_open(self):
        google = FakeGoogle([task("parent", title="Parent"), task("child", parent="parent")])

        def edit(tid):
            if tid == "child":
                google.data[tid]["due"] = "2026-12-01T00:00:00Z"

        google.on_get = edit
        self.sync(google)
        self.assertEqual(google.completed, [])

    def test_edited_task_updates_todoist_copy_then_completes(self):
        google = FakeGoogle([task(due="2026-11-18T00:00:00Z")])
        google.fail_complete.add("one")
        self.sync(google)
        google.fail_complete.clear()
        google.data["one"]["title"] = "Edited"
        del google.data["one"]["due"]
        result = self.sync(google)
        self.assertEqual((result.updated, result.completed, result.errors), (1, 1, 0))
        self.assertEqual(len(self.todoist.calls), 1)  # No duplicate copy.
        args = self.todoist.updates[0]["args"]
        self.assertEqual(args["id"], "todo-" + self.todoist.calls[0]["uuid"])
        self.assertEqual(args["content"], "Edited")
        self.assertIsNone(args["due"])  # Removed date is cleared in Todoist too.
        self.assertEqual(google.completed, ["one"])

    def test_edit_that_todoist_rejects_is_reported_once(self):
        google = FakeGoogle([task()])
        google.fail_complete.add("one")
        self.sync(google)
        google.fail_complete.clear()
        google.data["one"]["title"] = "Edited"
        self.todoist.reject_updates = True
        with self.assertLogs("tododolist.sync", "WARNING") as logs:
            first = self.sync(google)
        self.assertEqual((first.errors, first.pending), (0, 1))
        self.assertEqual(len(logs.output), 1)
        with self.assertNoLogs("tododolist.sync", "WARNING"):
            second = self.sync(google)
        self.assertEqual(second.pending, 1)
        self.assertEqual(google.completed, [])

    def test_edit_without_known_copy_id_is_reported_once(self):
        google = FakeGoogle([task()])
        google.fail_complete.add("one")
        self.sync(google)
        google.fail_complete.clear()
        self.state.db.execute("UPDATE transfers SET todoist_id=NULL")
        self.state.db.commit()
        google.data["one"]["title"] = "Edited"
        self.sync(google)
        with self.assertNoLogs("tododolist.sync", "WARNING"):
            self.sync(google)
        self.assertEqual(self.todoist.updates, [])
        self.assertEqual(google.completed, [])

    def test_reopened_transferred_task_not_copied_again(self):
        google = FakeGoogle([task()])
        self.sync(google)
        google.data["one"]["status"] = "needsAction"
        with self.assertLogs("tododolist.sync", "WARNING"):
            self.sync(google)
        with self.assertNoLogs("tododolist.sync", "WARNING"):
            self.sync(google)  # Reported once, not every poll.
        self.assertEqual(len(self.todoist.calls), 1)
        self.assertEqual(len(google.completed), 1)

    def test_label_added_to_every_copy(self):
        google = FakeGoogle([task("a"), task("b")])
        self.sync(google)
        self.assertEqual(self.todoist.labels, ["Google Tasks"])
        for call in self.todoist.calls:
            self.assertEqual(call["args"]["labels"], ["Google Tasks"])

    def test_blank_label_adds_none(self):
        self.sync(FakeGoogle([task()]), label="")
        self.assertNotIn("labels", self.todoist.calls[0]["args"])
        self.assertEqual(self.todoist.labels, [])

    def test_configured_project_used_as_destination(self):
        self.todoist.project = "work-project"
        self.sync(FakeGoogle([task()]))
        self.assertEqual(self.todoist.calls[0]["args"]["project_id"], "work-project")

    def test_subtasks_nested_under_parent_copy(self):
        google = FakeGoogle(
            [
                task("grandchild", parent="child"),
                task("child", parent="parent"),
                task("parent", title="Parent"),
            ]
        )
        self.sync(google)
        by_id = {
            c["args"]["description"].split("Google task ID: ")[1]: c for c in self.todoist.calls
        }
        self.assertNotIn("parent_id", by_id["parent"]["args"])
        self.assertEqual(by_id["child"]["args"]["parent_id"], "todo-" + by_id["parent"]["uuid"])
        self.assertEqual(by_id["grandchild"]["args"]["parent_id"], "todo-" + by_id["child"]["uuid"])
        self.assertEqual(google.completed, ["grandchild", "child", "parent"])

    def test_child_waits_when_parent_copy_fails(self):
        google = FakeGoogle([task("parent", title="Parent"), task("child", parent="parent")])
        self.todoist.fail_titles.add("Parent")
        result = self.sync(google)
        self.assertEqual(self.todoist.calls[-1]["args"]["content"], "Parent")
        self.assertEqual(len(self.todoist.calls), 1)  # Child not copied flat.
        self.assertEqual(result.pending, 1)
        self.todoist.fail_titles.clear()
        self.sync(google)
        child = self.todoist.calls[-1]
        self.assertEqual(child["args"]["parent_id"], "todo-" + self.todoist.calls[1]["uuid"])
        self.assertEqual(google.completed, ["child", "parent"])

    def test_completion_rereads_only_changes(self):
        google = FakeGoogle([task("a"), task("b"), task("c")])
        self.sync(google)
        full_reads = [r for r in google.reads if r is None]
        self.assertEqual(len(full_reads), 2)  # Initial read and one fresh read.
        self.assertEqual(len([r for r in google.reads if r]), 3)  # One small read per task.

    def test_auth_alert_sent_once_and_rearmed(self):
        alert_once(self.state, self.todoist, "expired")
        alert_once(self.state, self.todoist, "expired")
        self.assertEqual(len(self.todoist.alerts), 1)
        self.assertEqual(self.todoist.alerts[0]["args"]["project_id"], "inbox-id")
        self.state.clear_alert("google-auth")
        alert_once(self.state, self.todoist, "expired again")
        self.assertEqual(len(self.todoist.alerts), 2)

    def test_rate_limit_stops_the_pass(self):
        google = FakeGoogle([task("first"), task("second")])

        def limited(_):
            raise RateLimited("Todoist", 120)

        self.todoist.execute = limited
        with self.assertRaises(RateLimited):
            self.sync(google)
        self.assertEqual(google.completed, [])

    def test_lock_blocks_overlapping_processes(self):
        with self.assertRaisesRegex(BridgeError, "Another sync"):
            State(self.path)

    def test_long_title_preserved_in_description(self):
        title = "x" * 1024
        command = make_command(task(title=title), {"id": "list"}, "inbox")
        self.assertEqual(len(command["args"]["content"]), 500)
        self.assertIn(title, command["args"]["description"])

    def test_invalid_due_date_never_copied_or_completed(self):
        google = FakeGoogle([task(due="not a date")])
        result = self.sync(google)
        self.assertEqual(result.errors, 1)
        self.assertEqual(self.todoist.calls, [])
        self.assertEqual(google.completed, [])

    def test_todoist_receipt_saved_before_google_completion(self):
        google = FakeGoogle([task()])

        def check(*_):
            row = self.state.db.execute("SELECT * FROM transfers").fetchone()
            self.assertEqual(row["accepted"], 1)
            self.assertIsNotNone(row["todoist_id"])
            google.data["one"]["status"] = "completed"

        google.complete = check
        self.sync(google)


if __name__ == "__main__":
    unittest.main()
