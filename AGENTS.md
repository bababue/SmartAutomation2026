# AGENTS.md — Borehole Atlas

Instructions for coding agents (Claude Code, Codex, …). This file is the **canonical
source**; `CLAUDE.md` points here. Setup, commands in detail and configuration are in
[README.md](README.md). This file covers the rules for working in the repo.

## Project

Black Forest Hackathon 2026, Herrenknecht challenge. The task is to extract borehole
logs from PDFs, scans and images, standardise them and georeference them. The results go
into a searchable database with a world map. The UI takes the LGRB portal
(bohrungen.lgrb-bw.de) as its model, made more modern.

What the challenge owner weighs most is in
[docs/challenge-owner-feedback.md](docs/challenge-owner-feedback.md). Coordinates come
first, and quality beats quantity. Uncertain records go to a person.

- `schema/`: the canonical Pydantic model (`borehole.py`) and the generated JSON schema.
- `pipeline/`: ingest, then classify, extract, assemble, georeference, score and store.
  CLI: `python -m pipeline …`.
- `api/`: FastAPI over SQLite (R-tree for the map box, FTS5 for search).
- `web/`: React 19, Vite, TypeScript and MapLibre.
- `docs/prompt-backlog.md`: prompt changes collected for the next `PROMPT_VERSION`.

## Data confidentiality — read before anything else

The challenge dataset is **confidential** real project data. Only the event participants
may use it, only for this challenge, and it is deleted after the event.

- `temp/` (dataset, notes) and `data/` (pages, cache, DB) are git-ignored and stay that
  way.
- **Nothing from the dataset goes into the repo.** That covers names, project names,
  borehole IDs, coordinates, places, excerpts and file names. It applies to code, tests,
  fixtures, prompts, docs and commit messages. Examples and tests use public landmarks or
  invented values (see `tests/test_georef.py`, `fixtures/demo_data.py`).
- Describe findings generically ("multi-sheet log in feet"), never by document.
- Never print, log or ask for credentials. Look at `.env` only with the values redacted.
  A subscription OAuth token (`sk-ant-oat…`) is not an API key and is never routed into
  the API backend.

## Languages

| | Language |
|---|---|
| Code, identifiers, comments, docstrings | **English** |
| Commit messages, branch names | **English** |
| UI text | **English** |
| README, `docs/*`, this file | **English** |
| Chat and reports to Marvin | **German** |

## Commands

Python runs in `.venv` (`source .venv/bin/activate`). The frontend uses `npm` in `web/`.

| Command | Purpose |
|---|---|
| `python -m pytest -q` | all tests, about 3 s |
| `cd web && npm run lint` | oxlint |
| `cd web && npm run build` | type check (`tsc -b`) and production build |
| `python -m pipeline estimate` | page count and cost before a run |
| `python -m pipeline run --doc <id>` | extract one document (costs money or quota) |
| `python -m pipeline rebuild` | reassemble from the cache, free |
| `python -m pipeline eval` | extraction quality against human-checked records |
| `DB_PATH=data/demo.sqlite python -m fixtures.demo_data` | synthetic demo DB, no data or key needed |

**LLM runs cost money or subscription quota.** Run `pipeline run` beyond a single test
document only when asked. The same goes for anything that invalidates the cache. A
`PROMPT_VERSION` bump re-extracts every page, which is why prompt changes are collected in
`docs/prompt-backlog.md` and shipped together. Code changes in assembly, georef or scoring
need only `rebuild`.

### Live checks

Servers are started through `.claude/launch.json`: `api` on :8000 with the real DB,
`api-demo` with the synthetic DB, and `web` (Vite on :5173, proxying `/api`). Reuse a
running server before starting another one. A "no source page" in the UI usually means the
demo DB is loaded.

## Quality gates

Proportionate, not ceremonial. This is a hackathon prototype that has to work reliably on
demo day.

- Any Python change: `python -m pytest -q` is green.
- Any frontend change: `npm run lint` shows no errors (existing warnings are fine) and
  `npm run build` passes.
- UI changes that affect layout or interaction: look at them once in the browser.
- Cosmetic edits (comments, copy, renames within a file) need only the matching linter or
  the tests.

### Tests

Write a test where a bug would silently corrupt data. That means parsing, georeferencing,
assembly, scoring and review persistence: one focused test per rule or per fixed bug,
including the failure case. Coordinate parsing gets a case in the notation that broke.

No tests for UI components, no snapshot tests, no tests that only restate the
implementation, and no coverage targets. Tests must not call the LLM. Feed them cached or
hand-written page results.

## Code conventions

- **Comments only where they add what the code can't say:** a non-obvious why, a quirk
  of the source documents, a limit of an API. Keep them short, in English, and about the
  current state, never about the change. No docstrings that restate the signature, no
  section banners, no narration of the next line. Most code needs none.
- **Every implementation reads as the first one.** Nothing in the tree hints that an
  earlier version existed: no "now"/"no longer"/"legacy", no compatibility shims or dead
  fields. The only exceptions are the DB migration in `store.py` and the cache keys. Only
  the commit message describes a change.
- **Before writing code, stop at the first rung that holds:**
  1. Does it need to exist? If nobody asked for it and nothing uses it, leave it out.
  2. Is it already in the repo? Check `pipeline/`, `web/src/` and the `schema/` helpers.
  3. Is it already installed? Pydantic, pyproj, PyMuPDF, the stdlib, MapLibre.
  4. Only then write the smallest implementation that works. New dependencies need a
     reason.
- No dead exports, no unused parameters "for later", no config with one value.
- Never cut safety to save code: validation, the blocking findings and error handling
  stay. Cut abstraction nobody needs yet.
- **Python:** type hints, Pydantic v2 for every structure crossing a boundary (LLM
  output, DB, API). Settings come from environment variables in `pipeline/config.py`;
  there are no magic paths elsewhere. A new field goes into `schema/borehole.py` first,
  then the JSON schema is regenerated.
- **Frontend:** function components and hooks, no UI library. API calls only through
  `web/src/api.ts`, shared types in `web/src/types.ts`. Colours, fonts and sizes come
  from the CSS variables in `web/src/index.css`. Hex values belong only where the colour
  is data: the lithology patterns and the ICS colours in `lithology.tsx`. Keep it
  keyboard-usable, with visible focus.
- **Pipeline rules:** the release score is `min(extraction, position)` confidence.
  Findings that could hide wrong data are **blocking** and always go to review. Do not
  loosen them to raise the auto-release rate. A human decision persists only while the
  record's content hash is unchanged.

## Known pitfalls

- **Structured outputs:** a schema allows at most 16 nullable/union parameters and 24
  optional parameters. So classification uses strict structured output, while extraction
  returns JSON that Pydantic validates locally, with one repair round.
- **MapLibre 6:** it has named imports only. The worker needs `setWorkerUrl` with a
  `?url` import, and `optimizeDeps.exclude: ['maplibre-gl']` in `vite.config.ts`.
  Otherwise the map stays empty with "Worker failed to load".
- **CLI backend (`LLM_BACKEND=claude-code`):** `_cli_env()` strips API credentials so
  `claude -p` uses its own login. Do not pass a key through.
- **`--max-pages`** truncates a document. Such documents are complete in the DB only
  after a full run.
- **Deleting the DB deletes review decisions and corrections.** Derived tables can be
  rebuilt from the cache; human decisions cannot.

## Workflow

- Work happens on the branch **`marvin`**. Commit finished, verified work there.
  **Never push, rebase or rewrite history unless asked.**
- Parallel sessions (Claude and Codex) often work in the same checkout. **Stage only
  your own files** (`git add <paths>`, never `git add -A`). Leave other people's
  uncommitted changes alone.
- Larger changes (several modules, or a new pipeline rule) get one review pass before
  the commit, for example Codex as a second opinion. Treat review reports as hints and
  verify each finding yourself.
- Scratch scripts and intermediate results go to the scratchpad or `temp/`, not the repo.

### Commit messages — Conventional Commits

Format: `<type>(<scope>): <description>`, in imperative mood, lowercase after the colon,
with no trailing period, in English. The body explains the why when it isn't obvious.

Scopes: `schema`, `ingest`, `llm`, `prompt`, `georef`, `assemble`, `scoring`, `store`,
`review`, `eval`, `export`, `api`, `web`, `map`, `docs`, `deps`.

Types: `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `build`, `chore`. A
`BREAKING CHANGE:` footer marks changes to the DB schema, the export format or the API
that old data or clients can't handle.

```text
feat(georef): read packed DDMMSS coordinates
fix(assemble): keep the last layer open when the sheet continues
feat(prompt): report depths as printed and convert in code (PROMPT_VERSION v4)
docs(readme): add troubleshooting for the CLI backend
```

The history before this file uses plain sentences. That is the old state, not a template,
and it is not rewritten.

### Versioning — SemVer

Versions are git tags `vMAJOR.MINOR.PATCH` on `marvin`, starting at `v0.1.0`. While the
version is `0.x`, `feat` bumps MINOR and `fix`/`perf` bump PATCH. Tag at milestones (the
state shown to the jury, the final submission) rather than on every commit, and only when
asked. The Conventional Commit log is the changelog; there is no separate file.
`PROMPT_VERSION` and `LAB_PROMPT_VERSION` in `pipeline/config.py` are independent: they are cache keys, bumped only
when the prompt changes.
