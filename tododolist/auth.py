from __future__ import annotations

import os
import tempfile
from pathlib import Path

from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .clients import AuthError, BridgeError

SCOPES = ["https://www.googleapis.com/auth/tasks"]


def save_token(path: Path, credentials):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".google-token-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(credentials.to_json())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def authenticate(client_file: Path, token_file: Path, port=8765, open_browser=True):
    if not client_file.is_file():
        raise BridgeError(f"Google desktop OAuth client file is missing: {client_file}")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
    credentials = flow.run_local_server(
        host="localhost",
        port=port,
        open_browser=open_browser,
        access_type="offline",
        prompt="consent",
        timeout_seconds=300,
    )
    if not credentials.refresh_token:
        raise BridgeError("Google did not issue a refresh token; run auth again and grant access")
    save_token(token_file, credentials)


def google_session(token_file: Path):
    if not token_file.is_file():
        raise AuthError("Google is not connected yet. Run: python -m tododolist auth")
    try:
        credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        if not credentials.refresh_token:
            raise AuthError("Google refresh token missing. Run the auth command again")
        if not credentials.valid:
            credentials.refresh(Request())
            save_token(token_file, credentials)
        return AuthorizedSession(credentials)
    except TransportError:
        # A network problem while refreshing is temporary, not a lost authorisation.
        raise BridgeError("Could not reach Google to refresh access; will retry") from None
    except (GoogleAuthError, ValueError, KeyError):
        raise AuthError("Google authorisation failed. Run the auth command again") from None
