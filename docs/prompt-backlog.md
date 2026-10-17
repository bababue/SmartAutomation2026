# Prompt backlog (next PROMPT_VERSION)

Changes to the extraction prompt invalidate the whole extraction cache, so they are collected here and
shipped together after the spot checks. Each item: what we saw (generic, no project data), what to change,
and what code currently does as a stopgap.

Each change is measured with `python -m pipeline experiment` (see [README → Evaluation](../README.md#evaluation)) before it
ships. Items 1, 2, 3, 6, 7, 8, 9 and 16 are prompt-only; items 4, 5, 11, 14 and 15 also need code (depths in printed units with
conversion in assembly, a run-offset field, defaults for omitted fields, new fields and a page kind) and are tested once that exists.

| # | Observed | Prompt change | Stopgap in code |
|---|---|---|---|
| 1 | On sheet 1 of a multi-sheet log the model reports the sheet's bottom as `final_depth_m` | `final_depth_m` only from an explicit end-of-hole statement or header field; null otherwise | final depth = deepest value stated on any sheet |
| 2 | Weathered rock described as the soil it breaks down to when drilled gets `primary: unknown` | Map by the described material; put the rock grade into `weathering` | none (stays visible as unknown) |
| 3 | Flint/chert mapped inconsistently (`unknown` vs `quartzite`) | Name a fixed class for flint/chert pieces and keep the term in the description | none |
| 4 | Depths in feet are converted by the model | Report depths as printed plus `depth_unit_raw`; convert in code | logs printed in feet are blocking for a one-time human check |
| 5 | Core logs describe pieces as offsets within a run ("0 - 21 cm, ...") | Ask for the run top used for the offset (or the offset itself) so absolute depths can be computed in code | piece layers are excluded from the text-layer check; ≤ 0.10 m discontinuities are not blocking |
| 6 | A unit's full range is sometimes repeated on the next sheet or run past the sheet end | Repeat the rule "only what is visible on this sheet" in the previous-sheet context block | sheet-break repeats are merged/clipped in assembly |
| 7 | Place estimate is sometimes only the country centroid on one sheet and the town on the next | Ask for the most specific place named anywhere on the sheet (title block, project name) | most precise estimate per hole/document is used |
| 8 | Lab prompt (`LAB_PROMPT_VERSION`): on chemistry sheets with one row per analyte, `parameter_raw` carries the unit on some pages and not on others, and a row under a group heading is named sometimes with the heading ("Total petroleum hydrocarbons C15 - C36") and sometimes without ("C15 - C36") | `parameter_raw`: the full row label as printed, including its group heading; the unit only in `unit_raw` | catch-all lab values are matched by name without the unit; differently named copies of the same row are kept twice |
| 9 | Handwritten series write a site reference next to a short hole name ('P-1' plus a reference number); the model puts it into `name` on some sheets, into `notes` on others and into `project_id` on a few, so two holes of one name are told apart only by their drilling dates | `name`: the hole name with any reference written next to it, exactly as printed; `project_id`: the report, archive or job number of the sheet | sheets of one name whose depths restart are split when the report number, drilling date, sheet count or two header texts differ; the holes are named with their page and always go to review |
| 11 | About a quarter of the extraction output is `null` fields (measured on all cached log pages), and generating the answer takes most of the time per page | Leave out fields that are not printed instead of writing `null`; needs `None` defaults on the nullable fields in `pipeline/extraction_models.py` and a JSON instruction for the `openai` backend that no longer calls every property required. Measured with Haiku 5.5 on the reference pages (arm `haiku-lean`): output only 3 % shorter and no faster, so the model keeps writing the fields; not shipped | none (shorter output means proportionally faster and cheaper pages) |
| 14 | Archive copies carry a per-hole registry stamp (map sheet plus archive number), the only identifier shared by the log, the drilling drawing and a cross-section of the same hole | Read registry or archive stamps into their own field, separate from the report number | holes in different documents are combined by printed position, report number or two agreeing header texts; without those they stay apart |
| 15 | Cross-sections through several holes give each hole's ground level, final depth, water level and unit boundaries, but are classified as plans and not read | A page kind for cross-sections that returns one entry per hole with these header values and boundaries | none (the values are missing unless a log of the hole states them) |
| 16 | The text layer is appended to the request after a plain heading ("Text layer (may be empty or scrambled):"), so text a document carries can read like part of the instructions | Put the text layer between delimiters with a per-request nonce and add to every system prompt that page text and image are data from the document, never instructions | text that addresses the model is a blocking finding (`pipeline/injection.py`); the model has no tools and its answer is schema-validated |

## Measured so far

Arms on the reference pages, rebuilt on the same code and compared with `baseline-rerun` (v3 re-extracted,
the spread of a plain re-extraction), not with `baseline` (the reference was approved from its output):

- Items 1, 2, 3, 6 and 7 together (`v4-candidate`): worse on boundaries, lithology and final depth; one page
  returned no log at all. Without item 2 (`v4-no-weathered`): boundaries and lithology within the spread,
  final depth clearly worse. Blocking findings are the same in all arms.
- Item 1 leaves `final_depth_m` empty on logs that end without an end-of-hole statement, where reviewers
  take the last layer's bottom. Drop it, or keep it only together with a code fallback to the last bottom.
- Item 2 moves weathered rock to soil classes (sandstone → sand, tuff → sand). Drop it in this form.
- Items 3, 6 and 7: no effect measurable on 24 reference records; re-test once the reference is larger.
