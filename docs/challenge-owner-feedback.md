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
| 1 | Covered and the area with the most work: CRS resolution to EPSG, notation variants (DMS, decimal minutes, hemisphere letters, unsigned westings), document-level CRS, plausibility against the geocoded place, ambiguous or inferred CRS sent to review, reviewer CRS corrections. Keep it the top priority. |
| 2 | Matches: vision LLM with a slim schema, IDs/CRS/WGS84/confidence set in code. |
| 3 | Matches: release score from computed evidence (not model self-assessment), blocking findings, auto-release only with positive evidence, export of released records only. Worth showing the eval numbers (`pipeline eval`) as proof of the hit rate. |
| 4 | Partly: two-column profile with DIN 4023 lithology and stratigraphy next to the source page. |
| 5 | The tunnel route profile places boreholes by station along the alignment and estimates material layers between them. Longer spans are marked as less certain; boundary correlations remain estimates. |
| 6 | Not started. Possible: a per-hole ground profile vector (lithology shares per depth band, rock vs. soil, depth to rock, groundwater) and a "similar boreholes" list in the detail view. |
| 7 | README's data-security section stays as a design principle; the provider is a configuration switch. No further work needed for the challenge. |
| 8 | Done: [model-comparison.md](model-comparison.md). Opus 5.5 and Haiku 5.5 measured against the human-checked records (quality, tokens, cost and time per page), plus an earlier agreement benchmark of nine configurations including a local model. Opus stays the corpus model; Haiku costs about 1/30 and is weaker on boundary precision, groundwater and lab values. |
| 9 | Open: a statistics view (lithology distribution, depths, release rates, recurring findings per document). |
