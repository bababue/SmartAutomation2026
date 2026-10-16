# Borehole Extraction & Global Geodatabase

Black Forest Hackathon 2026 – Smart Automation Edition (16–18 Oct 2026, Offenburg)
Challenge by **Herrenknecht AG**: *Automated extraction and standardisation of drilling data for a global geodatabase.*

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

## Repository layout

| Path | Content |
|---|---|
| `schema/` | Canonical Pydantic data model (`borehole.py`), generated JSON Schema, field mapping to Herrenknecht's format |
| `pipeline/` | Ingest → classify → extract (Claude) → georeference → assemble → SQLite |
| `api/` | FastAPI: search, map data, borehole detail, source page images, CSV export |
| `web/` | React + Vite + MapLibre UI: world map, result list with core strips, two-column profile next to the source page |
| `tests/` | Georeferencing tests built from public landmarks in the notations found in the data |
| `fixtures/` | Synthetic demo boreholes (no challenge data) for UI work without an API key |
| `temp/`, `data/` | Challenge dataset, rendered pages, cache and database – **confidential, git-ignored** |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm --prefix web install
```

Put the Anthropic API key into `.env` (git-ignored):

```bash
echo "ANTHROPIC_API_KEY=..." > .env
```

## Run

```bash
.venv/bin/python -m pipeline estimate            # render all pages, show page count and cost estimate
.venv/bin/python -m pipeline run --doc AUS01     # process one document end to end
.venv/bin/python -m pipeline run --all --yes     # process everything
.venv/bin/python -m pipeline rebuild             # re-run assembly/georeferencing from cached LLM output (free)
.venv/bin/uvicorn api.main:app --port 8000       # API (serves web/dist if built)
npm --prefix web run dev                          # UI on http://localhost:5173
```

Without a key, try the UI with synthetic data:

```bash
DB_PATH=data/demo.sqlite .venv/bin/python -m fixtures.demo_data
DB_PATH=data/demo.sqlite .venv/bin/uvicorn api.main:app --port 8000
```

Tests: `.venv/bin/python -m pytest`

## Data security

The brief asks for data security across every step. Design rules:

- Only send documents to processing services the team has explicitly approved for this data or run models locally; the provider is a configuration switch.
- Geocoding: prefer an offline gazetteer; place names can be confidential too.
- Keep source files, rendered pages, logs and the database local; delete all of it (incl. derived data) after the event.
- UI and database only for authenticated users.

## Data confidentiality

The challenge dataset contains confidential data from real projects. It may only be used for this challenge, must not be shared with anyone outside the event, and must be deleted after the event. It lives in `temp/`, which is git-ignored. Do not commit extracted values, screenshots or excerpts from it.
