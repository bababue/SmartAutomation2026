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


def test_discover_skips_docx_export_and_keeps_ids_unique(tmp_path):
    for name in ("report.docx", "report.pdf", "plan.pdf", "plan.jpg"):
        (tmp_path / name).write_bytes(b"x")
    docs = {d.path: d.doc_id for d in ingest.discover(tmp_path)}
    assert "report.pdf" not in docs  # the DOCX's own export
    assert docs["report.docx"] == "report"
    assert sorted(docs[n] for n in ("plan.jpg", "plan.pdf")) == ["plan", "plan-pdf"]


def test_export_next_to_docx_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr(config, "CONVERTED_DIR", tmp_path / "converted")
    docx.Document().save(tmp_path / "r.docx")
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "Log sheet")
    pdf.save(tmp_path / "r.pdf")
    pages = ingest.render(ingest.SourceDoc("r", "r.docx", "docx"), tmp_path)
    assert len(pages) == 1 and "Log sheet" in pages[0].text


def test_replaced_export_is_rendered_again(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr(config, "CONVERTED_DIR", tmp_path / "converted")
    docx.Document().save(tmp_path / "r.docx")
    def export(n):
        pdf = pymupdf.open()
        for i in range(n):
            pdf.new_page().insert_text((72, 72), f"sheet {i + 1}")
        pdf.save(tmp_path / "r.pdf")
    doc = ingest.SourceDoc("r", "r.docx", "docx")
    export(1)
    assert len(ingest.render(doc, tmp_path)) == 1
    export(2)  # corrected export saved over the old one
    os.utime(tmp_path / "r.pdf", ns=(1, 2_000_000_000_000_000_000))
    assert len(ingest.render(doc, tmp_path)) == 2
