"""Small clients for Google's Tasks REST API and Todoist's idempotent Sync API."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

LOG = logging.getLogger(__name__)
GOOGLE_BASE = "https://tasks.googleapis.com/tasks/v1"
TODOIST_SYNC = "https://api.todoist.com/api/v1/sync"


class BridgeError(Exception):
    """A user-actionable error that is safe to log (no HTTP bodies or credentials)."""


class ApiError(BridgeError):
    def __init__(self, service: str, status: int):
        self.status = status
        super().__init__(f"{service} returned HTTP {status}")


def retry_delay(response, attempt: int) -> float:
    value = response.headers.get("Retry-After", "")
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return min(2**attempt, 30)


def request_json(session, service: str, method: str, url: str, **kwargs) -> dict:
    # Todoist writes carry a stable command UUID. Google PATCH is deliberately
    # not retried here: re-reading first avoids applying stale completions.
    retryable = method == "GET" or service == "Todoist"
    for attempt in range(4):
        try:
            response = session.request(method, url, timeout=(10, 30), **kwargs)
        except requests.RequestException:
            if retryable and attempt < 3:
                time.sleep(min(2**attempt, 30))
                continue
            raise BridgeError(f"{service} connection failed; safe to retry the sync") from None
        if response.status_code == 429 or response.status_code >= 500:
            delay = retry_delay(response, attempt)
            if retryable and attempt < 3 and delay <= 60:
                LOG.warning("%s HTTP %s; retrying in %.0fs", service, response.status_code, delay)
                time.sleep(delay)
                continue
            # Do not immediately make another request during a long rate-limit.
            if response.status_code == 429:
                raise RateLimited(service, delay)
        if not 200 <= response.status_code < 300:
            raise ApiError(service, response.status_code)
        try:
            body = response.json()
        except ValueError:
            raise BridgeError(f"{service} returned invalid JSON") from None
        if not isinstance(body, dict):
            raise BridgeError(f"{service} returned an unexpected response")
        return body
    raise AssertionError("unreachable")


class RateLimited(BridgeError):
    def __init__(self, service: str, delay: float):
        self.delay = max(delay, 1)
        super().__init__(f"{service} rate limit; wait at least {self.delay:.0f}s")


class GoogleTasks:
    def __init__(self, session):
        self.session = session

    def _get(self, path, **params):
        return request_json(self.session, "Google Tasks", "GET", GOOGLE_BASE + path, params=params)

    def _pages(self, path, **params):
        items = []
        seen = set()
        while True:
            data = self._get(path, **params)
            items.extend(data.get("items", []))
            token = data.get("nextPageToken")
            if not token:
                return items
            if token in seen:
                raise BridgeError("Google Tasks repeated a pagination token")
            seen.add(token)
            params["pageToken"] = token

    def lists(self):
        return self._pages("/users/@me/lists", maxResults=100)

    def resolve_lists(self, selection):
        if selection == "*":
            return self.lists()
        # Resolve @default to its real ID so changing selection cannot duplicate tasks.
        return [self._get("/users/@me/lists/" + quote(selection, safe=""))]

    def tasks(self, list_id):
        return self._pages(
            "/lists/" + quote(list_id, safe="") + "/tasks",
            maxResults=100,
            showCompleted="false",
            showDeleted="false",
            showHidden="false",
            showAssigned="true",
        )

    def get(self, list_id, task_id):
        return self._get("/lists/" + quote(list_id, safe="") + "/tasks/" + quote(task_id, safe=""))

    def complete(self, list_id, task):
        headers = {"If-Match": task["etag"]} if task.get("etag") else {}
        result = request_json(
            self.session,
            "Google Tasks",
            "PATCH",
            GOOGLE_BASE
            + "/lists/"
            + quote(list_id, safe="")
            + "/tasks/"
            + quote(task["id"], safe=""),
            headers=headers,
            json={"status": "completed"},
        )
        if result.get("status") != "completed":
            raise BridgeError("Google Tasks did not confirm completion")


class Todoist:
    def __init__(self, token, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def _sync(self, **data):
        return request_json(self.session, "Todoist", "POST", TODOIST_SYNC, data=data)

    def inbox(self):
        data = self._sync(sync_token="*", resource_types=json.dumps(["projects"]))
        matches = [
            p
            for p in data.get("projects", [])
            if p.get("inbox_project") and not p.get("is_deleted") and not p.get("is_archived")
        ]
        if len(matches) != 1:
            raise BridgeError("Could not uniquely identify your Todoist Inbox")
        return str(matches[0]["id"])

    def execute(self, command):
        data = self._sync(commands=json.dumps([command]))
        result = data.get("sync_status", {}).get(command["uuid"])
        if result != "ok":
            # Even HTTP 200 can contain a rejected command. Never complete the source then.
            code = result.get("error_code", "unknown") if isinstance(result, dict) else "unknown"
            raise BridgeError(f"Todoist rejected the task (command error {code})")
        task_id = data.get("temp_id_mapping", {}).get(command["temp_id"])
        # A replay may confirm 'ok' without returning an old temp_id mapping.
        # The explicit success for the same persisted UUID is the copy receipt.
        return str(task_id) if task_id else None
