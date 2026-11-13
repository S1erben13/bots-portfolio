import re
from dataclasses import dataclass
from pathlib import Path

import snowballstemmer
from rank_bm25 import BM25Okapi

STEMMER = snowballstemmer.stemmer("russian")
STOP_WORDS = set("а и в во на по за из от до с со к у о об для как какой какая какие сколько это есть ли вы мы я мне у вас ваш можно что где когда пожалуйста расскажите".split())
PRICE_WORDS = {"стоит", "стоить", "стоимость", "цена", "цены", "цену"}


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[а-яa-z0-9]+", text.lower().replace("ё", "е"))
    return STEMMER.stemWords(["цена" if w in PRICE_WORDS else w for w in words if w not in STOP_WORDS])


def split_text(text: str, size: int = 900, overlap: int = 120) -> list[str]:
    """Ограничиваем размер, предпочитаем границы слов и сохраняем перекрытие."""
    if size < 2 or not 0 <= overlap < size:
        raise ValueError("Нужны size >= 2 и 0 <= overlap < size")
    text = text.strip()
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind(" ", start + size // 2, end)
            if boundary > start:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def read_document(path: Path) -> str:
    if path.suffix.lower() in {".md", ".txt"}:
        return path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    if path.suffix.lower() == ".docx":
        from docx import Document
        doc = Document(path)
        return "\n".join([p.text for p in doc.paragraphs] + [
            " | ".join(cell.text for cell in row.cells)
            for table in doc.tables for row in table.rows
        ])
    raise ValueError("Неподдерживаемый формат")


@dataclass
class Chunk:
    source: str
    text: str


class KnowledgeBase:
    def __init__(self, directory: Path, min_score: float = 0.8, min_coverage: float = 0.5):
        self.chunks = []
        self.min_score, self.min_coverage = min_score, min_coverage
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".md", ".txt", ".pdf", ".docx"}:
                self.chunks.extend(Chunk(path.relative_to(directory).as_posix(), text)
                                   for text in split_text(read_document(path)))
        self.tokens = [tokenize(c.text) for c in self.chunks]
        self.index = BM25Okapi(self.tokens) if any(self.tokens) else None

    def search(self, question: str, limit: int = 3) -> list[Chunk]:
        tokens = tokenize(question)
        if not tokens or self.index is None:
            return []
        scores = self.index.get_scores(tokens)
        found = []
        for i in sorted(range(len(scores)), key=lambda i: scores[i], reverse=True):
            coverage = len(set(tokens) & set(self.tokens[i])) / len(set(tokens))
            if scores[i] >= self.min_score and coverage >= self.min_coverage:
                found.append(self.chunks[i])
            if len(found) == limit:
                break
        return found
