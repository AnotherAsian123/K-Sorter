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
        self.mode = "sort"            # 'sort' | 'audit'
        self.phase = ""               # human label for the current phase
        self.total = 0                # videos discovered (denominator for the bar)
        self.processed = 0
        self.moved = 0
        self.skipped = 0
        self.errors = 0
        self.review: list[dict] = []
        self.manual: list[dict] = []
        self.duplicates: list[dict] = []
        self.export_path = ""

    @property
    def percent(self) -> int:
        if self.status in ("done", "error"):
            return 100
        if self.total <= 0:
            return 0
        return min(99, int(self.processed * 100 / self.total))

    def snapshot(self) -> dict:
        return {
            "id": self.id, "status": self.status, "message": self.message,
            "mode": self.mode,
            "phase": self.phase, "source": self.source, "dest": self.dest,
            "total": self.total, "processed": self.processed, "percent": self.percent,
            "moved": self.moved, "skipped": self.skipped, "errors": self.errors,
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

    def start_audit(self, dest: str) -> dict:
        if self.running:
            return {"ok": False, "error": "A job is already running."}
        st = JobState()
        st.id = uuid.uuid4().hex[:8]
        st.batch_id = uuid.uuid4().hex[:12]
        st.source = st.dest = dest
        st.mode = "audit"
        st.status = "scanning"
        st.message = "Checking the sorted folder…"
        self.state = st
        self._thread = threading.Thread(
            target=self._run_audit, args=(st,), daemon=True, name="ksorter-audit")
        self._thread.start()
        return {"ok": True, "id": st.id}

    def _run_audit(self, st: JobState) -> None:
        try:
            from . import audit
            st.status = "sorting"
            st.phase = "Checking"

            def _tick():
                st.processed += 1
                st.total = st.processed
                if st.processed % 25 == 0:
                    st.message = f"Checked {st.processed} files… ({len(st.review)} flagged)"

            for item in audit.audit_destination(Path(st.dest), on_scan=_tick):
                st.review.append(item.as_dict())

            st.status = "done"
            st.phase = "Done"
            flagged = len(st.review)
            st.message = (f"Checked {st.processed} sorted files — "
                          f"{flagged} possible miscategorisation(s) flagged."
                          if flagged else
                          f"Checked {st.processed} sorted files — all look correctly filed. ✓")
            log.info("Audit %s complete: %d scanned, %d flagged", st.id, st.processed, flagged)
        except Exception as exc:  # noqa: BLE001
            st.status = "error"
            st.message = f"Audit failed: {exc}. See the log file for full details."
            log.exception("Audit %s failed", st.id)

    def _run(self, st: JobState) -> None:
        try:
            dest_root = Path(st.dest)

            # Phase 1 — scan. Stream the source so the count climbs live, giving
            # immediate feedback even on a slow network share.
            st.status = "scanning"
            st.phase = "Scanning"
            files = []
            for vf in scan(st.source):
                files.append(vf)
                st.total = len(files)
                if st.total % 50 == 0:
                    st.message = f"Scanning… found {st.total} videos so far"
            st.message = f"Found {st.total} videos."
            if st.total == 0:
                st.status = "done"
                st.phase = "Done"
                st.message = "No videos found in the source folder."
                return

            # Phase 2 — sort, with a determinate progress bar.
            st.status = "sorting"
            st.phase = "Sorting" if st.apply else "Previewing (dry run)"
            all_items = []
            for vf in files:
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
                if st.processed % 10 == 0 or st.processed == st.total:
                    st.message = f"Sorting {st.processed} of {st.total}…"

            # Phase 3 — duplicate detection (flag only, never delete).
            st.phase = "Checking duplicates"
            st.message = "Checking for duplicates…"
            st.duplicates = duplicates.detect(st.source)
            if not st.apply:
                st.export_path = str(engine.export_plan_csv(all_items))

            st.status = "done"
            st.phase = "Done"
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
        # Items can live in either queue (confirm or manual) — resolve both.
        queue = idx = None
        for lst in (st.review, st.manual):
            j = next((i for i, it in enumerate(lst) if it["id"] == item_id), None)
            if j is not None:
                queue, idx = lst, j
                break
        if queue is None:
            return {"ok": False, "error": "Item not found (already handled?)."}
        item = engine.PlanItem(**queue[idx])

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
            # Remember the user's placement so the audit won't re-flag it.
            engine.record_decision(item.filename,
                                   engine.rel_location(result.get("dest", ""), Path(st.dest)))
            queue.pop(idx)
            return {"ok": True, "dest": result.get("dest")}
        return {"ok": False, "error": result.get("reason", result["status"])}

    def resolve_collab(self, item_id: str, action: str) -> dict:
        """Apply a user's decision for a multi-group video.
        action: 'replicate' | 'special' | 'group:<group_id>'."""
        st = self.state
        queue = idx = None
        for lst in (st.review, st.manual):
            j = next((i for i, it in enumerate(lst) if it["id"] == item_id), None)
            if j is not None:
                queue, idx = lst, j
                break
        if queue is None:
            return {"ok": False, "error": "Item not found (already handled?)."}
        item = engine.PlanItem(**queue[idx])
        dest_root = Path(st.dest)
        lang, _template = engine.get_naming()

        if action == "replicate":
            item.is_collab = True  # primary_dest + replica_dests already set
        elif action == "special":
            item.is_collab = False
            item.replica_dests = []
            item.primary_dest = str(dest_root / engine.SPECIAL_STAGES / item.filename)
        elif action.startswith("group:"):
            from .matcher import get_index
            g = get_index().groups.get(action.split(":", 1)[1])
            if not g:
                return {"ok": False, "error": "Unknown group."}
            item.is_collab = False
            item.replica_dests = []
            item.primary_dest = str(
                dest_root / engine._name(g, lang) / engine.GROUP_SUBFOLDER / item.filename)
            item.group_id, item.group_name = g.id, g.name
        else:
            return {"ok": False, "error": "Unknown action."}

        result = engine.apply_item(item, st.batch_id)
        if result["status"] == "moved":
            st.moved += 1
            engine.record_decision(item.filename,
                                   engine.rel_location(result.get("dest", ""), Path(st.dest)))
            queue.pop(idx)
            return {"ok": True, "dest": result.get("dest")}
        return {"ok": False, "error": result.get("reason", result["status"])}

    def skip(self, item_id: str) -> dict:
        st = self.state
        # Skipping an AUDIT item means "this is fine where it is" — remember that
        # so future integrity checks leave it alone.
        for lst in (st.review, st.manual):
            for it in lst:
                if it["id"] == item_id and it.get("current_location"):
                    engine.record_decision(it["filename"], it["current_location"])
                    break
        st.review = [it for it in st.review if it["id"] != item_id]
        st.manual = [it for it in st.manual if it["id"] != item_id]
        return {"ok": True}


manager = JobManager()
