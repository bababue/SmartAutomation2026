# How a document becomes a borehole record

From an uploaded file to a released record on the map: what the model reads, what our code decides, and where
a person steps in. Module names refer to `pipeline/`.

## Overview

```mermaid
flowchart TD
    A[PDF / scan / image / DOCX] --> B[Render every page<br/>ingest.py]
    B --> C{Classify page<br/>thumbnail, vision LLM}
    C -->|log, test pit, probe| D[Extract log<br/>full resolution, vision LLM]
    C -->|coordinate table| D
    C -->|lab results| L[Extract lab values<br/>vision LLM]
    C -->|text, legend, plan, photo| X[skipped]
    D --> K[(Page cache<br/>keyed by image + prompt + model)]
    L --> K
    K --> E[Assemble holes<br/>assemble.py]
    E --> G[Georeference<br/>georef.py]
    G --> H[Apply learned knowledge<br/>and reviewer corrections]
    H --> M[Combine copies of one hole<br/>across documents]
    M --> S[Score and decide<br/>scoring.py]
    S -->|score ≥ 90 and no blocking finding| R[Released]
    S -->|otherwise| Q[Review queue]
    Q --> P((Person))
    P -->|approve / reject| R
    P -->|correct a value| H
    R --> DB[(SQLite: R-tree, FTS5,<br/>versions, review log)]
    Q --> DB
    DB --> UI[Map, search, profile,<br/>tunnel route, export]
```

Only the two LLM boxes call a model. Everything after the cache is code, so changing a rule means
`pipeline rebuild`: it runs in seconds and costs nothing.

## The model reads, the code decides

| The vision model does | Our code does |
|---|---|
| Says what a page is (log, continuation sheet, coordinate table, lab sheet, report text) and which hole names it shows | Picks which pages are worth a full-resolution extraction |
| Copies what is printed: hole name, sheet "2 of 3", coordinates and CRS text **verbatim**, elevation, dates | Parses coordinates in every notation, resolves the CRS to an EPSG code, transforms to WGS84 |
| Reads each layer: depths, description in the original language, an English translation, the main constituent as a DIN 4023 class, stratigraphy code and colour | Joins sheets into one hole, checks gaps and overlaps, compares the depths with the PDF's own text layer |
| Reads SPT, core runs, samples, groundwater, lab values with their qualifiers (`<5`, `5–10`) | Converts units, keeps ranges as ranges, links lab sheets to holes by name and depth |
| Estimates where the named place is, and how precise that estimate is | Uses that estimate only to check the printed coordinates, or as an approximate position when none are printed |
| Lists what it could not read | Computes the confidence; the model never scores itself |
| Marks where on the page each layer and the coordinates are printed | Outlines that region on the source page when a reviewer hovers the value |

IDs, coordinate systems, WGS84 positions, confidence and review state are never taken from the model.

## Inside the LLM steps

```mermaid
flowchart LR
    subgraph classify [1 · Classify, every page]
        T[Thumbnail<br/>900 px] --> CL[Model, low effort<br/>strict structured output] --> PC[page kind, hole names,<br/>language, CRS text]
    end
    subgraph extract [2 · Extract, relevant pages only]
        F[Page image<br/>2000 px] --> EX[Model, medium effort]
        TX[Text layer of the PDF<br/>as a hint only] --> EX
        CX[Summary of the previous sheet<br/>last layers, hole name] --> EX
        KN[Learned knowledge<br/>local terms, known CRS] --> EX
        EX --> J[JSON] --> V{Pydantic<br/>validation}
        V -->|invalid| RP[One repair round] --> V
        V -->|valid| OUT[Page extraction]
    end
    PC --> F
```

- Classification uses strict structured output. The extraction schema is too large for that (structured
  outputs allow at most 16 nullable and 24 optional parameters), so the model returns JSON that Pydantic
  validates locally.
- Continuation sheets run in page order, each with a summary of the sheet before. Independent logs run in
  parallel.
- Every answer is cached per page. An interrupted run resumes, and a page is extracted again only when its
  image, the prompt version or the model changes.
- The backend is a setting: Claude through the API or a subscription, OpenRouter, or a local model. The corpus
  runs on Opus; live web uploads use Haiku for speed (comparison in [model-comparison.md](model-comparison.md)).

## Example: one page in, one record out

Invented values, for illustration only.

```text
Printed on the sheet                      Model output (excerpt)                 Stored record (code)
─────────────────────────────────────     ─────────────────────────────────      ───────────────────────────────
BOREHOLE BH-1        Sheet 1 of 2         name: "BH-1", sheet: "1 of 2"          id: <group>:borehole:BH-1
UTM 32N  E 421 650  N 5 369 084           coords_raw: "E 421 650 N 5 369 084"    EPSG 32632 → 48.47 N, 7.94 E
                                          crs_raw: "UTM 32N"                     position confidence 0.95
Ground level 165.2 m a.s.l.               elevation_raw: "165.2 m a.s.l."        elevation 165.2 m
0.00 – 0.40  Topsoil, brown               layer 0.0–0.4, primary: topsoil        joined with sheet 2,
0.40 – 3.10  Clay, silty, stiff, grey     layer 0.4–3.1, primary: clay,          no gaps, depths found in
                                            secondary: silt (slightly)            the PDF text → confirmed
3.10 – 6.00  Sandstone, weathered         layer 3.1–6.0, primary: sandstone      rock head 3.1 m
Water struck at 2.4 m                     groundwater 2.4 m, struck              score 0.95 → released
```

## Georeferencing

```mermaid
flowchart TD
    A[coords_raw + crs_raw<br/>as printed] --> B[Parse every reading:<br/>DMS, decimal minutes, hemisphere letters,<br/>packed values, unsigned westings]
    B --> C[Candidate CRS: the one printed,<br/>one stated elsewhere in the document,<br/>the country's usual grids]
    C --> D[Transform each candidate to WGS84]
    D --> E{Near the place the<br/>sheet names?}
    E -->|one clear winner| OK[Position, high confidence]
    E -->|CRS only inferred| INF[Position, below release threshold]
    E -->|several readings fit| AMB[Blocking: CRS ambiguous]
    E -->|nothing fits| FAR[Finding: does not match the place]
    A -->|no coordinates printed| GEO[Geocoded place only<br/>shown as a ring on the map]
    INF --> REV[Review: confirm the CRS<br/>once per document]
    AMB --> REV
    REV --> LEARN[Learned per country;<br/>positions set by hand become<br/>control points for a local grid]
```

## Release decision

```mermaid
flowchart TD
    A[Assembled record] --> X[Extraction confidence<br/>layer sequence, unreadable items,<br/>depths found in the PDF text]
    A --> P[Position confidence<br/>from georeferencing]
    X --> S["Score = min(extraction, position)"]
    P --> S
    S --> B{Blocking finding?<br/>gaps or overlaps, depth contradiction,<br/>ambiguous CRS, empty or lost log,<br/>depths in feet}
    B -->|yes| Q[Review queue]
    B -->|no| T{Score ≥ 90?}
    T -->|no| Q
    T -->|yes| R[Released automatically]
    Q -->|approved by a person| R2[Released]
```

A person's decision holds as long as the record's content is unchanged. When a re-run changes the content,
the record goes back to the queue with a note. Corrections are stored apart from the extraction and are applied
again on every run, so they survive re-extraction.

## Several documents, one hole

The same hole often appears in several files: the log itself, a coordinate table, a lab report, an older
version of the report. Within a document group the code:

1. joins continuation sheets by hole name and sheet number;
2. links coordinate tables and lab sheets to holes by name (and lab values by depth);
3. combines copies of one hole from different documents, ranking them by how they look after knowledge and
   corrections are applied; a copy with a blocking finding never leads;
4. turns a log page that came back empty, or a hole that could not be assembled, into a blocking placeholder,
   so nothing disappears silently.
