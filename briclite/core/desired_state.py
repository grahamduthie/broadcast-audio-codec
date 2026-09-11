"""Durable operator intent for recovering after a service restart.

The codec deliberately distinguishes an operator-requested disconnect from a
temporary process/device failure.  This file records only the former intent:
if it says the link is active, a fresh Briclite process must restore it.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any


logger = logging.getLogger("desired_state")


class DesiredState:
    """Atomically store the requested codec state outside the service process."""

    def __init__(self, directory: str | None = None):
        base = directory or os.environ.get("BRICLITE_STATE_DIR")
        if base is None:
            base = os.path.join(os.path.expanduser("~"), ".local", "state", "briclite")
        self.directory = Path(base)
        self.path = self.directory / "desired-link.json"

    def load(self) -> dict[str, Any]:
        try:
            with self.path.open() as source:
                value = json.load(source)
            if isinstance(value, dict) and value.get("active") is True:
                return value
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable desired-link state: %s", exc)
        return {}

    def save(self, value: dict[str, Any]) -> None:
        self.directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        payload = {"version": 1, "active": True, **value}
        fd, temporary = tempfile.mkstemp(prefix=".desired-link-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as target:
                json.dump(payload, target, sort_keys=True)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
