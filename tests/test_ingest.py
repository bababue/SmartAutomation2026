"""DOCX ingestion: a PDF export under CONVERTED_DIR replaces the embedded-image fallback (synthetic files)."""

import docx
import pymupdf

from pipeline import config, ingest


def test_docx_uses_pdf_export_when_present(tmp_path, monkeypatch):
    dataset, pages_dir, converted = tmp_path / "data", tmp_path / "pages", tmp_path / "converted"
    dataset.mkdir()
    monkeypatch.setattr(config, "PAGES_DIR", pages_dir)
    monkeypatch.setattr(config, "CONVERTED_DIR", converted)
    document = docx.Document()
    document.add_paragraph("Borehole log without inline images")
    document.save(dataset / "report.docx")
    doc = ingest.SourceDoc("report", "report.docx", "docx")

    assert ingest.render(doc, dataset) == []  # fallback: no embedded images

    converted.mkdir()
    pdf = pymupdf.open()
    for text in ("Sheet 1 of 2", "Sheet 2 of 2"):
        pdf.new_page().insert_text((72, 72), text)
    pdf.save(converted / "report.pdf")

    pages = ingest.render(doc, dataset)  # new source → rendered again without force
    assert [p.page for p in pages] == [1, 2]
    assert "Sheet 2 of 2" in pages[1].text
    assert ingest.render(doc, dataset) == pages  # cached now
