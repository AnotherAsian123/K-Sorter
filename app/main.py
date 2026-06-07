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
from . import enrich, paths, seed
from .config import settings
from .engine import get_naming, list_batches, set_naming, undo_batch
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


@app.get("/status", response_class=HTMLResponse)
async def status(request: Request):
    return templates.TemplateResponse(request, "_status.html", {
        "request": request, "state": manager.state.snapshot(),
        "running": manager.running,
    })


# ---- path validation ------------------------------------------------------
@app.post("/validate-path", response_class=HTMLResponse)
async def validate_path(request: Request, path: str = Form(""),
                        need_write: str = Form("false")):
    result = paths.validate_dir(path, need_write == "true")
    return templates.TemplateResponse(request, "_path.html", {"request": request, "r": result})


# ---- sorting --------------------------------------------------------------
@app.post("/sort", response_class=HTMLResponse)
async def sort(request: Request, source: str = Form(...), dest: str = Form(...),
               mode: str = Form("apply")):
    sv = paths.validate_dir(source, need_write=False)
    dv = paths.validate_dir(dest, need_write=True)
    if not sv["ok"] or not dv["ok"]:
        return templates.TemplateResponse(request, "_status.html", {
            "request": request, "running": False,
            "state": manager.state.snapshot(),
            "error": f"Source: {sv['reason']} | Destination: {dv['reason']}"})
    manager.start(source, dest, apply=(mode == "apply"))
    return templates.TemplateResponse(request, "_status.html", {
        "request": request, "state": manager.state.snapshot(),
        "running": manager.running})


@app.post("/resolve", response_class=HTMLResponse)
async def resolve(request: Request, item_id: str = Form(...),
                  group_id: str = Form(""), member_id: str = Form(""),
                  learn: str = Form("true")):
    res = manager.resolve(item_id, group_id or None, member_id or None,
                          learn=(learn == "true"))
    return templates.TemplateResponse(request, "_status.html", {
        "request": request, "state": manager.state.snapshot(),
        "running": manager.running,
        "error": None if res["ok"] else res.get("error")})


@app.post("/skip", response_class=HTMLResponse)
async def skip(request: Request, item_id: str = Form(...)):
    manager.skip(item_id)
    return templates.TemplateResponse(request, "_status.html", {
        "request": request, "state": manager.state.snapshot(),
        "running": manager.running})


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
    idx = get_index()
    member_ids = set(idx.group_to_members.get(group_id, []))
    out = []
    for mid in member_ids:
        m = idx.members.get(mid)
        if m and (not q or q.strip().lower() in (m.name or "").lower()):
            out.append({"id": m.id, "name": m.name, "name_ko": m.name_ko})
    return JSONResponse(sorted(out, key=lambda x: x["name"]))


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


@app.post("/undo")
async def undo(batch_id: str = Form(...)):
    return JSONResponse(undo_batch(batch_id))


@app.post("/settings")
async def save_settings(language: str = Form("en"), template: str = Form("nested")):
    set_naming(language, template)
    return JSONResponse({"ok": True})


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "counts": db.counts()}
