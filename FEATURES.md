# Features

What Borehole Atlas does, and ideas for what it could do next. How to run it is in [README.md](README.md);
how a document flows through the pipeline, with diagrams, is in [docs/pipeline.md](docs/pipeline.md);
the challenge owner's priorities are in [docs/challenge-owner-feedback.md](docs/challenge-owner-feedback.md)
(referenced below as *feedback #n*).

## What it does

### Ingest and extraction

- Reads PDF (text, scanned, vector-only), JPG/PNG/TIFF, DOCX (through its PDF export, else its embedded images),
  plain-text logs and Excel workbooks (both laid out on A4 pages first).
- Renders and processes every page on its own, so file size does not matter (reports of ~80 MB / 150 pages).
- Classifies pages on a thumbnail (log, continuation sheet, coordinate table, lab sheet) and extracts only
  the relevant ones at full resolution.
- A vision LLM fills a slim extraction schema; IDs, CRS, WGS84 position, confidence and review state are set
  in code, never by the model. A log page that returns fewer logs than the classification counted is read once
  more.
- Merges multi-sheet logs by hole name and sheet number, including descriptions and back-references across a
  sheet break; continuation sheets get a summary of the previous sheet. Missing sheets ("2 of 3") are reported.
- Combines copies of one hole across documents (a log, its image export, a drawing); within one folder or upload
  batch, one hole number is one hole. Records whose evidence points both ways stay apart and go to review.
- Links coordinate tables and lab sheets to their holes by type and number, so a borehole and a test pit of the
  same number stay apart.
- Normalises units to metres/SI, lithology to DIN 4023 classes, and translates descriptions to English while
  keeping the original.
- Keeps lab values with their qualifiers and ranges (`<5`, `5–10`) instead of inventing a point value.
- Caches every page: interrupted runs resume, and reassembly (`pipeline rebuild`) is free.
- Backends: OpenRouter (zero-data-retention providers, Haiku by default in `.env.example`), the Anthropic API,
  a Claude subscription through the Claude Code CLI, and local models through the OpenAI-compatible interface.
  Every model call is recorded (document, page, stage, model, endpoint, sizes sent, tokens, cost, outcome),
  never its content.

### Georeferencing (feedback #1)

- Resolves the coordinate system to an EPSG code: UTM, geographic, national and local grids.
- Reads notation variants: DMS, decimal minutes, hemisphere letters, unsigned westings, packed values, a UTM zone
  printed beside the values.
- Reads printed coordinates a second time from an enlarged crop; a different reading is a blocking finding.
- Applies a CRS stated anywhere in a document to logs that do not name one.
- Looks up the named place in an offline GeoNames gazetteer and checks every position against it; without
  coordinates the place gives the position, marked as approximate. Several holes of one folder or batch that
  would all sit on one place centre are left for a reviewer to place.
- Sends inferred or ambiguous systems, positions far from the document's other holes, and different holes
  sharing printed coordinates to review.
- Fits a local grid (Helmert or affine) from positions a reviewer has set, and places the other holes of that
  grid with it.

### Quality and human in the loop (feedback #3)

- Release score = the weaker of extraction and position confidence, computed from evidence rather than the
  model's own estimate.
- Auto-release only at a score of 90 or more and without blocking findings.
- Blocking findings include layer gaps or overlaps, depth contradictions, an ambiguous CRS, a second coordinate
  reading that disagrees, empty or missing log sheets, depths printed in feet, combined copies that disagree,
  and text in a document that addresses the model (possible prompt injection). A lost log page or a hole that
  could not be assembled becomes its own blocking placeholder record.
- Combined copies of one hole: the copy that is best after corrections leads. Differing properties across a
  sheet break are flagged for a look.
- Review queue, weakest first, also records without a position; filters by position source (printed,
  estimated from the place name, inferred CRS, none) and by finding; `A` approves, `R` rejects, the next record
  opens automatically.
- A document whose waiting holes share one inferred CRS can be confirmed for all of them at once.
- Inline corrections of fields, layers, groundwater, tests, position and CRS. A decision is refused if the
  record changed since the reviewer opened it, and holds only while the record's content is unchanged.
- Decisions and corrections follow a durable hole uid, so they survive a change of borehole id.
- Hovering a value outlines where it was read on the source page.
- Learns from reviewers: confirmed coordinate systems per country, local terms mapped to DIN 4023 classes.
  Every learned item is visible and removable.

### Evaluation (feedback #8)

- `pipeline eval` compares extraction against human-checked records: layer boundaries, precision, lithology,
  position error in metres, CRS, final depth, groundwater and lab values.
- `pipeline eval audit` draws automatic releases at random for a person to check and reports the error rate
  with a confidence interval; the sample opens in the review list.
- Isolated prompt experiments with their own cache and database, and holdout document groups.
- Measured model comparison (Opus and Haiku) on quality, cost per page and time, in
  [docs/model-comparison.md](docs/model-comparison.md).

### Database, API and export

- SQLite with an R-tree for the map box, FTS5 full-text search, queryable layer, lab and finding tables,
  versioned records and a review audit log.
- Load-tested with 50,000 synthetic boreholes (map points 0.8 s, list page 0.3 s, "clay between 10 and 20 m"
  0.2 s).
- FastAPI with typed response models: `/api/boreholes` (list, map points), `/api/boreholes/strips`,
  `/api/boreholes/{id}` with `/similar`, `/review` and `/corrections`, `/api/review-queue` with `/findings` and
  `/crs-groups`, `/api/uploads`, `/api/llm/usage`, `/api/stats`, `/api/stats/detail`, `/api/export.xlsx`,
  `/api/export.csv`.
- Excel export in Herrenknecht's BoreholeDataFormat (Metadata, Logdata, Samples, CoreRuns, SPT,
  Groundwater, TestResults) and a streamed per-layer CSV; released records only by default.
- Web upload of several files or a ZIP as one batch (up to 100 pages), assembled in one run. Files sent before
  (same SHA-256) and unsupported files are left out and reported. A batch, or single files of it, can be
  cancelled. The job queue survives a restart.
- Limits on model calls per upload batch and per UTC day.
- `pipeline share` packs a consistent copy of the database and page images for a teammate.
- A Dockerfile and Makefile build and test everything that runs without the dataset or a key, and serve the app
  with synthetic boreholes.

### Web app

- World map with clusters; colour shows the release state, a ring marks an approximate position.
- From zoom 15, boreholes become 3D columns with lithology patterns at a 1:1 depth scale; a menu switch turns
  them off.
- Search and filters: full text, release state with score range, lithology with depth range, lab values with
  min/max.
- Ground search (feedback #6): rock head and groundwater at most a given depth, metres of a material within a
  depth range, and presets such as "Clay over rock".
- Detail view: two-column profile (stratigraphy in ICS colours, DIN 4023 lithology), layers, tests and the
  source page side by side.
- Similar boreholes (feedback #6): holes in other documents with comparable ground, with a one-line reason.
- Tunnels (feedback #5): draw an alignment or import a WGS84 GeoJSON LineString, pick boreholes in a corridor,
  and set the tunnel axis by depth or elevation, with an optional diameter. Tunnels are saved in the browser and
  grouped by project.
- Longitudinal section along the tunnel: holes at their stations; rock head and groundwater are joined between
  neighbours up to 150 m apart, with a band that widens with distance. Layers between holes are estimated
  where the material sequences match, lighter beyond 150 m; correlated boundaries can be shown dotted. Drag to
  zoom.
- Statistics view (feedback #9) for the current filter: key figures, release funnel, release audit, findings,
  logged metres per lithology, final depths, documents with their most frequent finding, coordinate systems.
- Result list grouped by project (document group) with count and release share; "Show on map" filters and
  zooms to the group.
- Drag-and-drop upload with stage and page progress, model calls today against the limit, menu with review,
  statistics and export, dark mode, shareable URLs for view, selection and tunnel.

## Feature ideas

| Idea | Why | Effort |
|---|---|---|
| Anomalies across the collection: depth, elevation or lithology outliers against neighbouring holes | Feedback #9 | M |
| Model comparison page in the app (quality against cost per page) | Feedback #8 | S |
| Interface in English and German | Users at the challenge owner | M |
| Measure Mistral or Azure OpenAI through the OpenAI-compatible backend | Feedback #7, #8 | S–M |
| Projects and sites as entities with one canonical project name across sheets | Project names vary between sheets | M |
| Tunnels stored in the database and shared between users | Tunnels live in one browser | S–M |
| Authentication and per-project permissions | Required before shared use | M–L |
| Server-side map tiles for more than ~100,000 points | Map points are sent in full | L |
