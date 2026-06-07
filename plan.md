# K-Sorter — Project Plan

A self-hosted web app that sorts a folder of K-pop videos into correctly-named
group (and member) folders. Runs as a **single Docker container** on your Unraid
server. Sort once, sort TWICE — but only move each file *once*. 🎵

> **Guiding principle: accuracy over automation.** Your #1 rule is
> *"DO NOT MAKE MISTAKES."* Every decision below is biased toward *never* moving a
> file into the wrong place. The app runs **mostly hands-off** — it auto-sorts
> what it's confident about and only taps you on the shoulder for the genuinely
> uncertain ones (a quick "Yes or Yes?"). Every move is logged and reversible.

---

## 1. What the app does (in plain terms)

1. You point it at a **source folder** and a **destination folder**. Both can be
   local paths or network locations.
2. It **recursively scans** the source (including nested subfolders) for videos.
3. For each video it reads the **filename** and works out the **group**, and —
   for solo fancams/focus videos — the **member**.
4. **Confident matches are sorted automatically.** Only the ones it isn't sure
   about are surfaced to you, with a precise one-tap question.
5. Everything is logged; anything uncertain or unmatched is parked in a review
   queue and written to dedicated log/CSV files. Every move can be **undone**.

---

## 2. Core principle: how we avoid mistakes

| Safeguard | What it does |
|---|---|
| **Automatic where confident, ask where not** | High-confidence, unambiguous matches sort automatically. Uncertain ones go to a **"Needs confirmation"** queue; unmatched ones go to a **"Manual"** queue. You're only prompted when it matters. |
| **Precise, minimal prompts** | When it must ask, it shows its single best guess as a quick **Yes / No / pick-from-2** — not an open-ended puzzle. One decision, then it remembers your answer (see Learning, §10). |
| **Filenames are never changed** | The app **never renames or edits your files.** Filename "cleaning" happens only **in memory on the backend** purely to read the names — your files on disk are untouched. Dates stay put (they matter for natural sorting). |
| **Non-destructive moves** | Never overwrites. On a name collision it skips and flags — never clobbers. |
| **Fast + verified moves** | Same-filesystem = instant atomic rename. Cross-filesystem = copy → verify byte-count/size → then remove source (optional checksum toggle). |
| **Undo journal** | Every batch records `source → destination`. One click reverses the last batch. |
| **Layered logging** | UI summary + detailed backend logs, split by purpose (see §9). |

---

## 3. Tech stack — 100% free & self-hosted

No hosting fees, no paid APIs, no cloud. Everything runs in your one container on
hardware you already own.

| Layer | Choice | Why |
|---|---|---|
| **Backend** | **Python 3.12 + FastAPI** | Async, fast, matches your existing project style. |
| **Frontend** | **HTMX + Alpine.js + hand-written CSS** | No build step, tiny, server-rendered, calm. |
| **Database** | **SQLite** (with FTS5 full-text index) | One lightweight file in `appdata`; fast name lookups. |
| **Fuzzy matching** | **RapidFuzz** | Fast, tunable fuzzy string matching. |
| **Seed data** | **kpopnet.json** (CC0 — public domain) | English + Korean names, aliases, full rosters, and a `current` flag per member (= active/former). Free. |
| **Live lookups (unknowns)** | **Free public sources** (e.g. Wikipedia API + public K-pop DB pages) | No API key, no fees. |
| **Background jobs** | FastAPI background tasks / a lightweight in-process queue | Powers safe-mode for big libraries — no external broker needed. |
| **Server / packaging** | **Uvicorn** + **Docker** (`python:3.12-slim`, multi-stage) | Small single image, Unraid-friendly. |

---

## 4. The K-pop database

### Source & schema
Seeded from **kpopnet.json**. Per group: `name`, `name_original` (Korean),
`name_alias`, agency, debut/disband dates, and `members[]` each with a
`current` boolean and `roles`. Per idol: stage name (EN + KO), real name,
aliases, birth date.

```
groups(id, name, name_ko, agency, debut_date, disband_date, is_active, parent_id, source, confirmed)
members(id, stage_name, stage_name_ko, real_name, birth_date)
aliases(entity_type, entity_id, alias, alias_ko)        -- nicknames & romanizations
group_members(group_id, member_id, is_current, roles)   -- is_current = active/former
corrections(pattern, entity_type, entity_id, created_at) -- learned from your fixes (§10)
```

- **Former members are kept**, flagged `is_current = 0`. A fancam of a former
  member still sorts into the correct group. ✅
- `aliases` (Korean, English, nicknames, common misspellings) all point back to
  one entity → robust matching. FTS5 index for instant lookups.

### Seeding — only when you choose (for speed)
The dataset is **not** re-seeded on every run. It refreshes only:
1. **On container restart**, or
2. When you click **"Update Database"** in the UI (the manual refresh — *Signal*
   it and it goes get the latest 📡).

Unknown groups found during a sort trigger a one-time **free live lookup**,
shown for your confirmation, then cached permanently — so each unknown is
researched only once.

---

## 5. The sorting engine

### Step 1 — Read the filename (in memory only; files untouched)
Internally ignore noise — extension, resolution/codec tags (`4K`, `1080p`,
`x265`), uploader brackets, and filler words (EN + KO: `fancam`, `직캠`, `focus`,
`포커스`, `세로직캠`, `교차편집`, `stage mix`, `MV`). **Dates are NOT stripped** and the
file is **never renamed** — this parsing is purely to recognize names.

### Step 2 — Identify the group
Exact match against names + aliases (EN/KO/nickname) → fall back to **RapidFuzz**
with a high threshold → produce a confidence score → check **learned corrections**
first (your past fixes win).

### Step 3 — Identify the member (solo content only)
Detect a solo video via fancam/focus keywords, then match remaining tokens
against that group's members (**including former members**). Full-group videos
get no member folder.

### Step 4 — Confidence routing
```
Confident & unambiguous  → auto-sorted, recorded in moves log
Uncertain / ambiguous    → "Needs confirmation" queue (precise prompt) + log
None (no match)          → "Manual" queue + manual-intervention log
```

### Step 5 — Resulting folder structure
```
<destination>/
├── TWICE/
│   ├── Group/                 ← full-group videos: MVs, group stages, dance practice
│   ├── Nayeon/                ← solo fancams / focus videos
│   ├── Momo/
│   └── Mina/
├── MISAMO/                    ← sub-unit: its own top-level folder (parent link kept in DB)
└── _Special Stages/          ← collab / multi-group videos (also replicated per group, §10)
```

- Parent folder = **group name** (English by default; Korean optional — §10).
- **Videos with no specific member go into the group's `Group/` subfolder** (per
  your note) — so every group has a tidy `Group/` plus member folders only where
  solo content exists. No empty folders.
- Sub-units (e.g. **MISAMO**) get their own top-level folder, linked to the
  parent in the DB.

### Step 6 — Move safely, quickly, verified
- **Same filesystem** → atomic **rename** (instant, no copy, no corruption risk).
- **Cross filesystem** (e.g. local → network share) → **copy → verify
  byte-count/size → remove source**. Optional **checksum** toggle for extra
  paranoia (off by default — it roughly doubles I/O on large files).
- Collisions skipped & flagged; **every move journaled** for undo; recursive,
  with **path-traversal sanitization** so nothing escapes your destination.

---

## 6. Handling network locations & access

- **Unraid-native approach:** mount the SMB/NFS share on the host, bind-mount it
  into the container. Reliable, keeps credentials out of the app.
- The folder picker **validates** any chosen path and gives a friendly UI warning
  (full detail in the log) when a path is:
  - **unreachable** (network share down),
  - **read-only / not writable**, or
  - **blocked by permissions** (PUID/PGID mismatch, ACLs).
- Cross-filesystem moves automatically use the safe copy-verify-delete path.

---

## 7. User workflow & UI

### Flow (hands-off by default)
1. **Pick source + destination** (browse/type; validated live).
2. **Scan** runs in the background (safe-mode, §10) with a calm progress bar.
3. **Auto-sort** confident matches; the UI fills in as it goes.
4. **Review only the unsure ones** — a focused section with precise prompts
   ("This looks like TWICE — Momo. Yes or Yes? ✓ / ✗ / pick").
5. **Summary** — what moved, what's queued, suspected duplicates, with **Undo**.

### Design language — calming, with your palette
A soft, earthy, frosted-glass aesthetic built on your colours:

| Hex | Role |
|---|---|
| `#F2E3BC` (cream) | Background / canvas, light text on dark |
| `#96BBBB` (muted teal) | Primary accents, confident-match highlights |
| `#618985` (sage) | Buttons, active states |
| `#414535` (dark olive) | Text, dark theme base, headers |
| `#C19875` (clay/tan) | Warm secondary accents, "needs attention" chips |

- Frosted-glass panels, generous spacing, rounded corners, gentle shadows.
- **Subtle animations:** soft fade/slide as the list loads, a gentle "settle"
  when a file lands in its folder, a calm progress ripple during moves.
- Respects `prefers-reduced-motion`; light/dark themes; legible and uncluttered.
- A light sprinkle of TWICE-flavoured copy (success: *"Feel Special."*) — tasteful,
  never cringe.

---

## 8. Docker / Unraid deployment

- **One single container**, hosted on your Unraid server. Nothing else to run.
- **Multi-stage Dockerfile** on `python:3.12-slim`; runs Uvicorn.
- **Unraid conventions baked in:** `PUID=99`, `PGID=100`, `UMASK=022`; volumes for
  `/config` (SQLite DB, logs, CSVs, settings → `appdata`) and your media mount(s);
  configurable WebUI port.
- **Watch-folder** path set via an **environment variable** (§10).
- **Unraid Community Applications template (XML)** so it installs via a friendly
  form — no command line.
- GitHub Actions to build & publish the image to GHCR (free for public repos),
  CI watched to green.

---

## 9. Logging (per CLAUDE.md) — separate logs by purpose

Every failure gives a **friendly UI summary** + **maximally detailed backend log**.
Logs live in `CONFIG_DIR/logs/`:

| File | Contents |
|---|---|
| `k-sorter.log` | Main app log: every decision, score, path, error/traceback. |
| `moves.log` | **Every move** performed (source → destination, method, verified). |
| `needs_review.log` | Uncertain/ambiguous videos awaiting your confirmation. |
| `manual_intervention.log` | Confidence **None** videos that need you to decide. |
| `duplicates.csv` | Suspected duplicates for review (§10). |

---

## 10. Confirmed features to build

1. **Undo / move history** — journal of every batch; one-click reverse. ✅
2. **Learning from corrections** — when you fix a match, store the
   `alias/pattern → group/member` mapping so it's right automatically next time. ✅
3. **Duplicate detection** — never auto-deletes. Flags suspected duplicates using
   **file size + metadata** (and optional hash), surfaces them in a clear,
   outlined UI section **and** writes `duplicates.csv` for review. ✅
4. **Watch-folder mode** — set a folder via **env variable**; new files dropped
   there are sorted automatically (confident only; the rest still queued). ✅
5. **Multi-group / collab handling** — videos with multiple groups go to a
   top-level **`_Special Stages/`** folder **and are replicated into each relevant
   group**. To avoid wasting space, replication uses **hardlinks** when on the
   same filesystem, falling back to a copy otherwise. ✅
6. **Sub-units** — own top-level folder, parent link kept in DB. ✅
7. **Dry-run export** — save the proposed plan as CSV/text to review before
   anything moves. ✅
8. **Configurable naming** — **English by default**, Korean folder names optional;
   template **`Group/Member` by default**, with `Group - Member` or a **custom**
   template option. ✅
9. **Safe-mode for huge libraries** — *always on:* streaming scan, background job
   with live progress, paginated UI. Tens of thousands of files stay responsive,
   no freezes or timeouts. ✅

---

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Filename has no usable name | → "Manual" queue, never guessed. |
| Two groups share a member name | Disambiguate by group token; else → confirmation queue. |
| Network share drops mid-move | Verify-then-delete: source never lost; failure logged & re-queued. |
| Romanization variants (Mina/미나) | Alias table + fuzzy match + learned corrections. |
| Huge library performance | Safe-mode streaming + background jobs + pagination. |
| Malicious/odd path in filename | Path-traversal sanitization on every write. |

---

## 12. Build milestones (verify at each step)

1. **Repo + skeleton** → FastAPI serves a page in Docker locally.
2. **DB layer + seed import** → groups/members/aliases queryable; former members `is_current=0`; refresh only on restart / "Update Database".
3. **Recursive scanner + parser + matcher** → unit tests over real TWICE-style fancam filenames produce correct group/member/confidence.
4. **Confidence routing + auto-sort + queues** → confident auto-sorts; uncertain/None routed correctly; dry-run export works.
5. **Safe move engine + undo + collision/path guards** → same-fs & cross-fs moves verified; undo restores; tested on throwaway files.
6. **Duplicate detection + CSV/UI section** → suspected dupes flagged, never deleted.
7. **Collab/Special Stages + sub-units + configurable naming** → replication via hardlink/copy; templates honored.
8. **Live enrichment + confirmation + learning** → unknown group → free lookup → confirm → cached → learned next time.
9. **Watch-folder mode (env var)** → drops auto-sort confident files.
10. **Calming UI + palette + animations + reduced-motion.**
11. **Dockerfile + Unraid template + CI to green.**

---

## 13. Decisions

**Locked in:**

1. **Tech stack** — ✅ FastAPI + HTMX + SQLite, **fully free / self-hosted**.
2. **Folder naming** — ✅ **English default**, Korean optional; template
   `Group/Member` default (or `Group - Member` / custom).
3. **Apply mode** — ✅ **Mostly automated**: auto-sort confident matches, only
   prompt for uncertain ones (precise questions). *(Updated from "always preview"
   per your note — Undo + logs remain the safety net. A dry-run export is still
   available if you ever want a look-before-you-leap pass.)*
4. **Repo** — ✅ **Public**, created **after you approve this plan**.
5. **Folder layout** — ✅ group-wide videos → `Group/` subfolder; solo →
   member subfolders; collabs → `_Special Stages/` + replicated per group.
6. **Seeding** — ✅ only on restart or manual "Update Database".

**Defaults assumed (say the word to change):**

7. **Live-lookup source** — free public sources (Wikipedia API + public K-pop DB),
   no API key.
8. **Checksum on cross-fs moves** — off by default (size/byte-count verify on),
   toggle available.

---

## Sources
- [FastAPI + HTMX production guide](https://medium.com/@sylvesterranjithfrancis/complete-guide-building-production-ready-web-apps-with-fastapi-and-htmx-from-setup-to-deployment-3010b1c8ff5c)
- [Unraid Docs — managing & customizing containers](https://docs.unraid.net/unraid-os/using-unraid-to/run-docker-containers/managing-and-customizing-containers/)
- [Unraid forums — PUID/PGID/UMASK](https://forums.unraid.net/topic/118751-puid-pgid-and-umask/)
- [kpopnet.json — open K-pop dataset (CC0)](https://github.com/kpopnet/kpopnet.json)
- [Docker + SMB/CIFS mounting best practices](https://forums.docker.com/t/optimal-method-for-mounting-cifs-nas-in-docker-bind-mounts-vs-volumes/146523)
