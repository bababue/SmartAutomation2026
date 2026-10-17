# Data model review (Codex, read-only, code only) and load test

Reviewed commit: fe684dc. Synthetic load test: 50,000 boreholes (tests use no project data).

## Before and after

| Path | Before | After |
|---|---|---|
| Map | 2.3 s / 39 MB (all fields) | 0.8 s / 1.5 MB gzipped (slim points) |
| List | everything | page of 200 in 0.3 s, total and counts from the DB |
| One lithology | 1.9 s | 0.3 s (layers table) |
| Lithology in a depth range | not possible | 0.2 s |
| UCS >= 80 MPa | 0.8 s via JSON, not in the API | 0.1 s (lab_values table) |
| Review queue | 5.0 s | 6 ms per page |
| CRS groups | 4.2 s | 2 ms (findings table) |
| CSV export | 4.7 s buffered, 179 MB in memory | streamed, first bytes after 21 ms |
| Delete one document (rebuild) | ~1 s | 12 ms |

## Status of the review's recommendations

| Recommendation | Status |
|---|---|
| 1. Indexes, FTS by rowid, paging, slim map, CSV streaming | done (fdf9742, de9d7c2, fad47d4, 39a4088) |
| 2. Durable identity, record versions, review revision | done (fb2bbc6): uid + aliases, record_versions, review_revision; corrections and review events follow the uid |
| 3. Queryable entities | done: layers, lab_values, findings, record_sources (fb2bbc6) |
| 4. Short publication transactions | done per document group (f7d7200); unchanged records are still rewritten |
| 5. Authentication, project permissions, durable jobs | open: required before shared use |
| Projects/sites/document groups as entities, FKs, CHECK constraints | open |
| Server-side map tiles/clusters beyond ~100,000 points | open |

## Review

**Fit for a local prototype; not yet fit for a global engineering database.** The first limits are unbounded API responses, repeated scans during rebuilds, and unstable identity. Moving to PostgreSQL without fixing these would preserve most problems.

This is a code review plus in-memory `EXPLAIN QUERY PLAN` checks against the supplied schema. Performance numbers below are your measurements; projections are estimates.

For normal form and integrity, these are the material findings:

| Priority | Finding and evidence | Consequence |
|---|---|---|
| **Critical** | **“Stable” IDs depend on mutable input.** Document IDs derive from paths; borehole IDs derive from extracted names, with collision-dependent rewriting. [ingest.py:42](pipeline/ingest.py:42), [assemble.py:74](pipeline/assemble.py:74), [assemble.py:503](pipeline/assemble.py:503) | A file move, corrected extraction of a name, or new naming collision can create a new identity. Previous approvals no longer carry over; corrections and review events remain under the old ID. **Unmatched corrections do not produce a stale-correction finding**, because replay only visits newly assembled IDs. |
| **Critical** | **Approval hashes do not cover everything reviewed/exported.** `content_hash()` omits project, location name, dates, investigation type, notes and findings. Export publishes several omitted fields. [store.py:91](pipeline/store.py:91), [export.py:103](pipeline/export.py:103) | An approved record can change project or dates while retaining approval. New blocking findings can also be bypassed by unchanged-hash carry-over. Conversely, hashing complete nested location/layer objects makes provenance/confidence changes invalidate approval even when engineering values are unchanged. Define the approval contract explicitly. |
| **High** | **JSON and columns are competing representations without database enforcement.** Name, location, depth, status, counts, lithologies and strip duplicate JSON content. Current write helpers generally synchronize them transactionally. [store.py:151](pipeline/store.py:151), [store.py:207](pipeline/store.py:207) | Drift is a structural risk, not demonstrated existing corruption. `depth` is already a derived value: missing/falsey `depth_m` falls back to the **last listed** layer bottom, although layers need not be depth-sorted. |
| **High** | **Review state has three representations:** JSON fields, `boreholes.review_status`, and events. Events contain a hash but no retained record version. [schema/borehole.py:412](schema/borehole.py:412), [store.py:38](pipeline/store.py:38), [store.py:190](pipeline/store.py:190) | You can see who decided, but cannot reliably reconstruct what they saw. Automatic invalidation/carry-over is not itself an event. `reset()` deletes review history while retaining corrections and knowledge. [store.py:75](pipeline/store.py:75) |
| **High** | **Scopes are string conventions rather than entities.** “Document” means the ID prefix before `__`; project scope removes punctuation and case from the project name. [store.py:218](pipeline/store.py:218), [knowledge.py:40](pipeline/knowledge.py:40) | Renames strand scoped knowledge/corrections; unrelated projects can collide. Country knowledge survives source renames/deletions, and path-derived evidence can count a renamed source as independent confirmation. There is no project/site identity or explicit document-group membership. |
| **High** | **No declared foreign keys, almost no SQL validation, and important indexes are absent.** [store.py:17](pipeline/store.py:17) | Pages can lack documents; histories can reference nonexistent boreholes; statuses/scopes/payloads can be invalid. Required missing indexes include document membership, correction targets and review-event lookup. Existing `boreholes.id` uniqueness and `pages(doc_id,page)` already cover their respective lookups. |

On normal form specifically: SQLite treats `data TEXT` as an atomic value, so “JSON violates 1NF” is too simplistic. **The application’s conceptual model contains repeating entities hidden inside one column**, while also storing independently writable copies of their summaries. It lacks relational keys and constraints for those entities. Pydantic is a useful interchange/validation model; it should not dictate physical persistence.

The domain objects are partly present already—layers, samples, tests, groundwater and source references exist in Pydantic—but **none has a relational identity**. [borehole.py:398](schema/borehole.py:398) Findings are strings, lab results reference samples by name, and provenance references files/pages rather than page keys. [borehole.py:342](schema/borehole.py:342), [borehole.py:406](schema/borehole.py:406)

At scale, the following paths fail first:

| Path | Why it scales poorly | Concrete change |
|---|---|---|
| **Map: 2.3 s / 39 MB** | Fetches every matching row, parses every strip and constructs every GeoJSON feature; no limit or pagination. Strip size also grows with layers. [api/main.py:41](api/main.py:41), [api/main.py:108](api/main.py:108) | Require viewport/zoom for map requests; return server-side clusters/tiles at low zoom. Markers need ID, coordinates, status and selected summary fields. Fetch strips separately for visible list rows. |
| **Lithology: 1.9 s** | Comma-separated primary codes searched through concatenation and leading-wildcard `LIKE`; confirmed scan. Multiple filters mean AND across the **whole borehole**, not the same interval. [api/main.py:92](api/main.py:92) | Indexed layer/component rows and `EXISTS` predicates. Preserve existing whole-hole filter semantics separately from interval queries. |
| **Review queue: 5.0 s** | Fetches and parses every pending record’s full JSON merely to retrieve reasons. Existing status/score index only partly supplies the requested ordering. [api/main.py:112](api/main.py:112) | Keyset pagination over deterministic `(score,name,id)` ordering; return structured findings/reasons without loading the record. |
| **CRS groups: 4.2 s** | Reads all pending JSON, then extracts EPSG from English issue text with a regex. [api/main.py:126](api/main.py:126) | Store finding code, blocking flag, document-group ID and candidate EPSG; aggregate in SQL. |
| **Document replacement: 1 s/document** | Document selection scans `boreholes`; **each individual deletion also scans FTS by `id UNINDEXED`**. Corrections use the same deletion path. [store.py:35](pipeline/store.py:35), [store.py:109](pipeline/store.py:109) | Index `doc_id`; bind FTS `rowid` to the durable integer borehole key; update existing records instead of replacing them. |
| **CSV: 4.7 s / 179 MB; XLSX worse** | CSV validates complete records and buffers the entire output before wrapping it in `StreamingResponse`. XLSX fetches everything, constructs all Pydantic objects, row lists and a normal workbook. [api/main.py:490](api/main.py:490), [api/main.py:529](api/main.py:529), [export.py:95](pipeline/export.py:95) | Real chunked CSV from typed rows. Large exports should become bounded background jobs against a captured dataset version; partition spreadsheets. |
| **Detail: reported 3 ms** | Despite indexed borehole lookup, every request loads **all document paths and page keys**. [api/main.py:160](api/main.py:160) | Join only the record’s source links. The benchmark inserts no documents/pages, so it misses this cost. [bench.py:44](bench.py:44) |

At one million records with comparable content, simple proportional estimates are **~780 MB per full map response, ~3.6 GB CSV, and ~21 GB database**, before additional history, WAL and source artifacts. These are size estimates, not latency forecasts.

The rebuild path is particularly problematic:

- `_store()` assembles outside the transaction, but then holds **one `BEGIN IMMEDIATE` across all selected completed documents**, including knowledge application, correction replay, deletion and insertion. It is not one transaction per document. [__main__.py:217](pipeline/__main__.py:217)
- Correction lookup scans the corrections table per borehole; knowledge application can perform another lookup per borehole. Evaluation repeats this across the database. [store.py:233](pipeline/store.py:233), [knowledge.py:243](pipeline/knowledge.py:243), [evaluate.py:200](pipeline/evaluate.py:200)
- Every replacement rewrites JSON, strip, ordinary indexes, R-tree and FTS, even for unchanged content. `rid` is not preserved deliberately and can change or be reused. Repeated full-index FTS searches can make bulk replacement approach quadratic work.
- WAL permits concurrent readers, but still only **one writer**. Reviewers/corrections wait behind this transaction, with a 30-second connection timeout. Long exports can also delay checkpoint progress. [store.py:58](pipeline/store.py:58), [SQLite WAL documentation](https://www.sqlite.org/wal.html)

Keep the R-tree: the 32 ms selective bbox result is encouraging. However, the current containment predicate can exclude boundary points because R-tree coordinates are rounded outward. Use overlapping candidate boxes followed by exact coordinate checks, and split antimeridian-crossing viewports. [api/main.py:88](api/main.py:88), [SQLite R-tree documentation](https://www.sqlite.org/rtree.html)

For TBM design, several semantics need fixing alongside normalization:

- **Depth/lithology:** distinguish measured distance along the hole from vertical depth. The model documents along-hole layer depths but also has vertical final depth and inclination; a collar point plus inclination cannot describe a deviated borehole. Preserve datum, depth basis and eventually trajectory survey stations. [borehole.py:8](schema/borehole.py:8), [borehole.py:392](schema/borehole.py:392)
- **UCS/CAI:** query results by sample/interval, parameter, standardized units and method. A borehole containing limestone, high UCS and high CAI does not establish that those properties describe the same material. Store censoring/range bounds explicitly: current `LabResult` has a qualifier but only one numeric value. Repeated tests can also overwrite each other in the spreadsheet pivot. [borehole.py:342](schema/borehole.py:342), [export.py:125](pipeline/export.py:125)
- **Groundwater:** retain multiple dated observations, dry versus unknown, observation kind and reference elevation; do not reduce these to one borehole-level water depth. The existing model already preserves much of this. [borehole.py:272](schema/borehole.py:272)
- **Provenance:** “records from page X” must traverse every observation’s source links. `bh.sources` initially lists log pages; linked laboratory results retain their own sources without adding them to that list. Unmatched lab rows are only reported, rather than persisted for reconciliation. [assemble.py:543](pipeline/assemble.py:543), [assemble.py:644](pipeline/assemble.py:644)
- **Identity across sources:** deduplication chooses a richer record and merges source lists using normalized name/project and rounded coordinates. The winning ID can switch, and rebuilding a subset can produce different deduplication outcomes. Represent one physical investigation with multiple source assertions instead. [assemble.py:617](pipeline/assemble.py:617)

A concrete target model would be:

| Tables | Keys, relationships and important indexes |
|---|---|
| `projects`, `sites`, `document_groups` | Immutable PKs; site → project; explicit group membership. Names/external references are attributes or aliases, not internal keys. Add ownership/confidentiality and project-access membership. |
| `documents`, `document_versions`, `pages` | Durable document PK; immutable version with content hash and artifact location; page FK to document version; `UNIQUE(document_version_id,page_no)`. Preserve previous source images. |
| `boreholes`, `borehole_aliases` | Durable integer PK for SQLite indexes plus stable public UUID; site FK; current-version FK; legacy-ID mapping. Keep document-scoped name aliases without assuming names uniquely identify physical holes. |
| `extraction_runs`, `page_extractions`, `record_versions` | Run records model, prompt/schema/code versions and cache keys. Immutable page outputs and assembled versions; version → borehole, parent version and run. Capture applied correction/knowledge revisions and fitted-grid inputs/parameters. |
| `layers`, `layer_components` | Layer PK, record-version FK, ordinal, measured top/bottom, primary code and descriptions; components reference layer and lithology dictionary. `UNIQUE(version_id,ordinal)`; indexes `(version_id,top_m)` and `(primary_code,top_m,bottom_m,version_id)`. |
| `samples`, `lab_results`, `lab_parameters` | Sample PK and version FK; result PK, optional sample FK, explicit interval, parameter FK, method, lower/upper values, qualifier and raw text. Index `(parameter_id,value_lower,version_id)` and `(version_id,top_m)`; index sample FKs. Preserve replicate tests. |
| `groundwater_observations`, `core_runs`, `spt_tests` | Independent PKs and version FKs; depth/date/reference fields as applicable. Index `(version_id,depth_m)` or interval top; groundwater additionally by observation date when required. |
| `borehole_sources`, `layer_sources`, `test_sources`, etc. | Typed links with real FKs to version/observation and page; field/role and bbox where applicable. Index **page first** for reverse provenance queries. |
| `findings`, `review_events`, `review_state` | Findings reference version/observation and have code, severity, blocking flag and structured parameters. Events reference exact version and authenticated user. One authoritative current state per borehole, with monotonically increasing revision; queue index `(status,score,name,borehole_id)`. |
| `corrections`, `correction_applications` | Corrections reference durable borehole or explicit document-group FK, base version and optional target observation. Record replay outcome and resolved target per new version. Index `(borehole_id,id)` and `(group_id,id)`; ambiguous remapping becomes a finding. |
| `knowledge_items`, `knowledge_evidence`, `knowledge_applications` | Typed scope FKs, versioned payload, source and author. Unique independent-evidence links replace the JSON evidence array and mutable confirmation count. Record which knowledge revision influenced each version. |

Use relational effective values as authoritative; retain **immutable raw extraction JSON** as evidence. Generate the existing Pydantic response through an adapter. Map summaries, strips, FTS and current-layer/test search tables can remain disposable projections, maintained atomically.

For interval queries use `top_m < requested_bottom AND bottom_m > requested_top`. A composite B-tree narrows lithology and one range boundary; it does not magically index both inequalities. Start with spatially selected boreholes when selective, then inspect actual plans. Keep historical versions out of current-search projections as history grows.

Add `NOT NULL`, enum checks, ownership-consistent FKs and valid coordinate-pair checks. Preserve invalid extracted intervals as reviewable candidates/raw evidence with findings; do not discard them through blanket constraints. Enable foreign keys on every connection. Replace `INSERT OR REPLACE` with UPSERT before adding relationships, because replacement deletes the existing parent row. [store.py:81](pipeline/store.py:81), [SQLite foreign-key documentation](https://www.sqlite.org/foreignkeys.html)

I would migrate in this order, ranked by benefit per effort:

1. **Very high benefit / small effort — remove immediate amplification.** Add `boreholes(doc_id)`, `corrections(scope,target,id)`, `review_events(borehole_id,id)` and extend queue ordering to `(review_status,score,name,id)`. Rebuild FTS once with explicit `rowid=rid`, then delete/update by rowid. Paginate queues, slim map responses and implement actual CSV streaming. Indexes alone will not fix payload size.
2. **Very high benefit / medium effort — preserve identity and decisions.** Introduce durable borehole/document/group keys and backfill legacy mappings before changing ID generation. Retain old-ID API resolution. Add immutable record versions and mandatory optimistic concurrency on **both content version and review-state revision**. Corrections also need a base version. Currently hashes are optional, corrections have none, and two decisions on unchanged content can overwrite each other. [api/main.py:184](api/main.py:184), [api/main.py:289](api/main.py:289)
3. **High benefit / medium-to-large effort — backfill queryable entities.** Populate layers, tests, samples, findings and source links from current JSON. Preserve unresolved provenance/identity explicitly. Compare reconstructed records, counts, hashes and released exports before switching reads.
4. **High benefit / medium-to-large effort — change publication, not cache behavior.** Keep cache → assembly → knowledge → corrections. Stage candidates outside the writer lock; publish changed versions in short transactions per coherent document group, checking correction/knowledge revisions before commit. Preserve unchanged versions/index entries. Mark absent records retired only after a complete successful group rebuild.
5. **Required before shared confidential use — enforce access and durable work ownership.** No application authentication/authorization is visible; reviewer identity is supplied text, while page/export/knowledge endpoints expose their contents. [api/main.py:39](api/main.py:39), [api/main.py:452](api/main.py:452) Add authenticated users, project permissions and tenant-bounded knowledge scopes. Apply the same access rules to sources, caches and exports. Replace process-local rebuild threads/status with durable jobs. [api/main.py:344](api/main.py:344)

**SQLite can remain adequate for 100,000–1,000,000 boreholes on one host** if queries are selective, responses bounded and writes short; ten million layer rows alone is not a migration threshold. Validate with realistic documents/pages, growing corrections/history, selective engineering queries and concurrent review/rebuild/export workloads.

Move to **PostgreSQL/PostGIS** when sustained concurrent writes exceed the short-transaction design, deployment needs multiple database clients across hosts, or corridor/distance/trajectory queries and database-enforced project isolation become core requirements. PostGIS supplies indexed spatial operations; PostgreSQL supplies row security. Neither removes the need for stable identities, normalized observations and bounded APIs. [PostGIS indexes](https://postgis.net/docs/using_postgis_dbmanagement.html), [PostgreSQL row security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)