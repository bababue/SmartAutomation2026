# Features

What Borehole Atlas does, and ideas for what it could do next. How to run it is in [README.md](README.md);
how a document flows through the pipeline, with diagrams, is in [docs/pipeline.md](docs/pipeline.md);
the challenge owner's priorities are in [docs/challenge-owner-feedback.md](docs/challenge-owner-feedback.md)
(referenced below as *feedback #n*).

## What it does

### Ingest and extraction

- Reads PDF (text, scanned, vector-only), JPG/PNG/TIFF and DOCX (through its PDF export).
- Renders and processes every page on its own, so file size does not matter (reports of ~80 MB / 150 pages).
- Classifies pages on a thumbnail (log, continuation sheet, coordinate table, lab sheet) and extracts only
  the relevant ones at full resolution.
- A vision LLM fills a slim extraction schema; IDs, CRS, WGS84 position, confidence and review state are set
  in code, never by the model.
- Merges multi-sheet logs by hole name, including descriptions and back-references across a sheet break;
  continuation sheets get a summary of the previous sheet.
- Normalises units to metres/SI, lithology to DIN 4023 classes, and translates descriptions to English while
  keeping the original.
- Keeps lab values with their qualifiers and ranges (`<5`, `5–10`) instead of inventing a point value.
- Caches every page: interrupted runs resume, and reassembly (`pipeline rebuild`) is free.
- Backends: Claude (API or subscription), OpenRouter (zero-data-retention providers) and local models through
  the OpenAI-compatible interface; token usage and cost are recorded per page.

### Georeferencing (feedback #1)

- Resolves the coordinate system to an EPSG code: UTM, geographic, national and local grids.
- Reads notation variants: DMS, decimal minutes, hemisphere letters, unsigned westings, packed values.
- Applies a CRS stated anywhere in a document to logs that do not name one.
- Checks every position against the geocoded place; without coordinates it geocodes the place and marks the
  position as approximate.
- Sends inferred or ambiguous systems, and different holes sharing printed coordinates, to review.
- Fits a local grid (Helmert or affine) from positions a reviewer has set, and places the other holes of that
  grid with it.

### Quality and human in the loop (feedback #3)

- Release score = the weaker of extraction and position confidence, computed from evidence rather than the
  model's own estimate.
- Auto-release only at a score of 90 or more and without blocking findings.
- Blocking findings: layer gaps or overlaps, depth contradictions, ambiguous CRS, empty logs, depths printed in
  feet. A lost log page or a hole that could not be assembled becomes its own blocking placeholder record.
- Copies of one hole from several documents are combined; the copy that is best after corrections leads.
  Differing properties across a sheet break are flagged for a look.
- Review queue, weakest first, with keyboard shortcuts and a filter for blocking findings.
- Inline corrections; a decision holds only while the record's content is unchanged.
- Hovering a value outlines where it was read on the source page.
- Learns from reviewers: confirmed coordinate systems per country, local terms mapped to DIN 4023 classes.
  Every learned item is visible and removable.

### Evaluation (feedback #8)

- `pipeline eval` compares extraction against human-checked records: layer boundaries, precision, lithology,
  position error in metres, CRS, final depth, groundwater and lab values.
- Isolated prompt experiments with their own cache and database, and holdout document groups.
- Measured model comparison (Opus and Haiku) on quality, cost per page and time, in
  [docs/model-comparison.md](docs/model-comparison.md).

### Database, API and export

- SQLite with an R-tree for the map box, FTS5 full-text search, versioned records and a review audit log.
- Load-tested with 50,000 synthetic boreholes (map points 0.8 s, "clay between 10 and 20 m" 0.2 s).
- Excel export in Herrenknecht's BoreholeDataFormat (Metadata, Logdata, Samples, CoreRuns, SPT,
  Groundwater, TestResults) and a per-layer CSV; released records only by default.
- Web upload with a job queue that survives a restart.

### Web app

- World map with clusters; colour shows the release state, a ring marks an approximate position.
- From zoom 15, boreholes become 3D columns with lithology patterns at a 1:1 depth scale.
- Search and filters: full text, release state, position quality, lithology with depth range, lab values with
  min/max.
- Detail view: two-column profile (stratigraphy in ICS colours, DIN 4023 lithology), layers, tests and the
  source page side by side.
- Section view (feedback #5): holes side by side along a line at ground level; rock head and groundwater are
  joined between neighbours up to 150 m apart, with a band that widens with distance. Layer boundaries are
  joined where the material sequence of both holes matches unambiguously, drawn dotted as an estimate.
- Similar boreholes (feedback #6): holes in other projects with comparable ground, with a one-line reason.
- Result list grouped by project (document group) with count and release share; "Show on map" filters and
  zooms to the group.
- Drag-and-drop upload, menu with review queue and export, dark mode, shareable URLs for view and selection.
- Statistics with a quality tab: extraction quality against the reviewed reference, release audit, Opus against
  Haiku with cost per page, prompt experiments side by side, recorded model calls.

## Feature ideas

| Idea | Why | Effort |
|---|---|---|
| Statistics view: lithology distribution, depths, release rate, most frequent findings per document | Feedback #9 | M |
| Anomalies across the collection: depth, elevation or lithology outliers against neighbouring holes | Feedback #9 | M |
| Draw a tunnel route and pick the holes in a corridor along it for the section | Feedback #5 | M |
| Search by ground profile ("rock above 10 m, clay on top") instead of by a single hole | Feedback #6 | M |
| Interface in English and German | Users at the challenge owner | M |
| Measure Mistral or Azure OpenAI through the OpenAI-compatible backend | Feedback #7, #8 | S–M |
| Projects and sites as entities with one canonical project name across sheets | Data model review | M |
| Authentication and per-project permissions | Required before shared use | M–L |
| Server-side map tiles for more than ~100,000 points | Data model review | L |
