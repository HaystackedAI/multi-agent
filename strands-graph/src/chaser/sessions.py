"""Session manager factory: local files by default, S3 when SESSION_BUCKET is set (AgentCore)."""

from __future__ import annotations

import os

from strands.session import FileSessionManager, S3SessionManager
from strands.session.session_manager import SessionManager

from .config import SESSIONS_DIR

ASK_SESSION_ID = "chaser-owner-chat"


def make_session_manager(session_id: str) -> SessionManager:
    """Return a session manager for ``session_id``.

    AgentCore Runtime has an ephemeral filesystem, so when ``SESSION_BUCKET`` is set the
    conversation is persisted to S3 under ``SESSION_PREFIX`` (default ``chaser/sessions``).
    """
    bucket = os.getenv("SESSION_BUCKET")
    if bucket:
        return S3SessionManager(
            session_id=session_id,
            bucket=bucket,
            prefix=os.getenv("SESSION_PREFIX", "chaser/sessions"),
            region_name=os.getenv("AWS_REGION", "us-east-1"),
        )
    return FileSessionManager(session_id=session_id, storage_dir=SESSIONS_DIR)
