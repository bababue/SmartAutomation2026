"""Discover source files and render every page to an image (plus text layer when present).

All formats end up as page images because many text layers are scrambled, broken OCR,
or missing entirely (vector PDFs with outlined lettering). The text layer is kept as a
hint for the model and for keyword search.
"""

from __future__ import annotations

import html
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path

import docx
import openpyxl
import pymupdf

from . import config

SUPPORTED = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".docx", ".txt", ".xlsx", ".xlsm"}


@dataclass
class SourceDoc:
    doc_id: str
    path: str  # relative to the dataset dir
    kind: str  # pdf | image | docx | text | sheet


@dataclass
class PageImage:
    doc_id: str
    page: int  # 1-based
    image: str  # path relative to PAGES_DIR, full resolution for extraction
    thumb: str  # path relative to PAGES_DIR, small image for classification
    text: str  # text layer ("" if none)
    width: int
    height: int


def doc_id_for(rel: Path) -> str:
    slug = "__".join(rel.with_suffix("").parts)
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", slug)


def discover(dataset_dir: Path | None = None) -> list[SourceDoc]:
    dataset_dir = dataset_dir or config.DATASET_DIR
    docs, used = [], set()
    for path in sorted(dataset_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED:
            rel = path.relative_to(dataset_dir)
            kind = {".pdf": "pdf", ".docx": "docx", ".txt": "text", ".xlsx": "sheet", ".xlsm": "sheet"}.get(
                path.suffix.lower(), "image")
            doc_id = doc_id_for(rel)
            if doc_id in used:  # e.g. 'X.pdf' and 'X.jpg': never share pages, cache and records
                doc_id = f"{doc_id}-{path.suffix.lower().lstrip('.')}"
            used.add(doc_id)
            docs.append(SourceDoc(doc_id, str(rel), kind))
    return docs


def _native_long_edge(page: pymupdf.Page) -> int | None:
    """Pixel size of the largest embedded image (raster sources), else None."""
    images = page.get_images(full=True)
    return max((max(img[2], img[3]) for img in images), default=None)


def _save(page: pymupdf.Page, long_edge: int, out: Path, upscale: bool = True) -> tuple[int, int]:
    if not upscale:
        # Raster sources: page.rect is in points, so derive the scale from native pixels instead.
        native = _native_long_edge(page)
        if native:
            long_edge = min(long_edge, native)
    scale = long_edge / max(page.rect.width, page.rect.height)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    out.write_bytes(pix.tobytes("jpeg", jpg_quality=85))
    return pix.width, pix.height


def _docx_images(path: Path) -> list[tuple[bytes, str]]:
    """Inline images of a DOCX in document order (scanned logs are often pasted as images)."""
    document = docx.Document(str(path))
    images = []
    for shape in document.inline_shapes:
        blips = shape._inline.graphic.graphicData.xpath(".//a:blip/@r:embed")
        for rid in blips:
            part = document.part.related_parts[rid]
            ext = part.partname.ext.lower()
            images.append((part.blob, ext))
    return images


def converted_dir(dataset_dir: Path) -> Path:
    """Where the PDF renderings of a folder's DOCX files go. Each upload gets its own subfolder, so its relative
    paths cannot collide with the dataset's or another upload's."""
    try:
        return config.CONVERTED_DIR / "uploads" / dataset_dir.resolve().relative_to(config.UPLOADS_DIR.resolve())
    except ValueError:
        return config.CONVERTED_DIR


def converted_pdf(doc: SourceDoc, dataset_dir: Path | None = None) -> Path | None:
    """PDF rendering of a DOCX, converted with LibreOffice on first use. None if that is not possible."""
    if doc.kind != "docx":
        return None
    dataset_dir = dataset_dir or config.DATASET_DIR
    src, pdf = dataset_dir / doc.path, converted_dir(dataset_dir) / Path(doc.path).with_suffix(".pdf")
    if not pdf.exists() or pdf.stat().st_mtime < src.stat().st_mtime:
        _convert(src, pdf.parent)
    return pdf if pdf.exists() else None


def _convert(src: Path, out_dir: Path) -> None:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        print(f"  ! {src.name}: LibreOffice (soffice) not found, cannot render the DOCX", file=sys.stderr)
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    # Its own profile, so a LibreOffice instance the user has open does not take over the conversion.
    with tempfile.TemporaryDirectory() as profile:
        try:
            subprocess.run([soffice, f"-env:UserInstallation={Path(profile).as_uri()}", "--headless",
                            "--convert-to", "pdf", "--outdir", str(out_dir), str(src)],
                           check=True, capture_output=True, timeout=180)
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"  ! {src.name}: LibreOffice conversion failed: {exc}", file=sys.stderr)


def text_pdf(path: Path) -> pymupdf.Document:
    """A plain-text log (e.g. a layer data export) laid out on A4 pages, long lines wrapped. PyMuPDF's own text
    reader takes the bytes as Latin-1; these files come as UTF-8 or Windows-1252."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    return _text_pages(text)


def sheet_pdf(path: Path) -> pymupdf.Document:
    """A spreadsheet (e.g. layer or coordinate data) as text pages: each sheet under its name, one row per line,
    cells separated by ' | ', values as computed when the file was last saved."""
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        parts = []
        for sheet in book.worksheets:
            lines = []
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                while cells and not cells[-1].strip():
                    cells.pop()
                if cells:
                    lines.append(" | ".join(cells))
            if lines:
                parts.append(f"[{sheet.title}]\n" + "\n".join(lines))
    finally:
        book.close()
    return _text_pages("\n\n".join(parts))


def _text_pages(text: str) -> pymupdf.Document:
    story = pymupdf.Story(html='<pre style="font-family: monospace; font-size: 9pt; white-space: pre-wrap">'
                               f"{html.escape(text)}</pre>")
    page, buf = pymupdf.paper_rect("a4"), io.BytesIO()
    writer, more = pymupdf.DocumentWriter(buf), True
    while more:
        device = writer.begin_page(page)
        more, _ = story.place(page + (40, 40, -40, -40))
        story.draw(device)
        writer.end_page()
    writer.close()
    return pymupdf.open(stream=buf.getvalue(), filetype="pdf")


def render(doc: SourceDoc, dataset_dir: Path | None = None) -> list[PageImage]:
    dataset_dir = dataset_dir or config.DATASET_DIR
    out_dir = config.PAGES_DIR / doc.doc_id
    manifest = out_dir / "manifest.json"
    src = dataset_dir / doc.path
    converted = converted_pdf(doc, dataset_dir)
    source = str(converted or src)  # rendered from; a new PDF rendering of a DOCX triggers a re-render
    origin = Path(source)
    stat = origin.stat() if origin.exists() else None
    # Replacing a file at the same path, or other render settings, must re-render too.
    fingerprint = f"{stat.st_size}:{stat.st_mtime_ns}:{config.EXTRACT_LONG_EDGE}:{config.THUMB_LONG_EDGE}" if stat else ""
    marker = out_dir / "source.txt"
    if marker.exists():
        recorded = marker.read_text(encoding="utf-8").splitlines() + [""]
        # A marker without a fingerprint line names the source only.
        same_source = recorded[0] == source and (not recorded[1] or recorded[1] == fingerprint)
    else:
        same_source = converted is None
    if manifest.exists() and same_source:
        return [PageImage(**p) for p in json.loads(manifest.read_text(encoding="utf-8"))]
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()  # page count may change

    pages = []
    with ExitStack() as opened:
        if converted:
            d = opened.enter_context(pymupdf.open(converted))
            pdf_pages = [(p, p.get_text()) for p in d]
        elif doc.kind == "docx":
            print(f"  ! {doc.path}: no PDF rendering; using embedded images only, which can split logs laid out "
                  "from several images", file=sys.stderr)
            sources = [opened.enter_context(pymupdf.open(stream=blob, filetype=ext)) for blob, ext in _docx_images(src)]
            pdf_pages = [(d[0], "") for d in sources]
        else:
            d = opened.enter_context(text_pdf(src) if doc.kind == "text" else sheet_pdf(src) if doc.kind == "sheet"
                                     else pymupdf.open(src))
            pdf_pages = [(p, p.get_text() if doc.kind in ("pdf", "text", "sheet") else "") for p in d]

        for i, (page, text) in enumerate(pdf_pages, start=1):
            full, thumb = out_dir / f"{i:04d}.jpg", out_dir / f"{i:04d}.thumb.jpg"
            upscale = doc.kind in ("pdf", "text", "sheet")  # raster sources gain nothing from upscaling
            w, h = _save(page, config.EXTRACT_LONG_EDGE, full, upscale)
            _save(page, config.THUMB_LONG_EDGE, thumb, upscale)
            pages.append(
                PageImage(
                    doc_id=doc.doc_id,
                    page=i,
                    image=str(full.relative_to(config.PAGES_DIR)),
                    thumb=str(thumb.relative_to(config.PAGES_DIR)),
                    text=text.strip(),
                    width=w,
                    height=h,
                )
            )
    manifest.write_text(json.dumps([asdict(p) for p in pages], ensure_ascii=False), encoding="utf-8")
    marker.write_text(f"{source}\n{fingerprint}", encoding="utf-8")
    return pages
