# K-Sorter 🎵

*Sort once, sort TWICE.* A calm, self-hosted web app that sorts a messy folder of
K-pop videos into correctly-named **group** (and **member**) folders. Built to run
as a **single Docker container** on Unraid.

> **Accuracy first.** Confident matches are sorted automatically; anything
> uncertain waits for a quick confirmation instead of being guessed. Nothing is
> ever renamed, nothing is overwritten, and **every move can be undone**.

![palette](https://img.shields.io/badge/calm-earthy%20palette-96BBBB)

---

## What it does

- 📂 You pick a **source** folder and a **destination** folder (local paths or
  network shares). It scans recursively, including nested subfolders.
- 🧠 It reads each **filename** (never modifying it) and matches it to a group —
  and, for solo fancams/focus videos, to a **member**.
- ✅ **Confident, unambiguous matches sort automatically.** Uncertain ones land in
  a "needs confirmation" queue with a precise one-tap question; unmatched ones go
  to a manual queue. You're only asked when it matters.
- 🗂️ Folder layout:
  ```
  Destination/
  ├── TWICE/
  │   ├── Group/          ← full-group videos (MVs, group stages)
  │   ├── Nayeon/         ← solo fancams / focus
  │   └── Momo/
  ├── MISAMO/             ← sub-units get their own top-level folder
  └── _Special Stages/    ← collabs (also hardlinked into each group)
  ```

## Highlights

| Feature | Notes |
|---|---|
| **Korean + nicknames + former members** | Seeded from the open CC0 `kpopnet.json` dataset. Former members are kept and flagged inactive, so their videos still sort correctly. |
| **Safe moves** | Same-filesystem = instant atomic rename. Cross-filesystem = copy → verify size → delete (optional SHA-256). Never overwrites. |
| **Undo** | Every batch is journaled; one click puts files back. |
| **Learns from you** | Confirm an odd name once and it's remembered as an alias. |
| **Duplicate detection** | Flags suspected dupes (size + partial hash) in the UI and `duplicates.csv` — never deletes. |
| **Collabs** | Multi-group videos → `_Special Stages/`, replicated per group via hardlinks. |
| **Watch-folder** | Optional env-var folder; new drops auto-sort (confident only). |
| **Dry-run export** | Preview the whole plan as CSV before moving anything. |
| **Safe-mode** | Streaming scan + background job + paginated UI; tens of thousands of files stay responsive. |
| **Calming UI** | Frosted-glass earthy palette, gentle animations, light/dark, `prefers-reduced-motion`. |

## Tech stack (100% free / self-hosted)

FastAPI · HTMX + Alpine.js · SQLite · RapidFuzz · Uvicorn · Docker. No paid APIs,
no cloud, no hosting fees. Live lookups for unknown groups use the free Wikipedia API.

---

## Run on Unraid

1. Add the template (`unraid-template.xml`) via Community Applications, or pull
   `ghcr.io/anotherasian123/k-sorter:latest`.
2. Map:
   - `/config` → `/mnt/user/appdata/k-sorter`
   - `/source` → the folder to sort (e.g. `/mnt/user/Media/unsorted`)
   - `/destination` → where sorted folders go (e.g. `/mnt/user/Media/K-Pop`)
   - *(optional)* `/watch` + `/watch_dest` to enable watch-folder mode
   Keep Source and Destination on the **same share** so moves are instant atomic renames.
3. Set `PUID=99`, `PGID=100`, `UMASK=022` (defaults).
4. Open the WebUI — Source and Destination are prefilled with `/source` and
   `/destination` (from the mounts), so just click **Start**.

### Network shares
Mount the SMB/NFS share on the Unraid host and bind-mount it into the container
(the standard Unraid approach). Cross-filesystem moves automatically use the safe
copy-verify-delete path.

## Run locally (dev)

```bash
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements.txt
KSORTER_CONFIG_DIR=./config uvicorn app.main:app --reload --port 8080
# open http://localhost:8080
```

Run the tests:

```bash
pip install pytest
python -m pytest tests/ -q
```

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `KSORTER_CONFIG_DIR` | `/config` | DB, logs, exports |
| `KSORTER_PORT` | `8080` | WebUI port |
| `KSORTER_WATCH_DIR` / `KSORTER_WATCH_DEST` | — | Enable watch-folder mode |
| `KSORTER_AUTO_THRESHOLD` | `90` | Score at/above which matches auto-sort |
| `KSORTER_CONFIRM_THRESHOLD` | `70` | Score at/above which to ask, below which manual |
| `KSORTER_VERIFY_CHECKSUM` | `false` | SHA-256 verify cross-disk moves (slower) |
| `PUID` / `PGID` / `UMASK` | `99` / `100` / `022` | Unraid permissions |

## Logs (under `/config/logs`)

`k-sorter.log` (everything) · `moves.log` (every move) · `needs_review.log` ·
`manual_intervention.log` · `duplicates.csv` · `dry_run_plan.csv`.

---

Data: [kpopnet.json](https://github.com/kpopnet/kpopnet.json) (CC0). Licensed MIT.
See [plan.md](plan.md) for the full design.
