# Prompt backlog (next PROMPT_VERSION)

Changes to the extraction prompt invalidate the whole extraction cache, so they are collected here and
shipped together after the spot checks. Each item: what we saw (generic, no project data), what to change,
and what code currently does as a stopgap.

Each change is measured with `python -m pipeline experiment` (see README → Prompt experiments) before it
ships. Items 1, 2, 3, 6, 7, 8, 9 and 10 are prompt-only; items 4 and 5 also need code (depths in printed units with
conversion in assembly, a run-offset field) and are tested once that exists.

| # | Observed | Prompt change | Stopgap in code |
|---|---|---|---|
| 1 | On sheet 1 of a multi-sheet log the model reports the sheet's bottom as `final_depth_m` | `final_depth_m` only from an explicit end-of-hole statement or header field; null otherwise | final depth = deepest value stated on any sheet |
| 2 | Weathered rock described as soil ("crumbling silty sand by drilling") gets `primary: unknown` | Map by the described material; put the rock grade into `weathering` | none (stays visible as unknown) |
| 3 | Flint/chert mapped inconsistently (`unknown` vs `quartzite`) | Name a fixed class for flint/chert pieces and keep the term in the description | none |
| 4 | Depths in feet are converted by the model | Report depths as printed plus `depth_unit_raw`; convert in code | logs printed in feet are blocking for a one-time human check |
| 5 | Core logs describe pieces as offsets within a run ("0 - 21 cm, ...") | Ask for the run top used for the offset (or the offset itself) so absolute depths can be computed in code | piece layers are excluded from the text-layer check; ≤ 0.10 m discontinuities are not blocking |
| 6 | A unit's full range is sometimes repeated on the next sheet or run past the sheet end | Repeat the rule "only what is visible on this sheet" in the previous-sheet context block | sheet-break repeats are merged/clipped in assembly |
| 7 | Place estimate is sometimes only the country centroid on one sheet and the town on the next | Ask for the most specific place named anywhere on the sheet (title block, project name) | most precise estimate per hole/document is used |
| 8 | Lab prompt (`LAB_PROMPT_VERSION`): on chemistry sheets with one row per analyte, `parameter_raw` carries the unit on some pages and not on others, and a row under a group heading is named sometimes with the heading ("Total petroleum hydrocarbons C15 - C36") and sometimes without ("C15 - C36") | `parameter_raw`: the full row label as printed, including its group heading; the unit only in `unit_raw` | catch-all lab values are matched by name without the unit; differently named copies of the same row are kept twice |
| 9 | Handwritten series write a site reference next to a short hole name ('P-1' plus a reference number); the model puts it into `name` on some sheets, into `notes` on others and into `project_id` on a few, so two holes of one name are told apart only by their drilling dates | `name`: the hole name with any reference written next to it, exactly as printed; `project_id`: the report, archive or job number of the sheet | sheets of one name whose depths restart are split when the report number, drilling date, sheet count or two header texts differ; the holes are named with their page and always go to review |
| 10 | The system prompt asks to list everything "not printed or not readable" in `unreadable`, so blank header fields (elevation, diameter, contractor) and notes printed on another sheet are reported there; only about one entry in ten describes an actual reading problem, yet each one lowers the extraction confidence and keeps most records below the release threshold | Blank or unprinted fields are null and not listed; `unreadable` only for printed values that cannot be read reliably and for values taken from the depth scale or the previous sheet (experiment arm `not-stated`) | none (each entry still costs 0.05, at most 0.2) |
