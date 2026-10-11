"""Gmail: create drafts through the Gmail API, or build a compose link that needs no setup.

Setup for the API (optional): create a Google Cloud OAuth client of type "Desktop app",
download it as credentials.json into the data folder (~/.ql), then run `uv run ql gmail-auth`.
Only the gmail.compose scope is requested: QL can create drafts but never reads your mail.
"""

import base64
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode

from ql.config import get_settings

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


def credentials_path() -> Path:
    settings = get_settings()
    return settings.gmail_credentials_file or settings.data_dir / "credentials.json"


def token_path() -> Path:
    return get_settings().data_dir / "gmail_token.json"


def is_configured() -> bool:
    return token_path().exists()


def authorize() -> Path:
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_file = credentials_path()
    if not creds_file.exists():
        raise SystemExit(f"Put your OAuth client file at {creds_file} first (see README).")
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path().write_text(creds.to_json())
    return token_path()


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(token_path()), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path().write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def create_draft(to: str, subject: str, body: str, thread_id: str | None = None) -> tuple[str, str]:
    """Create a Gmail draft. Returns (draft id, thread id). Nothing is sent."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    message: dict = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        message["threadId"] = thread_id
    draft = _service().users().drafts().create(userId="me", body={"message": message}).execute()
    return draft["id"], draft.get("message", {}).get("threadId", "")


def compose_url(to: str, subject: str, body: str) -> str:
    """A link that opens Gmail's compose window, pre-filled. Works without any API setup."""
    query = urlencode({"view": "cm", "fs": "1", "to": to, "su": subject, "body": body})
    return f"https://mail.google.com/mail/?{query}"
