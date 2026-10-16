# Prompt backlog (next PROMPT_VERSION)

Changes to the extraction prompt invalidate the whole extraction cache, so they are collected here and
shipped together after the spot checks. Each item: what we saw (generic, no project data), what to change,
and what code currently does as a stopgap.

| # | Observed | Prompt change | Stopgap in code |
|---|---|---|---|
| 1 | On sheet 1 of a multi-sheet log the model reports the sheet's bottom as `final_depth_m` | `final_depth_m` only from an explicit end-of-hole statement or header field; null otherwise | final depth = deepest value stated on any sheet |
| 2 | Weathered rock described as soil ("crumbling silty sand by drilling") gets `primary: unknown` | Map by the described material; put the rock grade into `weathering` | none (stays visible as unknown) |
| 3 | Flint/chert mapped inconsistently (`unknown` vs `quartzite`) | Name a fixed class for flint/chert pieces and keep the term in the description | none |
| 4 | Depths in feet are converted by the model | Report depths as printed plus `depth_unit_raw`; convert in code | logs printed in feet are blocking for a one-time human check |
| 5 | Core logs describe pieces as offsets within a run ("0 - 21 cm, ...") | Ask for the run top used for the offset (or the offset itself) so absolute depths can be computed in code | piece layers are excluded from the text-layer check; ≤ 0.10 m discontinuities are not blocking |
| 6 | A unit's full range is sometimes repeated on the next sheet or run past the sheet end | Repeat the rule "only what is visible on this sheet" in the previous-sheet context block | sheet-break repeats are merged/clipped in assembly |
| 7 | Place estimate is sometimes only the country centroid on one sheet and the town on the next | Ask for the most specific place named anywhere on the sheet (title block, project name) | most precise estimate per hole/document is used |
