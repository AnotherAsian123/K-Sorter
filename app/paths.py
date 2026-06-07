"""Path validation for the folder pickers (plan.md §6): clear, friendly reasons
for unreachable network shares, missing folders, and permission problems."""
from __future__ import annotations

import os
from pathlib import Path


def validate_dir(raw: str, need_write: bool) -> dict:
    """Return {ok, reason, exists, will_create} for a chosen folder."""
    if not raw or not raw.strip():
        return {"ok": False, "reason": "No path provided.", "exists": False}
    p = Path(raw)

    try:
        exists = p.exists()
    except OSError as exc:
        return {"ok": False, "exists": False,
                "reason": f"Path is unreachable (network share down?): {exc}"}

    if exists:
        if not p.is_dir():
            return {"ok": False, "exists": True, "reason": "That path is a file, not a folder."}
        if not os.access(p, os.R_OK):
            return {"ok": False, "exists": True,
                    "reason": "No read permission (check PUID/PGID / share permissions)."}
        if need_write and not os.access(p, os.W_OK):
            return {"ok": False, "exists": True,
                    "reason": "No write permission (check PUID/PGID / share permissions)."}
        return {"ok": True, "exists": True, "reason": "OK", "will_create": False}

    # Doesn't exist.
    if not need_write:
        return {"ok": False, "exists": False,
                "reason": "Folder not found (or the network share is unreachable)."}
    # Destination may be created if the parent is writable.
    parent = p.parent
    if parent.exists() and os.access(parent, os.W_OK):
        return {"ok": True, "exists": False, "will_create": True,
                "reason": "Folder will be created."}
    return {"ok": False, "exists": False,
            "reason": "Folder doesn't exist and its parent isn't writable."}
