# Feedback from the challenge owner (Herrenknecht), 16 Oct 2026

Notes from a conversation with the challenge owner at the event (machine transcript, cleaned up and
summarised; not a verbatim record). No project data.

## What they said

| # | Statement | Priority |
|---|---|---|
| 1 | **Coordinates are the most important part.** Documents use many systems (UTM, geographic, local grids); they must be converted and stored so the records can be shown and found in the database. That is the first step to anything usable. | must |
| 2 | "OCR" in the brief was only a keyword. The real task is information extraction; a vision LLM guided towards the needed fields is fine. | confirms approach |
| 3 | **Data quality over quantity.** Better fewer records with a high hit rate than reading everything and getting half of it wrong. A human in the loop for uncertain records (especially handwriting) is the right call. | confirms approach |
| 4 | Once the map works: understand what a log says about the ground at that spot. | next |
| 5 | **Interpolate between neighbouring boreholes** along a planned tunnel route (holes 10–20 m apart): if granite is at 50 m in one hole and at 20 m in the next, estimate how it develops in between, with a stated uncertainty. Look at holes together, not one by one. | idea |
| 6 | **Matching by geotechnical parameters:** for a new project (e.g. a tunnel on some coast), find earlier projects with similar ground conditions. | idea |
| 7 | Data protection is **not a requirement for the challenge.** The company uses Microsoft (Azure) with a contractual data-residency assurance; sensitive data needs customer consent. Self-hosted models are a long-term option for them. Mistral is welcome if available, but in their experience somewhat weaker. | relaxes a constraint |
| 8 | **Model comparison** (which model, what price, what result) on their test data would be interesting. | idea |
| 9 | They want something showable quickly that can then be extended; statistics and anomalies across the collected data are of interest. Build your own ideas. | open |

## Where we stand

| # | Status in this repository |
|---|---|
| 1 | Covered and the area with the most work: CRS resolution to EPSG, notation variants (DMS, decimal minutes, hemisphere letters, unsigned westings, packed values, a zone printed beside the values), a second reading of printed coordinates from an enlarged crop, document-level CRS, plausibility against an offline gazetteer, outliers within a document, ambiguous or inferred CRS sent to review, reviewer CRS corrections for a whole document, and local grids fitted from reviewer-set positions. Holes that would only share a place centre are left for a person to place. |
| 2 | Matches: vision LLM with a slim schema, IDs/CRS/WGS84/confidence set in code. |
| 3 | Matches: release score from computed evidence (not model self-assessment), blocking findings, auto-release only with positive evidence, export of released records only. The hit rate against human-checked records (`pipeline eval`) and a release audit (`pipeline eval audit`: a random sample of automatic releases checked by a person) are shown in the README and on the statistics page. |
| 4 | Covered: detail view with a two-column profile (DIN 4023 lithology, stratigraphy in ICS colours), layers and tests next to the source page; 3D columns on the map from zoom 15. |
| 5 | Covered: a tunnel alignment (drawn or imported as GeoJSON) picks the holes in a corridor; the longitudinal section joins rock head and groundwater between neighbours and estimates layers where the material sequences match, with an uncertainty band that widens with distance. Boundary correlations remain estimates. |
| 6 | Covered: ground search (rock head and groundwater depth, metres of a material in a depth range, presets such as "Clay over rock") and a list of similar boreholes from other documents in the detail view, each with a one-line reason. |
| 7 | README's data-security section stays as a design principle; the provider is a configuration switch. No further work needed for the challenge. |
| 8 | Done: [model-comparison.md](model-comparison.md) and the "Quality & models" tab of the statistics page. Opus 5.5 and Haiku 5.5 measured against the human-checked records (quality, tokens, cost and time per page), prompt versions side by side, and an earlier agreement benchmark of seven configurations including a local model. Opus stays the corpus model; Haiku costs about 1/30 and is weaker on boundary precision, groundwater and lab values. |
| 9 | Statistics covered: lithology distribution, final depths, release funnel, recurring findings per document, coordinate systems, and recorded model calls. Anomalies across the collection (outliers against neighbouring holes) are open, listed in FEATURES.md → Feature ideas. |
