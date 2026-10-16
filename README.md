# Borehole Extraction & Global Geodatabase

Black Forest Hackathon 2026 – Smart Automation Edition (16–18 Oct 2026, Offenburg)
Challenge by **Herrenknecht AG**: *Automated extraction and standardisation of drilling data for a global geodatabase.*

Jump to [Getting started](#getting-started) to run it yourself.

## Goal

Automatically extract borehole profiles from PDFs, scans and images, convert them into a standardised format, georeference them and store them in a central database that can be searched via an interactive world map.

Success metric: reliable extraction, standardisation, georeferencing and storage; profiles are searchable and shown on a world map.

## What the challenge brief emphasises

- Per log: **elevation**, **depth of each layer**, **age & colour code** (formation abbreviations and their colours), **rock type, pattern & colour**.
- Inputs are unstructured, multilingual, use local geology terms, vary by company and decade, range from scans to native digital, and carry meaning in colour and patterns.
- Pipeline steps: OCR → metadata (location, date, elevation) → extraction (depth, lithology, stratigraphy) → map & profile → search.
- **Data security across every step.**
- Reference UI: the LGRB Baden-Württemberg borehole portal (bohrungen.lgrb-bw.de).

## Approach

```
input files ──► page classification ──► extraction (vision LLM, structured output)
                                              │
                                              ▼
              world map + search ◄── database ◄── normalisation & georeferencing ◄── validation
```

1. **Ingest** – PDF (text, scanned, vector-only), JPG/PNG, DOCX. Every page is rendered to an image; text layers are used only as a hint because many are scrambled or bad OCR.
2. **Page classification** – find log pages (incl. continuation sheets), coordinate tables and lab result sheets; large reports contain only a few.
3. **Extraction** – a vision LLM fills a slim extraction schema (observations, original strings, page references). IDs, CRS resolution, WGS84 coordinates, confidence and review state are set by our own code, never by the model.
4. **Normalisation** – units to metres/SI, lithology to DIN 4023 keys, descriptions translated to English (original kept).
5. **Georeferencing** – parse coordinates as written, resolve the CRS (EPSG), transform to WGS84, check plausibility against the geocoded project location; fall back to geocoding when no coordinates exist.
6. **Validation** – gaps/overlaps between layers, depth consistency, position sanity; findings are stored as issues, not thrown away.
7. **Storage & UI** – database with spatial index and full-text search; map with clustering and filters; borehole detail view with a two-column profile (stratigraphy code + colour, DIN 4023 lithology pattern) next to the original page (inspired by the LGRB BW borehole portal).

## Human in the loop

Every record gets a **release score (0–100)**: the weaker of extraction confidence and position confidence.
Extraction confidence is computed, not self-reported by the model: plausibility of the layer sequence,
items the model reported as unreadable, and – for PDFs with a text layer – the share of extracted layer
depths that literally appear in that text.

- Score ≥ `AUTO_APPROVE_THRESHOLD` (default 90) and no blocking finding → **released automatically**.
- Otherwise → **review queue** in the UI. Blocking findings always need a person: layer gaps/overlaps,
  depth mismatch, ambiguous coordinate system, empty log.
- Positions whose coordinate system is only inferred (not printed), or whose notation allows readings a
  few hundred metres apart (decimal minutes vs. seconds), stay below the threshold until a person confirms them.
- Reviewers approve, reject or reopen with name and optional comment; every decision is logged.
- Re-runs keep human decisions as long as the reviewed content is unchanged; if the data changes, the
  record returns to the queue with a note.
- The CSV export contains released records only by default.

## Large documents

Documents are never sent as a whole. Every page is rendered locally and processed on its own, so file
size (some inputs are ~80 MB / 150 pages) does not matter for API limits:

1. Cheap classification of every page on a small thumbnail.
2. Full-resolution extraction only for log and coordinate-table pages (typically a third of a report).
3. Per-page cache: interrupted runs resume, re-assembly (`pipeline rebuild`) costs nothing.
4. Context across pages is restored in code: multi-sheet logs are merged by hole name, coordinate tables
   are linked to logs, lab sheets can be linked by hole name and depth.
5. Continuation sheets (same hole name as the previous log page) are extracted in page order, each with a
   short summary of the previous sheet (last layers, hole name); independent logs run in parallel.
6. A coordinate system stated anywhere in the document (other logs, coordinate tables, notes, legends, report
   text) is used for logs that do not name one; holes far from the others of the same document are flagged.

Every layer and the coordinates carry an approximate region on the source page (read by the vision model);
the UI outlines it on the original page when a reviewer hovers the value.

## Repository layout

| Path | Content |
|---|---|
| `schema/` | Canonical Pydantic data model (`borehole.py`), generated JSON Schema, field mapping to Herrenknecht's format |
| `pipeline/` | Ingest → classify → extract (Claude) → georeference → assemble → SQLite |
| `api/` | FastAPI: search, map data, borehole detail, source page images, CSV export |
| `web/` | React + Vite + MapLibre UI: world map, result list with core strips, two-column profile next to the source page |
| `tests/` | Georeferencing (public landmarks in the notations found in the data), scoring, review workflow, API |
| `fixtures/` | Synthetic demo boreholes (no challenge data) for UI work without an API key |
| `temp/`, `data/` | Challenge dataset, rendered pages, cache and database – **confidential, git-ignored** |

## Getting started

### 1. Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12 or newer (tested with 3.14) | pipeline and API |
| Node.js | 22 or newer (tested with 26) | web UI (Vite 8, React 19, MapLibre 6) |
| Git | any | work on the branch `marvin` |
| Claude access | Anthropic API key **or** Claude Code CLI with a subscription | only needed to process documents |

No Docker, database server or GIS stack needed: storage is a single SQLite file, coordinate
transformations use pyproj. Plan for ~0.5 GB of disk space for rendered pages.

### 2. Install

```bash
git clone <repository-url> SmartAutomation2026
cd SmartAutomation2026
git checkout marvin
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm --prefix web install
```

### 3. Try the UI without data or credentials

Synthetic boreholes at public places (no challenge data) in a separate database:

```bash
DB_PATH=data/demo.sqlite .venv/bin/python -m fixtures.demo_data
DB_PATH=data/demo.sqlite .venv/bin/uvicorn api.main:app --port 8000   # terminal 1
npm --prefix web run dev                                               # terminal 2
```

Open http://localhost:5173. The header shows "Synthetic demo data" while the API serves this database;
demo records have no source pages.

### 4. Add the challenge data

The dataset comes from Herrenknecht (password-protected download, confidential). Unpack it so that the
documents end up here – the folder is git-ignored:

```
temp/HKBoreholeChallenge_Data_BFH2026/
├── BoreholeData/        ← PDFs, JPG/PNG, DOCX (the pipeline reads this folder)
└── Standards_Formats/   ← target format (BoreholeDataFormat.xlsx), DIN 4023
```

A different location works too: set `DATASET_DIR` (see Configuration).

### 5. Choose how the pipeline talks to Claude

**Option A – Anthropic API key (recommended).** Create a key in the Claude Console (platform.claude.com →
API Keys; it starts with `sk-ant-api03-`) and put it into `.env`, which is git-ignored:

```bash
printf 'ANTHROPIC_API_KEY=%s\n' "sk-ant-api03-..." > .env
```

Tokens starting with `sk-ant-oat` (from `claude setup-token` / a Claude subscription) are **not** API keys
and are rejected with `401 invalid x-api-key`.

**Option B – Claude subscription via Claude Code (for testing).** If the `claude` CLI is installed and
logged in, the pipeline can call it in print mode instead of the API. Slower, subject to the subscription's
usage limits:

```bash
echo "LLM_BACKEND=claude-code" >> .env
```

To use another subscription than the CLI's default login, add `CLAUDE_CONFIG_DIR=~/.claude-other`
(a profile directory logged in with that account) or `CLAUDE_CLI_TOKEN_FILE=~/path/to/token`.

### 6. Process documents

```bash
.venv/bin/python -m pipeline ingest                    # render all pages, list document ids (no API calls)
.venv/bin/python -m pipeline estimate                  # page count and rough API cost
.venv/bin/python -m pipeline run --doc profile01       # smoke test: one page
.venv/bin/python -m pipeline run --doc MEA03-jpg --doc AUS01 --max-pages 6
.venv/bin/python -m pipeline run --all --yes           # everything (~1,000 pages)
```

- `--doc` takes a document id (relative path without extension, folders joined by `__`, e.g.
  `SEA03-png__Report01`) or a folder prefix (`MEA03-jpg` = all images in that folder). Repeatable.
- `--max-pages N` treats the first N pages of each document as the whole document (cheap demo runs).
- Runs above ~$5 estimated cost ask for `--yes`.
- Every LLM answer is cached per page in `data/cache/`. Interrupted runs resume; repeated runs are free.
- `rebuild` re-runs assembly, georeferencing and scoring from the cache only – use it after code changes
  (pass the same `--doc` / `--max-pages` as the run):

```bash
.venv/bin/python -m pipeline rebuild --doc MEA03-jpg --doc AUS01 --max-pages 6
```

At the end of a run the pipeline prints how many boreholes were stored, how many were released
automatically and how many wait for review.

### 7. Start the app

Development (hot reload), two terminals:

```bash
.venv/bin/uvicorn api.main:app --port 8000
npm --prefix web run dev                     # http://localhost:5173 (proxies /api to port 8000)
```

Single server (e.g. for the demo): build the UI once, the API then serves it as well:

```bash
npm --prefix web run build
.venv/bin/uvicorn api.main:app --port 8000   # http://localhost:8000
```

Restart the API after pipeline code changes; the UI picks up new data on reload.

### 8. Use the app

- **Map**: clusters by location; colour = release state (green released, ochre waiting for review, red rejected),
  ring = approximate position (estimated from the place name). Hover for name and score; click to open.
- **Search and filters**: full text over names, projects, places and layer descriptions; release state,
  position quality, lithology.
- **Detail**: two-column profile (stratigraphy, DIN 4023 lithology), layers, tests, record data and the
  original page; hover a layer or the coordinates to outline where they were read.
- **Review queue**: everything below the threshold or with blocking findings, weakest first. Enter your name
  once, then approve or reject; from the queue the next waiting record opens automatically. Keys: `A` approve,
  `R` reject, `←`/`→` previous/next in the queue, `Esc` close.
- **Section**: "+ Section" on boreholes shows them side by side along a line, at ground level where known.
- **Export**: "Export released (CSV)" – one row per layer, released records only.

### 9. Tests

```bash
.venv/bin/python -m pytest -q
npm --prefix web run build      # includes the TypeScript check
```

## Evaluation

Measures extraction quality against records a human has checked.

```bash
.venv/bin/python -m pipeline eval snapshot   # approved records (incl. corrected ones) become the reference
.venv/bin/python -m pipeline eval            # compare the model output (before corrections) with the reference
.venv/bin/python -m pipeline eval --corrected   # compare the final values, reviewer corrections included
```

`snapshot` writes `data/eval/reference.json` (git-ignored, confidential like all of `data/`). It adds to and
updates the existing reference, so a record approved earlier stays in it even if a later run changes it and
sends it back to review. Per record it keeps the layers (top, bottom, primary lithology), the position and the
EPSG code.

`eval` prints, per document and overall:

| Column | Meaning |
|---|---|
| `holes` / `miss` / `extra` | reference records, of those not found by ID, records of the same documents not in the reference (often just not reviewed yet) |
| `bounds` | share of reference layer boundaries with a current boundary within 0.1 m |
| `lith` | same primary lithology on layers whose top and bottom both match |
| `med m` / `max m` | median and maximum position error in metres |
| `no pos` / `crs≠` | records that lost their position / have a different EPSG code |
| `corr` | compared records with reviewer corrections |

Every run keeps the model output before reviewer corrections (layers and position, column `boreholes.extracted`).
`eval` compares that by default, so a record a reviewer fixed still counts the model's mistake. With
`--corrected` it compares the final values instead; corrected records then match the reference by construction.
Records stored before this column existed are compared with their final values until the next `rebuild`.
Typical use: snapshot after a review session, change prompts or code, `rebuild`/`run`, then `eval`.

## Configuration

Environment variables, read from the shell or from `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | API key for `LLM_BACKEND=api` |
| `LLM_BACKEND` | `api` | `api` (Anthropic API) or `claude-code` (local `claude -p`) |
| `CLAUDE_CLI` | `claude` | path to the Claude Code CLI |
| `CLAUDE_CONFIG_DIR` | CLI default | profile directory for another subscription (CLI backend) |
| `CLAUDE_CLI_TOKEN_FILE` | – | token file for another subscription (CLI backend) |
| `CLASSIFY_MODEL` / `CLASSIFY_EFFORT` | `claude-opus-5-5` / `low` | page classification |
| `EXTRACT_MODEL` / `EXTRACT_EFFORT` | `claude-opus-5-5` / `medium` | log extraction |
| `MAX_WORKERS` | `6` (API), `3` (CLI) | parallel LLM calls |
| `AUTO_APPROVE_THRESHOLD` | `0.9` | release score for automatic release |
| `DATASET_DIR` | `temp/HKBoreholeChallenge_Data_BFH2026/BoreholeData` | input documents |
| `DATA_DIR` | `data` | rendered pages, cache, database |
| `DB_PATH` | `data/boreholes.sqlite` | database (`data/demo.sqlite` for synthetic data) |
| `EXTRACT_LONG_EDGE` / `THUMB_LONG_EDGE` | `2000` / `900` | rendering size in pixels |

Changing models, effort or prompts (`PROMPT_VERSION` in `pipeline/config.py`) invalidates the cache for the
affected stage – the next run calls the LLM again.

Generated data, all git-ignored and confidential: `data/pages/` (rendered pages), `data/cache/` (LLM answers),
`data/boreholes.sqlite` (database incl. review decisions). Deleting the database and running `rebuild`
restores the records from the cache, but **review decisions are lost**.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `401 invalid x-api-key` | The key is not an API key (e.g. `sk-ant-oat...` from a subscription). Use a Console key or `LLM_BACKEND=claude-code`. |
| `No Anthropic credentials` | No `.env` in the project root, or the variable is missing. Check with `ls -la .env`. |
| Map stays empty, console says "Worker failed to load" | Old UI build. Run `npm --prefix web run build` (port 8000) or restart `npm --prefix web run dev`. |
| "No source page stored for this record" for every record | The API serves the demo database. Restart it without `DB_PATH=data/demo.sqlite`. |
| `address already in use` on 8000 / 5173 | Another server runs there: `lsof -nP -iTCP:8000 -sTCP:LISTEN`, stop it or use another port. |
| A document is skipped with "kept previous results for incomplete documents" | Some pages failed (rate limit, timeout). Run the same command again; finished pages come from the cache. |
| Records do not change after a code change | Run `pipeline rebuild` with the same `--doc`/`--max-pages`, then reload the UI. |

## Data security

The brief asks for data security across every step. Design rules:

- Only send documents to processing services the team has explicitly approved for this data or run models locally; the provider is a configuration switch.
- Geocoding: prefer an offline gazetteer; place names can be confidential too.
- Keep source files, rendered pages, logs and the database local; delete all of it (incl. derived data) after the event.
- UI and database only for authenticated users.

## Data confidentiality

The challenge dataset contains confidential data from real projects. It may only be used for this challenge, must not be shared with anyone outside the event, and must be deleted after the event. It lives in `temp/`, which is git-ignored. Do not commit extracted values, screenshots or excerpts from it.
