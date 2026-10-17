# Prompt backlog (next PROMPT_VERSION)

Changes to the extraction prompt invalidate the whole extraction cache, so they are collected here and
shipped together after the spot checks. Each item: what we saw (generic, no project data), what to change,
and what code currently does as a stopgap.

Each change is measured with `python -m pipeline experiment` (see README → Prompt experiments) before it
ships. Items 1, 2, 3, 6, 7, 8, 9, 10, 12 and 13 are prompt-only; items 4, 5 and 11 also need code (depths in printed units with
conversion in assembly, a run-offset field, defaults for omitted fields) and are tested once that exists.

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
| 10 | The system prompt asks to list everything "not printed or not readable" in `unreadable`, so blank header fields, header text cut off by a punch hole, sample depths and sheet-break tops taken from the depth scale are reported there; only about one entry in ten concerns a layer depth, yet each one lowers the extraction confidence and keeps most records below the release threshold | Blank or unprinted fields are null and not listed; depths a log only draws and tops continued from the previous sheet are not listed; every entry starts with its field (`depth:`, `lithology:`, `name:`, `coordinates:`, `header:`, `tests:`). Arm `unreadable-fields` on the reference pages: 113 entries → 46, of which 17 still count; quality against the reference within the spread of a plain re-extraction (`baseline-rerun`) | entries starting with `header:` or `tests:` do not lower the score; entries without a field name count as before |
| 11 | About a quarter of the extraction output is `null` fields (measured on all cached log pages), and generating the answer takes most of the time per page | Leave out fields that are not printed instead of writing `null`; needs `None` defaults on the nullable fields in `pipeline/extraction_models.py` and a JSON instruction for the `openai` backend that no longer calls every property required. Measured with Haiku 5.5 on the reference pages (arm `haiku-lean`, together with item 10): output only 3 % shorter and no faster, so the model keeps writing the fields; not shipped | none (shorter output means proportionally faster and cheaper pages) |
| 12 | Haiku 5.5 copies the printed field label into raw fields ("Location: …", "Ground level: …", "Date: …", also into the place name); Opus does so rarely | Raw fields carry the value exactly as printed, without the label in front of it | the UI does not repeat a CRS or datum the raw text already names; the labels stay in records and exports |
| 13 | A log without coordinates names a town whose name also exists in another country; Haiku 5.5 took the namesake abroad as country and place, thousands of km off (Opus kept the right country) | `country` only from evidence on the sheet (language, grid, postcode or phone format, standards cited); when a place name fits several countries and nothing decides, `country` null and a coarse `place_precision_km` | a position from the place name alone has confidence 0.3 and always goes to review |

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
