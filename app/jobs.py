"""Background job manager = safe-mode for huge libraries (plan.md §10).

The sort runs in a worker thread, streams files one at a time, applies the
confident ones immediately, and queues uncertain/unmatched ones for the UI —
so even a 20k-file library stays responsive and never locks the system up.
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path

from . import duplicates, engine
from .config import settings
from .logging_setup import get_logger
from .scanner import scan

log = get_logger("ksorter.jobs")


class JobState:
    def __init__(self) -> None:
        self.id = ""
        self.status = "idle"          # idle|scanning|sorting|done|error
        self.message = ""
        self.source = ""
        self.dest = ""
        self.batch_id = ""
        self.apply = True
        self.processed = 0
        self.moved = 0
        self.skipped = 0
        self.errors = 0
        self.review: list[dict] = []
        self.manual: list[dict] = []
        self.duplicates: list[dict] = []
        self.export_path = ""

    def snapshot(self) -> dict:
        return {
            "id": self.id, "status": self.status, "message": self.message,
            "source": self.source, "dest": self.dest,
            "processed": self.processed, "moved": self.moved,
            "skipped": self.skipped, "errors": self.errors,
            "review_count": len(self.review), "manual_count": len(self.manual),
            "duplicate_count": len(self.duplicates),
            "review": self.review, "manual": self.manual,
            "duplicates": self.duplicates, "export_path": self.export_path,
        }


class JobManager:
    def __init__(self) -> None:
        self.state = JobState()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, source: str, dest: str, apply: bool = True) -> dict:
        if self.running:
            return {"ok": False, "error": "A sort is already running."}
        st = JobState()
        st.id = uuid.uuid4().hex[:8]
        st.batch_id = uuid.uuid4().hex[:12]
        st.source, st.dest, st.apply = source, dest, apply
        st.status = "scanning"
        st.message = "Starting…"
        self.state = st
        self._thread = threading.Thread(
            target=self._run, args=(st,), daemon=True, name="ksorter-sort")
        self._thread.start()
        return {"ok": True, "id": st.id}

    def _run(self, st: JobState) -> None:
        try:
            dest_root = Path(st.dest)
            all_items = []
            st.status = "sorting"
            for vf in scan(st.source):
                item = engine.build_plan_item(vf, dest_root)
                all_items.append(item)
                if st.apply and item.status == "auto":
                    result = engine.apply_item(item, st.batch_id)
                    if result["status"] == "moved":
                        st.moved += 1
                    elif result["status"] == "skipped":
                        st.skipped += 1
                    else:
                        st.errors += 1
                        st.manual.append(item.as_dict())
                elif item.status == "confirm":
                    st.review.append(item.as_dict())
                else:  # manual
                    st.manual.append(item.as_dict())
                st.processed += 1
                if st.processed % 25 == 0:
                    st.message = f"Processed {st.processed} files…"

            # Duplicate detection over the source set (flag only, never delete).
            st.duplicates = duplicates.detect(st.source)
            if not st.apply:
                st.export_path = str(engine.export_plan_csv(all_items))

            st.status = "done"
            st.message = (f"Done. {st.moved} sorted, {len(st.review)} to confirm, "
                          f"{len(st.manual)} manual, {st.skipped} skipped.")
            log.info("Sort %s complete: %s", st.id, st.snapshot()
                     | {"review": "...", "manual": "...", "duplicates": "..."})
        except Exception as exc:  # noqa: BLE001 - report, log full detail
            st.status = "error"
            st.message = f"Sort failed: {exc}. See the log file for full details."
            log.exception("Sort %s failed", st.id)

    # ---- resolving the review queue ----------------------------------
    def resolve(self, item_id: str, group_id: str | None,
                member_id: str | None, learn: bool = True) -> dict:
        st = self.state
        idx = next((i for i, it in enumerate(st.review) if it["id"] == item_id), None)
        if idx is None:
            return {"ok": False, "error": "Item not found in review queue."}
        item_dict = st.review[idx]
        item = engine.PlanItem(**item_dict)

        if not group_id:
            return {"ok": False, "error": "Pick a group."}
        from .matcher import get_index
        gi = get_index()
        group = gi.groups.get(group_id)
        member = gi.members.get(member_id) if member_id else None
        if not group:
            return {"ok": False, "error": "Unknown group."}

        lang, template = engine.get_naming()
        if member:
            item.primary_dest = str(
                engine._solo_dir(Path(st.dest), group, member, lang, template)
                / item.filename)
            item.member_id, item.member_name = member.id, member.name
        else:
            item.primary_dest = str(
                Path(st.dest) / engine._name(group, lang)
                / engine.GROUP_SUBFOLDER / item.filename)
        item.group_id, item.group_name, item.is_collab = group.id, group.name, False

        result = engine.apply_item(item, st.batch_id)
        if result["status"] == "moved":
            st.moved += 1
            if learn:
                engine.learn_correction(Path(item.filename).stem, group_id, member_id)
            st.review.pop(idx)
            return {"ok": True, "dest": result.get("dest")}
        return {"ok": False, "error": result.get("reason", result["status"])}

    def skip(self, item_id: str) -> dict:
        st = self.state
        st.review = [it for it in st.review if it["id"] != item_id]
        st.manual = [it for it in st.manual if it["id"] != item_id]
        return {"ok": True}


manager = JobManager()
