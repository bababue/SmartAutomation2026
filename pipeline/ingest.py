"""Discover source files and render every page to an image (plus text layer when present).

All formats end up as page images because many text layers are scrambled, broken OCR,
or missing entirely (vector PDFs with outlined lettering). The text layer is kept as a
hint for the model and for keyword search.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import docx
import pymupdf

from . import config

SUPPORTED = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".docx"}


@dataclass
class SourceDoc:
    doc_id: str
    path: str  # relative to the dataset dir
    kind: str  # pdf | image | docx


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


def _docx_export(path: Path) -> bool:
    """A PDF saved next to a DOCX of the same name is that DOCX's export, not a document of its own."""
    return path.suffix.lower() == ".pdf" and any(path.with_suffix(ext).exists() for ext in (".docx", ".DOCX"))


def discover(dataset_dir: Path = config.DATASET_DIR) -> list[SourceDoc]:
    docs, used = [], set()
    for path in sorted(dataset_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED and not _docx_export(path):
            rel = path.relative_to(dataset_dir)
            kind = "pdf" if path.suffix.lower() == ".pdf" else "docx" if path.suffix.lower() == ".docx" else "image"
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


def converted_pdf(doc: SourceDoc, dataset_dir: Path = config.DATASET_DIR) -> Path | None:
    """PDF export of a DOCX: under CONVERTED_DIR, or saved next to the DOCX with the same name."""
    if doc.kind != "docx":
        return None
    for path in (config.CONVERTED_DIR / Path(doc.path).with_suffix(".pdf"), (dataset_dir / doc.path).with_suffix(".pdf")):
        if path.exists():
            return path
    return None


def render(doc: SourceDoc, dataset_dir: Path = config.DATASET_DIR, force: bool = False) -> list[PageImage]:
    out_dir = config.PAGES_DIR / doc.doc_id
    manifest = out_dir / "manifest.json"
    src = dataset_dir / doc.path
    converted = converted_pdf(doc, dataset_dir)
    source = str(converted or src)  # rendered from; a new PDF export of a DOCX triggers a re-render
    marker = out_dir / "source.txt"
    same_source = marker.read_text() == source if marker.exists() else converted is None
    if manifest.exists() and same_source and not force:
        return [PageImage(**p) for p in json.loads(manifest.read_text())]
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()  # page count may change

    if converted:
        d = pymupdf.open(converted)
        pdf_pages = [(p, p.get_text()) for p in d]
    elif doc.kind == "docx":
        print(f"  ! {doc.path}: no PDF export in {config.CONVERTED_DIR}; using embedded images only, which can split "
              "logs laid out from several images", file=sys.stderr)
        sources = [pymupdf.open(stream=blob, filetype=ext) for blob, ext in _docx_images(src)]
        pdf_pages = [(d[0], "") for d in sources]
    else:
        d = pymupdf.open(src)
        pdf_pages = [(p, p.get_text() if doc.kind == "pdf" else "") for p in d]

    pages = []
    for i, (page, text) in enumerate(pdf_pages, start=1):
        full, thumb = out_dir / f"{i:04d}.jpg", out_dir / f"{i:04d}.thumb.jpg"
        upscale = doc.kind == "pdf"  # raster sources gain nothing from upscaling
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
    manifest.write_text(json.dumps([asdict(p) for p in pages], ensure_ascii=False))
    marker.write_text(source)
    return pages
