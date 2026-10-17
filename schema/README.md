# Borehole schema

`borehole.py` (Pydantic v2) is the canonical, stored model; `borehole.schema.json` is generated from it.

It is **not** meant to be handed to the LLM as is: system fields (`borehole_id`, `lat/lon`, `crs_epsg`, `confidence`, `issues`, `score`, `review_status`, `reviewed_by`) are set by our code. The extraction step uses its own slimmer schema, [`pipeline/extraction_models.py`](../pipeline/extraction_models.py), adapted to the provider's structured-output rules (closed objects, required/nullable convention, no tuple `bbox`).

```bash
../.venv/bin/python -c "import json; from borehole import Borehole; json.dump(Borehole.model_json_schema(), open('borehole.schema.json','w'), indent=2, ensure_ascii=False)"
```

## Mapping from `BoreholeDataFormat.xlsx`

| Excel field | Schema field |
|---|---|
| BoreholeID / BoreholeName | `borehole_id` (internal, stable) / `borehole_name` (as written) |
| LocationName, ProjectID, ProjectName | `location_name`, `project_id`, `project_name` |
| StartDate / EndDate | `start_date` / `end_date` |
| Surface Elevation [mASL] | `surface_elevation_m` (positive up) + `surface_elevation_raw` + `elevation_datum` |
| X / Y / CoordinateSystem | `location.coords_raw/crs_raw` (as written) → `easting/northing` + `crs_epsg` (or `crs_proj`) → `lat/lon` (WGS84) |
| BoreholeDepth / BoreholeLength [m] | `depth_m` / `length_m` (ft converted), plus `inclination_deg` |
| StratigraphicName, EngineeringSoilUnit, GeologicAge | `layers[].stratigraphic_name/engineering_unit/geologic_age` |
| (challenge brief: "age & colour code") | `layers[].stratigraphic_code` (e.g. `qORT`) + `stratigraphic_color` → left column of the profile |
| Lithology/SoilName | `layers[].description_original` + `primary`/`secondary` (DIN 4023 keys) + `uscs` |
| Soil/Rockprofile (color/pattern) | derived from `layers[].primary` (DIN 4023 hatch) and `color` |
| Core, TotalCoreRecovery, RQD | `core_runs[]` (plus SCR) |
| SampleName, SampleDepth | `samples[]` |
| SPT | `spt[]` with blow counts, N and refusal flag |
| Groundwaterlevel | `groundwater[]` (several observations per hole are common) |
| Test results (UCS, CAI, …) | `lab_results[]` with `LabParameter` enum, target units in `LAB_UNITS` |

## Design decisions

- **Raw + normalised side by side.** Every value that needs interpretation (coordinates, SPT, lab values, descriptions) keeps the original string. Lets reviewers verify and lets us re-run normalisation without re-extracting.
- **Provenance everywhere.** `SourceRef` (file, page, optional bbox) on borehole, layer, test and location level → the UI can show the original page next to the extracted profile.
- **Confidence + issues instead of exceptions.** `validate_log()` is an explicit pipeline stage after normalisation; it reports invalid intervals, gaps, overlaps (incl. nested layers), mismatch with final length/depth, empty logs, contradictory groundwater, missing or ambiguous georeference. The record is still stored and flagged for review. Layers keep their source order.
- **Unknown is not false.** Flags like `core`, `dry`, `is_planned` and `investigation_type` default to `None`.
- **Georeferencing is explicit.** `method` says whether a position comes from the log, a separate coordinate table in the same report, or geocoding a place name; `accuracy_m` drives how the map displays it.
- **DIN 4023 lithology keys** in English; the German DIN terms live in `DIN4023_LABEL_DE`. The hatch chart in the dataset is the 2006 edition while the norm text is 2023 (rock symbols changed) – we render after the 2006 chart and say so; no claim of full DIN compliance.
- **Two-column profile** as in the challenge brief and the LGRB portal: stratigraphy (code + colour) left, lithology (DIN 4023 pattern + colour) right.
- **Investigation type.** Reports mix boreholes and test pits; both use the same model.
- **Confidence must not be the LLM's self-assessment alone.** Derive it from checks we can compute: layer continuity, depth vs. stated final depth, text-layer vs. vision agreement, CRS plausibility (distance between transformed coordinates and geocoded place).

## What the dataset demands (generic)

- Inputs: text PDFs (often with column-scrambled text layers), scanned PDFs, vector-only CAD PDFs (no text, no images → must be rendered), JPG/PNG, DOCX with poor embedded OCR, and handwritten field logs.
- Large reports contain only a few log pages → page classification before extraction. Keyword search produces false positives (photo and lab pages); use a cheap vision/LLM classifier.
- File names hint at regions but are not reliable.
- One log can span several pages; one file can contain many boreholes.
- Coordinates: UTM/MGA, national grids (OSGB36, Korean TM, NZ meridional circuits), local grids without EPSG code, lat/lon in unusual notations (degrees + decimal minutes), or missing entirely. UTM zones are often not stated. Coordinates sometimes live only in a summary table elsewhere in the report (proposed vs. actual).
- Several CRS candidates can produce a plausible on-land position in the same country; a check against the geocoded project location usually disambiguates them. If not, keep all `crs_candidates` and show the position as uncertain.
- Lab results may sit in a separate appendix and must be linked to the borehole by name and depth.
- Languages: EN, ES, CA, PT, NO, KO, AZ (plus mixed; the brief also mentions French).
- Number formats: decimal comma, thousands separators in both styles.
- SPT notations: `4,5,5 = 10`, `N=R`, `50/10`, single increments per 75 mm.

## Open points (from review, deliberately deferred)

- Raw text + unit for every numeric value (layer depths, recovery, sample depths), not only coordinates/SPT/lab.
- Stable sample IDs for linking lab appendices to logs (they are linked by hole name and depth).
- Date precision (year/month only, ambiguous day/month order).
- Separate stratigraphic intervals if formation boundaries differ from lithology boundaries (for now layers are split at every boundary).
- Versioned colour palettes for formations and lithology as fallback when the source has no colours.
- Groundwater reference point (ground vs. casing) and elapsed time after drilling.
