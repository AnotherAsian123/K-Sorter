"""K-Sorter FastAPI app. Sort once, sort TWICE — move each file just once. 🎵"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import database as db
from . import enrich, manage, paths, seed
from .config import settings
from .engine import get_batch_moves, get_naming, list_batches, set_naming, undo_batch
from .jobs import manager
from .logging_setup import get_logger, setup_logging
from .matcher import get_index, reload_index

log = get_logger("ksorter")
app = FastAPI(title="K-Sorter")

_BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_BASE / "templates"))
app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")


def _ensure_seed() -> None:
    """Seed on startup (from cache if present; downloads only on first run)."""
    try:
        if db.counts()["groups"] == 0:
            log.info("Empty database on startup — seeding from dataset.")
            seed.refresh_seed(force_download=False)
        reload_index()
    except Exception:  # noqa: BLE001
        log.exception("Startup seed failed; UI will offer 'Update Database'.")


@app.on_event("startup")
async def _startup() -> None:
    setup_logging()
    settings.ensure_dirs()
    db.get_conn()
    threading.Thread(target=_ensure_seed, daemon=True).start()
    if settings.watch_dir and settings.watch_dest:
        from .watch import watch_loop
        asyncio.create_task(watch_loop())


# ---- pages ----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "counts": db.counts(),
        "naming": get_naming(),
        "settings": settings,
        "state": manager.state.snapshot(),
        "batches": list_batches(),
        "last_seed": db.get_meta("last_seed_status", "never"),
    })


def _status_response(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "_status.html", {
        "request": request,
        "state": manager.state.snapshot(),
        "running": manager.running,
        "db_empty": db.counts()["groups"] == 0,
        "error": error,
    })


@app.get("/status", response_class=HTMLResponse)
async def status(request: Request):
    return _status_response(request)


# ---- path validation ------------------------------------------------------
@app.post("/validate-path", response_class=HTMLResponse)
async def validate_path(request: Request, path: str = Form(""),
                        need_write: str = Form("false")):
    result = paths.validate_dir(path, need_write == "true")
    return templates.TemplateResponse(request, "_path.html", {"request": request, "r": result})


# ---- sorting --------------------------------------------------------------
@app.post("/sort", response_class=HTMLResponse)
async def sort(request: Request, source: str = Form(""), dest: str = Form(""),
               mode: str = Form("apply")):
    # Fall back to the folders configured via env / container mounts, so they
    # only need to be typed in the UI when overriding to a different location.
    source = source.strip() or settings.source_default
    dest = dest.strip() or settings.dest_default
    if not source and not dest:
        return _status_response(request, error=(
            "No source or destination set. Map /source and /destination in the "
            "container (or enter folders above)."))
    sv = paths.validate_dir(source, need_write=False)
    dv = paths.validate_dir(dest, need_write=True)
    if not sv["ok"] or not dv["ok"]:
        return _status_response(request, error=(
            f"Source: {sv['reason']} | Destination: {dv['reason']}"))
    manager.start(source, dest, apply=(mode == "apply"))
    return _status_response(request)


@app.post("/resolve", response_class=HTMLResponse)
async def resolve(request: Request, item_id: str = Form(...),
                  group_id: str = Form(""), member_id: str = Form(""),
                  learn: str = Form("true")):
    res = manager.resolve(item_id, group_id or None, member_id or None,
                          learn=(learn == "true"))
    return _status_response(request, error=None if res["ok"] else res.get("error"))


@app.post("/skip", response_class=HTMLResponse)
async def skip(request: Request, item_id: str = Form(...)):
    manager.skip(item_id)
    return _status_response(request)


# ---- group search (for the confirm dropdown) ------------------------------
@app.get("/groups/search")
async def groups_search(q: str = ""):
    qn = q.strip().lower()
    if not qn:
        return JSONResponse([])
    idx = get_index()
    seen, out = set(), []
    for alias, ids in idx.group_alias_to_ids.items():
        if qn in alias:
            for gid in ids:
                if gid not in seen and gid in idx.groups:
                    seen.add(gid)
                    g = idx.groups[gid]
                    out.append({"id": g.id, "name": g.name, "name_ko": g.name_ko})
    return JSONResponse(out[:20])


@app.get("/members/search")
async def members_search(group_id: str, q: str = ""):
    # Query the DB directly so we get the is_current flag (current members first,
    # former members labelled, missing ones can be added).
    rows = db.query(
        "SELECT m.id, m.stage_name, m.stage_name_ko, gm.is_current "
        "FROM group_members gm JOIN members m ON m.id = gm.member_id "
        "WHERE gm.group_id = ? ORDER BY gm.is_current DESC, m.stage_name", (group_id,))
    ql = q.strip().lower()
    out = [{"id": r["id"], "name": r["stage_name"], "name_ko": r["stage_name_ko"],
            "current": bool(r["is_current"])}
           for r in rows if not ql or ql in (r["stage_name"] or "").lower()]
    return JSONResponse(out)


@app.post("/members/add")
async def members_add(group_id: str = Form(...), name: str = Form(...),
                      name_ko: str = Form("")):
    if not group_id or not name.strip():
        return JSONResponse({"ok": False, "error": "Pick a group and type a member name."})
    mid = await asyncio.to_thread(enrich.add_member, group_id, name.strip(),
                                  name_ko.strip() or None)
    if not mid:
        return JSONResponse({"ok": False, "error": "Unknown group."})
    return JSONResponse({"ok": True, "member_id": mid, "name": name.strip()})


# ---- live enrichment ------------------------------------------------------
@app.post("/enrich/search")
async def enrich_search(name: str = Form(...)):
    return JSONResponse(await asyncio.to_thread(enrich.search_group, name))


@app.post("/enrich/add")
async def enrich_add(name: str = Form(...), name_ko: str = Form("")):
    gid = await asyncio.to_thread(enrich.add_confirmed_group, name, name_ko or None)
    return JSONResponse({"ok": True, "group_id": gid})


# ---- database / undo / settings -------------------------------------------
@app.post("/update-db")
async def update_db():
    def _run():
        try:
            seed.refresh_seed(force_download=True)
            reload_index()
        except Exception:  # noqa: BLE001
            log.exception("Manual database update failed")
    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Updating database in the background…"})


@app.get("/batch/{batch_id}/moves", response_class=HTMLResponse)
async def batch_moves(request: Request, batch_id: str):
    return templates.TemplateResponse(request, "_moves.html", {
        "request": request, "moves": get_batch_moves(batch_id)})


@app.post("/undo")
async def undo(batch_id: str = Form(...)):
    return JSONResponse(undo_batch(batch_id))


@app.post("/settings")
async def save_settings(language: str = Form("en"), template: str = Form("nested")):
    set_naming(language, template)
    return JSONResponse({"ok": True})


# ---- database manager (advanced) ------------------------------------------
def _group_detail(request: Request, gid: str | None, refresh: bool = False):
    data = manage.get_group(gid) if gid else None
    headers = {"HX-Trigger": "ksRefresh"} if refresh else None
    return templates.TemplateResponse(request, "_group_detail.html",
                                      {"request": request, "data": data}, headers=headers)


@app.get("/manage", response_class=HTMLResponse)
async def manage_page(request: Request):
    return templates.TemplateResponse(request, "manage.html", {
        "request": request, "counts": db.counts()})


@app.get("/db/groups", response_class=HTMLResponse)
async def db_groups(request: Request, q: str = ""):
    return templates.TemplateResponse(request, "_group_list.html", {
        "request": request, "groups": manage.list_groups(q), "q": q})


@app.get("/db/group/{gid}", response_class=HTMLResponse)
async def db_group(request: Request, gid: str):
    return _group_detail(request, gid)


@app.post("/db/group/add", response_class=HTMLResponse)
async def db_group_add(request: Request, name: str = Form(...),
                       name_ko: str = Form(""), alias: str = Form("")):
    if not name.strip():
        return _group_detail(request, None)
    aliases = [a.strip() for a in alias.split(",") if a.strip()]
    gid = enrich.add_confirmed_group(name.strip(), name_ko.strip() or None, aliases)
    return _group_detail(request, gid, refresh=True)


@app.post("/db/group/{gid}/rename", response_class=HTMLResponse)
async def db_group_rename(request: Request, gid: str, name: str = Form(...),
                          name_ko: str = Form("")):
    manage.rename_group(gid, name, name_ko)
    return _group_detail(request, gid, refresh=True)


@app.post("/db/group/{gid}/alias", response_class=HTMLResponse)
async def db_group_alias(request: Request, gid: str, alias: str = Form(...)):
    manage.add_group_alias(gid, alias)
    return _group_detail(request, gid)


@app.post("/db/group/{gid}/active", response_class=HTMLResponse)
async def db_group_active(request: Request, gid: str, active: str = Form("1")):
    manage.set_group_active(gid, active == "1")
    return _group_detail(request, gid, refresh=True)


@app.post("/db/group/{gid}/delete", response_class=HTMLResponse)
async def db_group_delete(request: Request, gid: str):
    manage.delete_group(gid)
    return _group_detail(request, None, refresh=True)


@app.post("/db/group/{gid}/member/add", response_class=HTMLResponse)
async def db_member_add(request: Request, gid: str, name: str = Form(...),
                        name_ko: str = Form("")):
    if name.strip():
        await asyncio.to_thread(enrich.add_member, gid, name.strip(), name_ko.strip() or None)
    return _group_detail(request, gid, refresh=True)


@app.post("/db/group/{gid}/member/{mid}/rename", response_class=HTMLResponse)
async def db_member_rename(request: Request, gid: str, mid: str,
                           name: str = Form(...), name_ko: str = Form("")):
    manage.rename_member(mid, name, name_ko)
    return _group_detail(request, gid)


@app.post("/db/group/{gid}/member/{mid}/current", response_class=HTMLResponse)
async def db_member_current(request: Request, gid: str, mid: str, current: str = Form("1")):
    manage.set_member_current(gid, mid, current == "1")
    return _group_detail(request, gid)


@app.post("/db/group/{gid}/member/{mid}/remove", response_class=HTMLResponse)
async def db_member_remove(request: Request, gid: str, mid: str):
    manage.remove_member(gid, mid)
    return _group_detail(request, gid, refresh=True)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "counts": db.counts()}
