"""Environment-driven configuration and the demo clock."""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("CHASER_DATA_DIR", REPO_ROOT / "data"))
DEFAULT_DB_PATH = os.getenv("CHASER_DB_PATH", str(REPO_ROOT / ".data" / "chaser.db"))
# Sessions live next to the database by default, so pointing CHASER_DB_PATH at a writable
# location (AgentCore's code directory is read-only; /tmp is not) also moves the sessions.
SESSIONS_DIR = os.getenv("CHASER_SESSIONS_DIR", str(Path(DEFAULT_DB_PATH).parent / "sessions"))
DEFAULT_TODAY = "2026-09-12"


def today() -> date:
    """Return the demo 'today'. All ages and deadlines are computed relative to this date."""
    raw = os.getenv("DEMO_TODAY", DEFAULT_TODAY)
    return date.fromisoformat(raw)


def week_start(anchor: date | None = None) -> date:
    """Monday of the week containing ``anchor`` (default: today)."""
    anchor = anchor or today()
    return anchor - timedelta(days=anchor.weekday())


def days_between(earlier: str | date, later: str | date) -> int:
    """Whole days from ``earlier`` to ``later`` (negative if later comes first)."""
    if isinstance(earlier, str):
        earlier = date.fromisoformat(earlier)
    if isinstance(later, str):
        later = date.fromisoformat(later)
    return (later - earlier).days


def configure_logging() -> None:
    """Configure root logging once; set the strands logger to INFO."""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("strands").setLevel(logging.INFO)
