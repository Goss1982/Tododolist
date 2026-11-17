import json
import unittest
from unittest.mock import Mock, patch

import requests

from tododolist.clients import (
    ApiError,
    BridgeError,
    CommandRejected,
    GoogleTasks,
    RateLimited,
    Todoist,
    request_json,
)


def response(body, status=200, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = body
    return result


class ClientTests(unittest.TestCase):
    def test_google_tasks_paginates_and_includes_assigned_tasks(self):
        session = Mock()
        session.request.side_effect = [
            response({"items": [{"id": "one"}], "nextPageToken": "next"}),
            response({"items": [{"id": "two"}]}),
        ]
        self.assertEqual(len(GoogleTasks(session).tasks("list/id")), 2)
        calls = session.request.call_args_list
        self.assertIn("list%2Fid", calls[0].args[1])
        self.assertEqual(calls[0].kwargs["params"]["showAssigned"], "true")
        self.assertEqual(calls[1].kwargs["params"]["pageToken"], "next")

    def test_google_default_resolves_real_list_id(self):
        session = Mock()
        session.request.return_value = response({"id": "real", "title": "My Tasks"})
        self.assertEqual(GoogleTasks(session).resolve_lists("@default")[0]["id"], "real")

    def test_google_lists_paginates(self):
        session = Mock()
        session.request.side_effect = [
            response({"items": [{"id": "a"}], "nextPageToken": "b"}),
            response({"items": [{"id": "b"}]}),
        ]
        self.assertEqual(len(GoogleTasks(session).resolve_lists("*")), 2)

    def test_completion_uses_etag_and_status_only(self):
        session = Mock()
        session.request.return_value = response({"status": "completed"})
        GoogleTasks(session).complete("list", {"id": "task", "etag": '"v2"'})
        kwargs = session.request.call_args.kwargs
        self.assertEqual(kwargs["headers"], {"If-Match": '"v2"'})
        self.assertEqual(kwargs["json"], {"status": "completed"})

    def test_google_conditional_patch_not_retried(self):
        session = Mock()
        session.request.return_value = response({}, status=412)
        with self.assertRaises(ApiError):
            GoogleTasks(session).complete("l", {"id": "t", "etag": "v"})
        self.assertEqual(session.request.call_count, 1)

    def test_todoist_finds_inbox_by_flag_not_name(self):
        session = Mock()
        session.request.return_value = response(
            {
                "projects": [
                    {"id": "fake", "name": "Inbox", "inbox_project": False},
                    {"id": "real", "name": "Boîte", "inbox_project": True},
                ]
            }
        )
        self.assertEqual(Todoist("secret", session).inbox(), "real")

    def test_todoist_http_200_command_error_is_not_success(self):
        session = Mock()
        session.request.return_value = response({"sync_status": {"uuid": {"error_code": 20}}})
        with self.assertRaises(BridgeError):
            Todoist("secret", session).execute({"uuid": "uuid", "temp_id": "temp"})

    def test_todoist_missing_receipt_is_not_success(self):
        session = Mock()
        session.request.return_value = response({})
        with self.assertRaises(BridgeError):
            Todoist("secret", session).execute({"uuid": "uuid", "temp_id": "temp"})

    def test_replayed_command_success_without_mapping_is_a_receipt(self):
        session = Mock()
        session.request.return_value = response({"sync_status": {"uuid": "ok"}})
        self.assertIsNone(Todoist("secret", session).execute({"uuid": "uuid", "temp_id": "temp"}))

    @patch("tododolist.clients.time.sleep")
    def test_ambiguous_todoist_request_retries_same_command(self, sleep):
        session = Mock()
        session.request.side_effect = [
            requests.Timeout(),
            response({"sync_status": {"uuid": "ok"}, "temp_id_mapping": {"temp": "new-task"}}),
        ]
        command = {"uuid": "uuid", "temp_id": "temp", "args": {"content": "Milk"}}
        self.assertEqual(Todoist("secret", session).execute(command), "new-task")
        calls = session.request.call_args_list
        self.assertEqual(calls[0].kwargs["data"], calls[1].kwargs["data"])
        self.assertEqual(json.loads(calls[0].kwargs["data"]["commands"]), [command])

    @patch("tododolist.clients.time.sleep")
    def test_long_retry_after_surfaces_to_worker_without_early_retry(self, sleep):
        session = Mock()
        session.request.return_value = response({}, status=429, headers={"Retry-After": "120"})
        with self.assertRaises(RateLimited) as raised:
            request_json(session, "Todoist", "POST", "https://api.todoist.com/api/v1/sync")
        self.assertEqual(raised.exception.delay, 120)
        self.assertEqual(session.request.call_count, 1)
        sleep.assert_not_called()

    def test_errors_do_not_expose_response_bodies(self):
        session = Mock()
        session.request.return_value = response({"private-data": "secret"}, status=401)
        with self.assertRaises(ApiError) as raised:
            request_json(session, "Todoist", "GET", "url")
        self.assertNotIn("secret", str(raised.exception))

    def test_incremental_read_includes_finished_tasks(self):
        session = Mock()
        session.request.return_value = response({"items": []})
        GoogleTasks(session).tasks("list", updated_min="2026-11-17T20:00:00.000Z")
        params = session.request.call_args.kwargs["params"]
        self.assertEqual(params["updatedMin"], "2026-11-17T20:00:00.000Z")
        self.assertEqual(params["showCompleted"], "true")
        self.assertEqual(params["showDeleted"], "true")

    def test_todoist_projects_read_once_and_project_validated(self):
        session = Mock()
        session.request.return_value = response(
            {
                "projects": [
                    {"id": "inbox", "inbox_project": True},
                    {"id": "work"},
                    {"id": "old", "is_archived": True},
                ],
                "labels": [],
            }
        )
        todoist = Todoist("secret", session, project_id="work")
        self.assertEqual(todoist.destination(), "work")
        self.assertEqual(todoist.inbox(), "inbox")
        self.assertEqual(session.request.call_count, 1)
        with self.assertRaisesRegex(BridgeError, "TODOIST_PROJECT_ID"):
            Todoist("secret", session, project_id="old").destination()

    def test_label_created_only_when_missing(self):
        session = Mock()
        sent = []

        def reply(*args, **kwargs):
            if "commands" not in kwargs["data"]:
                return response({"projects": [], "labels": [{"name": "Other"}]})
            command = json.loads(kwargs["data"]["commands"])[0]
            sent.append(command)
            return response({"sync_status": {command["uuid"]: "ok"}})

        session.request.side_effect = reply
        todoist = Todoist("secret", session)
        todoist.ensure_label("Google Tasks")
        todoist.ensure_label("Google Tasks")
        self.assertEqual(
            [(c["type"], c["args"]) for c in sent], [("label_add", {"name": "Google Tasks"})]
        )
        self.assertEqual(session.request.call_count, 2)

    def test_existing_label_not_recreated(self):
        session = Mock()
        session.request.return_value = response(
            {"projects": [], "labels": [{"name": "Google Tasks"}]}
        )
        Todoist("secret", session).ensure_label("Google Tasks")
        self.assertEqual(session.request.call_count, 1)

    def test_rejected_command_has_its_own_error(self):
        session = Mock()
        session.request.return_value = response({"sync_status": {"u": {"error_code": 22}}})
        with self.assertRaises(CommandRejected):
            Todoist("secret", session).execute({"type": "item_update", "uuid": "u", "args": {}})

    def test_update_success_needs_no_temp_id(self):
        session = Mock()
        session.request.return_value = response({"sync_status": {"u": "ok"}})
        self.assertIsNone(
            Todoist("secret", session).execute({"type": "item_update", "uuid": "u", "args": {}})
        )


if __name__ == "__main__":
    unittest.main()
