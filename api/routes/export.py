"""Downloads of the records as CSV (one row per layer) and as a BoreholeDataFormat workbook."""

from __future__ import annotations

import csv
import io
import sqlite3
from collections.abc import Iterator
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import Response, StreamingResponse

from pipeline import export, store
from schema.borehole import Borehole

from ..db import DB, RELEASED, SOURCE_FILE

router = APIRouter()

ExportStatus = Literal["all", "released"]

CSV_HEADER = [
    "BoreholeID", "BoreholeName", "SourceFile", "SourcePages", "ProjectName", "LocationName",
    "Latitude_WGS84", "Longitude_WGS84",
    "OriginalCoordinates", "OriginalCRS", "SurfaceElevation_m", "ElevationDatum", "BoreholeDepth_m",
    "LayerTop_m", "LayerBottom_m", "Lithology", "Description", "StratigraphicName", "StratigraphicCode",
    "GeologicAge", "Colour", "ReviewStatus", "ReviewedBy", "Score",
]


@router.get("/api/export.csv")
def export_csv(status: ExportStatus = "released"):
    """Flat export in the spirit of BoreholeDataFormat.xlsx: one row per layer. Released records only
    by default, so unreviewed extractions do not leak into downstream design work. Streamed record by
    record from its own connection, so memory stays flat however large the database is."""
    return StreamingResponse(_csv_rows(status), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=boreholes.csv"})


@router.get("/api/export.xlsx")
def export_xlsx(con: DB, status: ExportStatus = "released"):
    """Workbook in Herrenknecht's BoreholeDataFormat (metadata, log data, samples, core runs, SPT,
    groundwater, test results). Released records only by default, like the CSV."""
    content = export.workbook([(Borehole.model_validate_json(r["data"]), r["source_file"])
                               for r in _export_rows(con, status)])
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=boreholes.xlsx"})


def _export_rows(con: sqlite3.Connection, status: ExportStatus) -> sqlite3.Cursor:
    where = "" if status == "all" else f"WHERE {RELEASED}"
    return con.execute(f"SELECT data, {SOURCE_FILE} FROM boreholes {where} ORDER BY id")


def _csv_rows(status: ExportStatus) -> Iterator[str]:
    buf = io.StringIO()
    w = csv.writer(buf)

    def flush() -> str:
        text = buf.getvalue()
        buf.seek(0)
        buf.truncate()
        return text

    w.writerow(CSV_HEADER)
    yield flush()
    con = store.connect()
    try:
        for row in _export_rows(con, status):
            _csv_record(w, row)
            yield flush()
    finally:
        con.close()


def _csv_record(w, row: sqlite3.Row) -> None:
    bh = Borehole.model_validate_json(row["data"])
    loc = bh.location
    original_crs = f"EPSG:{loc.crs_epsg}" if loc.crs_epsg else loc.crs_proj or loc.crs_raw
    source_file = row["source_file"]
    pages = {s.page for s in bh.sources if s.page is not None and (source_file is None or s.file == source_file)}
    source_pages = ";".join(str(p) for p in sorted(pages)) or None
    for layer in bh.layers or [None]:
        w.writerow([
            bh.borehole_id, bh.borehole_name, source_file, source_pages, bh.project_name, bh.location_name,
            loc.lat, loc.lon,
            loc.coords_raw, original_crs, bh.surface_elevation_m, bh.elevation_datum, bh.depth_m,
            *((layer.top_m, layer.bottom_m, layer.primary.value, layer.description_en or layer.description_original,
               layer.stratigraphic_name, layer.stratigraphic_code, layer.geologic_age, layer.color)
              if layer else [None] * 8),
            bh.review_status.value, bh.reviewed_by, bh.score,
        ])
