import json
import unittest
from unittest.mock import Mock, patch

import requests

from tododolist.clients import (
    ApiError,
    BridgeError,
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


if __name__ == "__main__":
    unittest.main()
