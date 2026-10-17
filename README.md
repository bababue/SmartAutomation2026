# Borehole Atlas

Black Forest Hackathon 2026 – Smart Automation Edition (16–18 Oct 2026, Offenburg).
Challenge by **Herrenknecht AG**: *automated extraction and standardisation of drilling data for a global
geodatabase.*

Borehole Atlas reads borehole logs from PDFs, scans, images and Word files, converts them into one standard
record, georeferences them and stores them in a searchable database with a world map. A vision LLM reads the
pages; our code decides what a value means, where the hole is and whether the record can be trusted.
Uncertain records go to a person.

```bash
docker build -t borehole-atlas . && docker run --rm -p 8000:8000 borehole-atlas   # http://localhost:8000
```

That builds and tests everything that runs without the confidential dataset or an LLM key, and serves the app
with synthetic boreholes (see [What the build checks](#what-the-build-checks)).

## Challenge goal and how we meet it

> Automatically extract borehole profiles from PDFs, scans and images, convert them into a standardised
> format, georeference them and store them in a central database that can be searched via an interactive
> world map. **Success metric:** reliable extraction, standardisation, georeferencing and storage; profiles
> are searchable and shown on a world map.

| Criterion | How | Evidence |
|---|---|---|
| **Extraction** | Every page is classified on a thumbnail; only log, coordinate-table and lab pages are read at full resolution by a vision LLM (Claude Opus 5.5) into a slim schema. Multi-sheet logs are joined in code. | 50 challenge documents, 1,037 pages, 364 holes. Against 63 records a person checked: **97.6 %** of layer boundaries found within 0.1 m, **98.1 %** same lithology, **98.4 %** final depth, all groundwater levels and lab values ([Evaluation](#evaluation)). |
| **Standardisation** | One Pydantic model ([`schema/borehole.py`](schema/borehole.py)): depths in metres, lithology as DIN 4023 classes, stratigraphy code and colour, description in the original language plus English, lab values with qualifiers and ranges. | Export in Herrenknecht's *BoreholeDataFormat* (all seven sheets) and as CSV. |
| **Georeferencing** | The model copies coordinates and CRS text verbatim; code parses every notation, resolves the CRS to an EPSG code, transforms to WGS84 and checks the result against the named place. Printed coordinates are read a second time. | Notations tested with public landmarks in [`tests/test_georef.py`](tests/test_georef.py): UTM, national grids, DMS, decimal minutes, hemisphere letters, packed and unsigned values. Ambiguous or merely inferred systems never release automatically. |
| **Reliability** | Release score = min(extraction, position confidence), computed from evidence, never self-reported by the model. Blocking findings always go to review; human decisions stay bound to the record's content. | Audit of 15 randomly drawn automatic releases: 0 needed a correction (95 % CI 0–20 %). |
| **Storage and search** | SQLite with R-tree (map box) and FTS5 (full text), queryable layer and lab tables, every review decision logged. | 50,000 synthetic holes: map points 0.8 s, list page 0.3 s, "clay between 10 and 20 m" 0.2 s ([Data model](#data-model)). |
| **World map** | React + MapLibre: clusters, filters, 3D columns, profile next to the source page, tunnel route section, similar ground. | Modelled on the LGRB borehole portal (bohrungen.lgrb-bw.de). |
| **Data security** | Pages are processed one by one, locally rendered; the model provider is a setting (Claude, zero-data-retention OpenRouter, or a local model); offline gazetteer. | [Data security](#data-security). |

The challenge owner's priorities from the event (coordinates first, quality over quantity) are in
[docs/challenge-owner-feedback.md](docs/challenge-owner-feedback.md). [FEATURES.md](FEATURES.md) lists every
feature and ideas for next steps; [docs/pipeline.md](docs/pipeline.md) shows the pipeline with diagrams.

## How it works

```mermaid
flowchart LR
    A[PDF / scan / image /<br/>DOCX / XLSX / TXT] --> B[Render pages]
    B --> C{Classify<br/>thumbnail}
    C -->|log, coordinates, lab| D[Extract<br/>vision LLM]
    C -->|other| X[skip]
    D --> K[(Page cache)]
    K --> E[Assemble holes] --> G[Georeference] --> H[Knowledge and<br/>reviewer corrections] --> S{Score}
    S -->|≥ 90, nothing blocking| R[Released]
    S -->|otherwise| Q[Review queue] --> P((Person)) --> R
    R --> DB[(SQLite)] --> UI[Map, search, profile, export]
```

Only the two LLM steps call a model; everything after the cache is code, so a rule change needs only
`pipeline rebuild` (seconds, free).

1. **Ingest:** every page of a PDF (text, scanned, vector-only), JPG/PNG/TIFF or DOCX (through its PDF export)
   is rendered to an image; plain-text logs and Excel workbooks are laid out on A4 pages first. Text layers are
   only a hint, because many are scrambled.
2. **Classify:** a thumbnail per page; large reports contain only a few log pages.
3. **Extract:** full-resolution log pages into a slim schema: values as printed, page regions, unreadable
   items. Continuation sheets get a summary of the previous sheet. IDs, CRS, WGS84, confidence and review
   state are set by code, never by the model.
4. **Assemble:** sheets joined by hole name and sheet number, coordinate tables and lab sheets linked to holes,
   copies of one hole across documents combined; depths compared with the PDF's text layer.
5. **Georeference:** see the table above. A CRS stated anywhere in a document serves logs that name none.
6. **Score and review:** see [Human in the loop](#human-in-the-loop).

## Human in the loop

- Score ≥ `AUTO_APPROVE_THRESHOLD` (0.9) and no blocking finding → released automatically; otherwise the
  record waits in the review queue, weakest first.
- Blocking: layer gaps or overlaps, depth contradictions, an ambiguous coordinate system, a second coordinate
  reading that disagrees, an empty or lost log page, depths in feet.
- An inferred coordinate system, or a notation whose readings lie a few hundred metres apart, stays below the
  threshold until a person confirms it, once per document.
- Reviewers approve, reject or correct a value; every decision is logged with name and time. A decision holds
  while the record's content hash is unchanged. If a re-run changes the content, the record returns to the
  queue with a note.
- Corrections are stored apart from the extraction and applied again on every run, so they survive
  re-extraction. Reviewer knowledge generalises: a confirmed CRS counts for the country, hand-placed holes
  become control points for a fitted local grid, local terms map to DIN 4023 classes.
- A release audit (`pipeline eval audit`) draws automatic releases at random for a person to check and reports
  the error rate with a confidence interval.
- Exports contain released records only, unless `?status=all`.

## What the build checks

The jury's build machine has no dataset and no secrets, so the [`Dockerfile`](Dockerfile) builds and checks
exactly what runs without them:

| Stage | Checks |
|---|---|
| Web | `npm ci`, `oxlint`, unit tests, TypeScript check and production build |
| Python | `pip install`, the full test suite (~290 tests: coordinate parsing in every notation that broke, CRS resolution, sheet assembly, scoring and blocking rules, review persistence, corrections, API, upload queue, export) |
| App | creates a database of synthetic boreholes at public places (`fixtures/demo_data.py`) and serves API and UI on port 8000 |

Not part of the build: extraction itself, which needs an LLM key and the confidential documents. Tests never
call an LLM; they use cached or hand-written page results. The same checks run locally with `make check`.

## Getting started

| Tool | Version | Needed for |
|---|---|---|
| Python | 3.12 or newer | pipeline and API |
| Node.js | 22 or newer | web UI |
| Claude access | API key, or the Claude Code CLI with a subscription | processing documents only |

No database server or GIS stack: storage is one SQLite file and coordinate transformations use pyproj.

```bash
make install                      # .venv, Python packages, web packages
make check                        # tests, lint, type check, build
make demo                         # synthetic database in data/demo.sqlite
DB_PATH=data/demo.sqlite .venv/bin/uvicorn api.main:app --port 8000   # terminal 1
npm --prefix web run dev                                               # terminal 2: http://localhost:5173
```

The header shows "Synthetic demo data" while the API serves the demo database; demo records have no source
pages. After `npm --prefix web run build` the API serves the UI on port 8000 as well.

### Processing the challenge documents

The dataset is confidential and git-ignored. Unpack it to `temp/HKBoreholeChallenge_Data_BFH2026/` (or set
`DATASET_DIR`). Optionally download the offline gazetteer for the place check (GeoNames,
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)):

```bash
.venv/bin/python -m pipeline.gazetteer download
```

Choose how the pipeline reaches a model, in `.env` (git-ignored; `.env.example` has the team setup):

| Backend | Setting |
|---|---|
| Anthropic API | `ANTHROPIC_API_KEY=sk-ant-api03-…` (a subscription token `sk-ant-oat…` is not an API key) |
| Claude subscription through the Claude Code CLI | `LLM_BACKEND=claude-code`; another account via `CLAUDE_CONFIG_DIR` or `CLAUDE_CLI_TOKEN_FILE` |
| OpenRouter or a local model (Ollama) | `LLM_BACKEND=openai`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`, model names of that server |

On OpenRouter, requests go only to zero-data-retention providers (`OPENROUTER_ZDR=1`). Ollama needs
`OLLAMA_CONTEXT_LENGTH=32768` and `MAX_WORKERS=1`.

```bash
.venv/bin/python -m pipeline ingest              # render all pages, list document ids (no model calls)
.venv/bin/python -m pipeline estimate            # page count and cost before a run
.venv/bin/python -m pipeline run --doc <id>      # one document; --max-pages N for a cheap test
.venv/bin/python -m pipeline run --all --yes     # everything (~1,000 pages)
.venv/bin/python -m pipeline rebuild             # reassemble from the cache after a code change (free)
.venv/bin/uvicorn api.main:app --port 8000
```

`--doc` takes a document id (relative path without extension, folders joined by `__`) or a folder prefix, and
can be repeated. Every model answer is cached per page with the model and effort that produced it:
interrupted runs resume, repeated runs are free, and a page is read again only when its image, the prompt
version or the model changes. Runs above about $5 ask for `--yes`.

Web uploads take several files or a ZIP as one batch (up to `MAX_UPLOAD_PAGES` pages): one document group,
processed in one run, so a hole spread over several files is combined. The queue lives in SQLite and survives
a restart; run one API process per database.

`pipeline share --out <dir>` packs a consistent copy of the database and page images for a teammate. The
package is confidential: pass it on locally (AirDrop, USB, `scp`), never through a cloud service.

## Using the app

- **Map:** clusters by location, colour by release state, a ring for a position estimated from a place name.
  From zoom 15, holes become 3D columns with lithology bands at a 1:1 metre scale.
- **Search and filters:** full text over names, projects, places and layer descriptions; release state,
  position quality, lithology, depth to rock, groundwater.
- **Detail:** two-column profile (stratigraphy, DIN 4023 lithology), layers, tests and lab values next to the
  original page; hovering a value outlines where it was read. Similar holes by ground profile.
- **Review queue:** approve with `A`, reject with `R`, `←`/`→` to move, `Esc` to close; the next record opens
  automatically.
- **Tunnel route:** draw an alignment or import a GeoJSON LineString, pick holes in a corridor and get a
  longitudinal section with estimated layers between holes. Spans over 150 m are drawn lighter as less certain.
  The demo data contains a route to try.
- **Statistics:** lithology, depth and release distributions and recurring findings for the current filter.
- **Export:** released records as BoreholeDataFormat workbook or CSV, one row per layer.

## Evaluation

```bash
.venv/bin/python -m pipeline eval snapshot      # records a person approved become the reference
.venv/bin/python -m pipeline eval               # model output (before corrections) against the reference
.venv/bin/python -m pipeline eval audit         # draw automatic releases for a person to check
```

`eval` compares the model's own output, before reviewer corrections, so a fixed record still counts the
model's mistake. Columns: boundaries found within 0.1 m and their precision, primary lithology per layer and
per metre of log, position error and EPSG code, final depth, groundwater levels, lab values. The reference was
approved from the pipeline's own output, which flatters it somewhat; a fresh re-extraction of the reference
pages scores 95.2 % on boundaries.

A prompt change is tried on the reference documents before it becomes the next `PROMPT_VERSION`
(`pipeline experiment init|run|compare`): each arm has its own cache and a fresh database without corrections,
so the scores are the model's alone. The same commands compare models;
[docs/model-comparison.md](docs/model-comparison.md) compares Opus 5.5 with Haiku 5.5 on quality, time and
cost. [docs/prompt-backlog.md](docs/prompt-backlog.md) collects prompt changes for the next version.

## Data model

One SQLite file; the schema version is in `PRAGMA user_version`.

| Table | Content |
|---|---|
| `boreholes` | one row per hole: the canonical record as JSON plus summary columns; a durable `uid` survives renames (aliases in `borehole_aliases`) |
| `layers`, `lab_values`, `findings` | queryable projections, rebuilt with every record |
| `boreholes_rtree`, `boreholes_fts` | map box and full-text search |
| `review_events`, `record_versions` | every decision, and the exact content it was about |
| `corrections`, `knowledge` | human input, applied again on every run |
| `documents`, `pages`, `upload_jobs` | inputs and the web upload queue |

Measured with 50,000 synthetic holes (1 GB): map points 0.8 s / 1.5 MB gzipped, a list page 0.3 s, "clay between
10 and 20 m" 0.2 s, "UCS ≥ 80 MPa" 0.1 s, a review-queue page 6 ms; the CSV export streams from the first row.

## Repository layout

| Path | Content |
|---|---|
| `schema/` | canonical Pydantic model, generated JSON Schema, mapping to the BoreholeDataFormat |
| `pipeline/` | ingest, classify, extract, assemble, georeference, score, store; CLI `python -m pipeline` |
| `api/` | FastAPI: search, map data, detail, source pages, review, corrections, upload, statistics, export |
| `web/` | React 19, Vite, TypeScript, MapLibre |
| `tests/` | pytest suite; public landmarks and invented values only |
| `fixtures/` | synthetic demo boreholes |
| `docs/` | pipeline diagrams, challenge-owner feedback, model comparison, prompt backlog |
| `temp/`, `data/` | dataset, pages, cache and database: confidential and git-ignored |

## Configuration

Environment variables, from the shell or `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `LLM_BACKEND` | `api` | `api`, `claude-code` or `openai` |
| `ANTHROPIC_API_KEY` | – | key for `api` |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` | `http://localhost:11434/v1` / – | server and key for `openai` |
| `OPENROUTER_ZDR` | `1` | OpenRouter: zero-data-retention providers only |
| `CLAUDE_CLI`, `CLAUDE_CONFIG_DIR`, `CLAUDE_CLI_TOKEN_FILE` | `claude`, CLI default, – | CLI backend and its account |
| `CLASSIFY_MODEL` / `CLASSIFY_EFFORT` | `claude-opus-5-5` / `low` | page classification |
| `EXTRACT_MODEL` / `EXTRACT_EFFORT` | `claude-opus-5-5` / `medium` | log extraction |
| `MAX_WORKERS` | `6` (API), `3` (CLI) | parallel model calls |
| `AUTO_APPROVE_THRESHOLD` | `0.9` | release score for automatic release |
| `MAX_UPLOAD_PAGES` | `100` | pages per upload batch |
| `MAX_UPLOAD_JOBS` / `MAX_UPLOAD_WORKERS` | `8` / `1` | web upload queue |
| `DATASET_DIR` | `temp/HKBoreholeChallenge_Data_BFH2026/BoreholeData` | input documents |
| `DATA_DIR` / `DB_PATH` | `data` / `data/boreholes.sqlite` | pages, cache, database |
| `GAZETTEER_PATH` | `data/gazetteer.sqlite` | offline GeoNames index |
| `EXTRACT_LONG_EDGE` / `THUMB_LONG_EDGE` | `2000` / `900` | rendering size in pixels |

Deleting the database and running `rebuild` restores every record from the cache, but **review decisions and
corrections are lost**.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `401 invalid x-api-key` | The key is a subscription token, not an API key. Use a Console key or `LLM_BACKEND=claude-code`. |
| `No Anthropic credentials` | No `.env` in the project root, or the variable is missing. |
| Map stays empty, "Worker failed to load" | Old UI build: `npm --prefix web run build`, or restart the dev server. |
| "No source page stored" on every record | The API serves the demo database; restart it without `DB_PATH`. |
| "kept previous results for incomplete documents" | Some pages failed (rate limit, timeout); run again, finished pages come from the cache. |
| Records unchanged after a code change | `pipeline rebuild`, then reload the UI. |

## Data security

The brief asks for data security at every step:

- Documents are rendered and split into pages locally; only single page images go to the model, and only to a
  provider the team approved for this data, or to a local model. The provider is one setting.
- Place names are looked up in an offline gazetteer before anything else.
- Source files, pages, cache and database stay local, in git-ignored folders, and are deleted after the event.
- Not in the prototype: user accounts. Run it on a trusted network.

The challenge dataset contains confidential data from real projects. It is used only for this challenge, is not
shared outside the event and is deleted afterwards. Nothing from it is committed: tests, fixtures and docs use
public landmarks and invented values.
