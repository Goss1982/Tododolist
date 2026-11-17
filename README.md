# Tododolist

A small background programme that moves open Google Tasks into your **Todoist Inbox** (or a project you choose), labelled **Google Tasks**.

It copies each task's title, notes, links and due date, waits for Todoist to confirm the copy, then marks the Google task complete. You can run it once or leave it checking every 60 seconds. Docker and Coolify deployment files are included for an Ubuntu server.

## What it does

- Uses your default Google Tasks list unless you select another list, or all lists.
- Copies incomplete tasks, including overdue tasks and tasks without dates.
- Keeps due **dates** exactly as returned by Google, without timezone shifts.
- Keeps notes and available source links in the Todoist description.
- Keeps Google subtasks nested under their parent's Todoist copy. The original parent ID is also recorded in the description.
- Adds the label **Google Tasks** to every copy, creating the label if needed, so they are easy to filter.
- If you edit a Google task after it has been copied, the edit is applied to the Todoist copy before the Google task is completed.
- Exposes a health check, and adds a high-priority reminder to Todoist if Google access expires or is revoked.
- Completes Google tasks only after a successful Todoist copy receipt has been saved to disk.
- Uses Todoist's idempotent Sync commands and a persistent SQLite journal so retries reuse the exact same command, including after a lost response.
- Retries failed Google completions without creating another Todoist task.
- Includes pagination, rate-limit handling, an overlapping-process lock and a preview mode.

This is a **one-way transfer**, not a two-way synchroniser. Edits or completion in Todoist do not go back to Google. Already-completed Google tasks are excluded. Recurrence rules and due times are not exposed by the Google Tasks API and cannot be transferred; a dated task becomes a non-recurring Todoist task for that date. A task deliberately reopened in Google after a recorded transfer is left open rather than copied again.

## 1. Install on a laptop or desktop

Python 3.11 or newer is required. These commands are for Linux/macOS:

```bash
git clone https://github.com/Goss1982/Tododolist.git
cd Tododolist
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install --no-deps .
cp .env.example .env
mkdir -p secrets data
```

On Windows, use `py -m venv .venv`, then `.venv\Scripts\Activate.ps1` in PowerShell. Use `Copy-Item .env.example .env` and create the `secrets` and `data` folders in Explorer.

## 2. Connect Google Tasks once

1. Create or select a project in the [Google Cloud console](https://console.cloud.google.com/).
2. Enable the **Google Tasks API** for that project.
3. Configure Google Auth Platform branding and audience. For a personal Gmail account, use **External** and add your own Google account as a test user while setting up.
4. Add the scope `https://www.googleapis.com/auth/tasks`. Write access is needed to mark tasks complete.
5. Create an OAuth client of type **Desktop app**, then download its JSON file.
6. Save it as `secrets/google-client-secret.json` in this project.
7. Run:

```bash
python -m tododolist auth
```

A Google sign-in page opens. Choose the account containing your tasks and grant access. The programme saves a refresh token to `data/google-token.json`. Your Google password is never entered into this programme.

**For unattended use:** Google normally expires refresh tokens after seven days for an External OAuth app whose publishing status is Testing when using the Tasks scope. Change the app's publishing status to **In production** before doing the final authorisation. Follow any consent or verification requirements Google displays. If you authorised while Testing, run `auth` again after changing status. See [Google's refresh-token documentation](https://developers.google.com/identity/protocols/oauth2#expiration).

### Authorising directly on a headless Ubuntu server

Alternatively, install the programme and place the desktop client JSON on the server. From your laptop open an SSH session with a loopback tunnel:

```bash
ssh -L 8765:localhost:8765 YOUR_USER@YOUR_SERVER
```

Inside that session, activate the server's virtual environment and run:

```bash
python -m tododolist auth --no-browser --port 8765
```

Open the printed Google login URL in your laptop's browser. Keep the SSH session open until authorisation finishes. The callback travels through the tunnel, so the server does not need a public callback URL or an exposed port.

## 3. Add your Todoist token and select the source list

Get your personal API token from [Todoist integration settings](https://app.todoist.com/app/settings/integrations/developer) and put it in `.env`:

```dotenv
TODOIST_API_TOKEN=your_token_here
GOOGLE_TASK_LIST_ID=@default
POLL_INTERVAL_SECONDS=60
```

To see your Google task-list names and IDs:

```bash
python -m tododolist lists
```

Keep `@default` for your default Google list. To use a different list, paste its exact ID into `GOOGLE_TASK_LIST_ID`. To include all Google task lists, set `GOOGLE_TASK_LIST_ID=*`.

By default the destination is the real Todoist Inbox, identified by its API flag rather than its display name. To use another project, set `TODOIST_PROJECT_ID` to that project's ID (open the project in Todoist on the web; the ID is the last part of the address). Changing the destination later does not cause duplicates.

Every copied task gets the label set in `TODOIST_LABEL`, which defaults to `Google Tasks`. Set it to an empty value for no label. Changing these settings affects new copies only.

## 4. Preview, transfer, then run continuously

```bash
# Read both services and preview task IDs/dates; no tasks are created or completed.
python -m tododolist sync --dry-run

# Perform one transfer pass.
python -m tododolist sync

# Check continuously until stopped with Ctrl+C.
python -m tododolist watch
```

To override the source for a single run:

```bash
python -m tododolist sync --list YOUR_LIST_ID --dry-run
python -m tododolist sync --list '*'
```

`sync` exits with status 1 if a task copy or completion fails. A successful preview exits with status 0. `watch` logs failures and retries on the next pass. Parent tasks waiting for children, subtasks waiting for their parent's copy, or tasks edited during a pass, can be reported as pending. Logs show counts and source task IDs, not titles or notes.

## Knowing it is still working

After every finished pass the programme records a heartbeat in the data folder. `python -m tododolist health` exits with status 0 if a pass finished recently and 1 otherwise. The limit is ten poll intervals, at least 600 seconds; set `HEALTH_MAX_AGE_SECONDS` to change it. The Docker image runs this check automatically, so `docker compose ps` shows the container as `unhealthy` if passes stop finishing.

If Google access expires or is revoked, the programme also adds one high-priority task, **Tododolist needs re-authorising with Google**, to your Todoist destination project. It does this once per outage. After you re-authorise and a pass succeeds, the reminder is re-armed for any future outage. Complete the reminder yourself once fixed.

## Docker on your Ubuntu server

Complete Google authorisation first, either on your laptop or using the SSH method above. Put `.env` and `data/google-token.json` on the server beside `compose.yaml`. The OAuth client JSON is needed only for `auth`, not for the running worker.

The container runs as UID/GID `10001`, so allow that user to write the data folder and refresh-token file:

```bash
sudo chown -R 10001:10001 data
sudo chmod 700 data
sudo chmod 600 data/google-token.json
docker compose build

# Verify credentials and source list without changing remote tasks.
docker compose run --rm tododolist sync --dry-run

# Start the background worker.
docker compose up -d
docker compose logs -f tododolist
```

Stop it with `docker compose down`. Keep the `data` folder when updating or recreating the container. If you go back to using the local Python install, return ownership of `data` to your local user first. A `Permission denied` error writing the token or SQLite file usually means the bind-mounted data folder has the wrong owner.

## Coolify

1. Add this GitHub repository as a **Dockerfile** application, using the Dockerfile in the repository root.
2. Set the runtime environment variables below. Store the Todoist token as a secret.
3. Add persistent storage mounted at `/data`. Use one worker/replica and do not expose a public domain or HTTP port.
4. Before starting the worker, securely upload your authorised `google-token.json` into that persistent storage. Make the folder and file writable by UID/GID `10001` (`700` on the directory, `600` on the file).
5. Start the application and review its logs. Do not configure an HTTP health check; the image's own health check (`python -m tododolist health`) reports whether sync passes are finishing.

| Variable | Value |
| --- | --- |
| `TODOIST_API_TOKEN` | Your Todoist personal API token |
| `GOOGLE_TASK_LIST_ID` | `@default`, a specific Google list ID, or `*` |
| `TODOIST_PROJECT_ID` | Blank for the Inbox, or a Todoist project ID |
| `TODOIST_LABEL` | `Google Tasks` (blank for no label) |
| `POLL_INTERVAL_SECONDS` | `60` |
| `GOOGLE_TOKEN_FILE` | `/data/google-token.json` |
| `STATE_FILE` | `/data/sync.sqlite3` |
| `LOG_LEVEL` | `INFO` |

You can also deploy `compose.yaml` with Coolify's Docker Compose mode. Replace the relative `./data` bind mount with persistent host storage suitable for your Coolify installation, then place the token there. This service has no web interface.

## Reliability and limits

- Keep **one running instance**, one Google account and one Todoist account per data folder. The process lock prevents overlap when both processes share that folder; it cannot coordinate unrelated copies on different servers.
- Back up the complete `data` folder while the worker is stopped. The SQLite journal holds copy receipts, prepared commands and source snapshots. Losing it can cause duplicates on a later run. It also contains task data, so keep backups private.
- The journal is written before sending a copy and again before completing Google. After an uncertain Todoist response the same saved command UUID is replayed, allowing Todoist to acknowledge it without another task being created.
- Google completions use a fresh read and an `If-Match` ETag when available. The service rechecks active descendants before completing a parent, and copies all tasks in a list before starting its completions. This is not a transaction across two services: a subtask added in the narrow gap between that recheck and a parent completion cannot be atomically guarded. Avoid actively reorganising a list during a bulk transfer.
- A failed copy leaves the Google task open. A confirmed copy with a failed Google completion is retried without recopying.
- **Changed tasks:** if a Google task is edited after it was copied, the title, notes, links and due date of the Todoist copy are updated to match, and the Google task is then completed. The frozen copy request itself is never replaced, so this cannot create a duplicate. If the copy cannot be updated, for example because you deleted it in Todoist, the Google task is left open and reported once in the log. Find the Todoist copy using the Google task ID in its description, apply the latest details there and manually complete the Google source. Do not delete the journal to resolve this. Moving a task to a different parent in Google is not mirrored in Todoist.
- **Reopened tasks:** a task reopened in Google after a recorded transfer is left open, not copied again, and reported once in the log.
- **Subtasks:** a subtask is copied only once its parent's copy is confirmed, so it can be nested. If the parent copy fails, the subtask waits and is retried on the next pass. Subtasks whose parent has already been transferred are copied as top-level tasks.
- Google does not expose recurrence rules, due times or every first-party feature through this API. Assigned tasks are requested, but availability is still subject to Google's API and account permissions. Already-completed occurrences are not imported.
- Long Google titles are shortened to 500 characters in Todoist's title, with the complete original title included in the description. Notes are not silently truncated; a Todoist validation error leaves the source open.
- Nothing runs in GitHub on a schedule. GitHub stores the programme and runs its automated tests; the transfer runs wherever you deploy the worker.

Your `.env`, OAuth files, token, state database and `data`/`secrets` folders are excluded from Git and Docker build context. Never paste real tokens into source files or commit them to GitHub.

## Development and validation

```bash
python -m unittest discover -s tests -v
python -m tododolist --help
```

Tests cover successful transfers, dates, undated tasks, pagination, assigned tasks, API errors, rate limits, lost replies, persistent retry behaviour, preview mode, process locking, nested subtasks, labels, destination projects, edits applied to copies, one-time warnings, incremental re-reads, the health check and the re-authorisation reminder. GitHub Actions runs the suite on Python 3.11, 3.12 and 3.13. The tests use fake services and never touch real tasks.

## API references

- [Google Tasks resource and due-date limitations](https://developers.google.com/workspace/tasks/reference/rest/v1/tasks)
- [Google Tasks list API](https://developers.google.com/workspace/tasks/reference/rest/v1/tasks/list)
- [Google Python OAuth setup](https://developers.google.com/workspace/tasks/quickstart/python)
- [Todoist API v1 — Sync command UUIDs, item_add and due dates](https://developer.todoist.com/api/v1/)
