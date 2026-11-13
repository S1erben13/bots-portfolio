import pytest

from app.knowledge import KnowledgeBase, read_document, split_text, tokenize


def test_chunking():
    text = "абвгдежз" * 80
    chunks = split_text(text, size=100, overlap=20)
    assert all(0 < len(c) <= 100 for c in chunks)
    assert chunks[0][-20:] == chunks[1][:20]
    assert chunks[-1].endswith(text[-20:])
    assert split_text("  ") == []
    assert split_text("Короткий текст") == ["Короткий текст"]
    with pytest.raises(ValueError):
        split_text(text, size=10, overlap=10)


def test_russian_normalization():
    assert tokenize("камерами") == tokenize("камера")
    assert tokenize("Объём") == tokenize("объем")
    assert tokenize("стоит") == tokenize("цены")


@pytest.mark.parametrize("question,source", [
    ("Какая гарантия на монтажные работы?", "06-warranty.md"),
    ("Какая предоплата по договору?", "09-payment.md"),
    ("Сколько стоит выезд для осмотра?", "08-visit.md"),
    ("Сколько стоит установка видеодомофона?", "05-prices.md"),
    ("Какой срок установки четырёх камер?", "07-timing.md"),
])
def test_search(kb_path, question, source):
    hits = KnowledgeBase(kb_path).search(question)
    assert hits
    assert hits[0].source == source


def test_unknown_and_empty(tmp_path, kb_path):
    kb = KnowledgeBase(kb_path)
    assert kb.search("Как приготовить борщ с капустой?") == []
    assert kb.search("   ") == []
    assert KnowledgeBase(tmp_path).search("гарантия") == []


def test_load_txt_docx_pdf(tmp_path):
    from docx import Document
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    (tmp_path / "a.txt").write_text("Текст гарантии", encoding="utf-8-sig")
    doc = Document()
    doc.add_paragraph("Описание домофона")
    doc.add_table(rows=1, cols=1).cell(0, 0).text = "Таблица цен"
    doc.save(tmp_path / "b.docx")
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 100 Td (PDF document) Tj ET")
    page[NameObject("/Contents")] = stream
    writer.write(tmp_path / "c.pdf")
    assert read_document(tmp_path / "a.txt") == "Текст гарантии"
    assert "Таблица цен" in read_document(tmp_path / "b.docx")
    assert "PDF document" in read_document(tmp_path / "c.pdf")
    assert {c.source for c in KnowledgeBase(tmp_path).chunks} == {"a.txt", "b.docx", "c.pdf"}
