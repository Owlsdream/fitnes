#!/usr/bin/env python
"""Загрузка книг (PDF/EPUB/DOCX/TXT/MD) в базу знаний RAG.

Примеры:
    python scripts/ingest_books.py                       # всё из data/books/
    python scripts/ingest_books.py "C:/Books/Supertraining.pdf"
    python scripts/ingest_books.py --in "C:/Books" --out data/knowledge/books

Скрипт режет книги на фрагменты ~900 символов и пишет markdown-файлы
с заголовками '## ', которые подхватывает services/rag.py.
После загрузки перезапустите бота (RAG кэширует базу знаний в памяти).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import html as html_lib
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IN = ROOT / "data" / "books"
DEFAULT_OUT = ROOT / "data" / "knowledge" / "books"

# локально установленный PyMuPDF (pip install --target .vendor pymupdf)
_VENDOR = ROOT / ".vendor"
if _VENDOR.exists():
    sys.path.insert(0, str(_VENDOR))


# ---------- извлечение текста ----------
def extract_pdf(path: Path) -> list[tuple[str, str]]:
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError:
            raise SystemExit(
                "PyMuPDF не установлен. Выполни:\n"
                "  ..\\venv\\Scripts\\python.exe -m pip install --target .vendor pymupdf\n"
                "или: pip install pymupdf")
    doc = pymupdf.open(str(path))
    pages = []
    for i, page in enumerate(doc, start=1):
        pages.append((f"стр. {i}", page.get_text("text")))
    doc.close()
    return pages


def _html_to_text(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html_lib.unescape(text)


def extract_epub(path: Path) -> list[tuple[str, str]]:
    """Без внешних зависимостей: читаем xhtml-файлы из zip-контейнера."""
    chapters = []
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist()
                       if n.lower().endswith((".xhtml", ".html", ".htm")))
        for i, name in enumerate(names, start=1):
            chapters.append((f"глава {i}", _html_to_text(z.read(name))))
    return chapters


def extract_docx(path: Path) -> list[tuple[str, str]]:
    """Без python-docx: вытаскиваем текстовые узлы из word/document.xml."""
    from xml.etree import ElementTree as ET
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paragraphs = []
    for p in root.iter(f"{ns}p"):
        text = "".join(t.text or "" for t in p.iter(f"{ns}t"))
        if text.strip():
            paragraphs.append(text)
    return [("документ", "\n".join(paragraphs))]


def extract_plain(path: Path) -> list[tuple[str, str]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return [("текст", raw.decode(enc))]
        except UnicodeDecodeError:
            continue
    return [("текст", raw.decode("utf-8", "replace"))]


EXTRACTORS = {
    ".pdf": extract_pdf,
    ".epub": extract_epub,
    ".docx": extract_docx,
    ".txt": extract_plain,
    ".md": extract_plain,
}


# ---------- очистка и нарезка ----------
def clean_text(text: str) -> str:
    text = text.replace("\u00ad", "")                     # мягкие переносы
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)     # перенос слова
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # убираем одинокие номера страниц
    text = re.sub(r"(?m)^\s*\d{1,4}\s*$", "", text)
    return text.strip()


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text)
    parts = re.split(r"(?<=[.!?;:])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def chunk_block(text: str, size: int, overlap: int) -> list[str]:
    """Собирает предложения в куски ~size символов с перехлёстом."""
    sentences = split_sentences(clean_text(text))
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for sentence in sentences:
        if length + len(sentence) + 1 > size and current:
            chunks.append(" ".join(current))
            # перехлёст: оставляем хвост предыдущего куска
            tail, tail_len = [], 0
            for s in reversed(current):
                if tail_len + len(s) > overlap:
                    break
                tail.insert(0, s)
                tail_len += len(s) + 1
            current, length = tail, tail_len
        current.append(sentence)
        length += len(sentence) + 1
    if current:
        chunks.append(" ".join(current))
    return [c for c in chunks if len(c) >= 200]


def chunk_book(pages: list[tuple[str, str]], size: int, overlap: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for label, page_text in pages:
        for i, chunk in enumerate(chunk_block(page_text, size, overlap), start=1):
            suffix = f" ({i})" if i > 1 else ""
            out.append((label + suffix, chunk))
    return out


def write_markdown(path: Path, title: str, chunks: list[tuple[str, str]]) -> None:
    seen: set[str] = set()
    blocks = [f"# {title}"]
    for label, text in chunks:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        blocks.append(f"## {title} — {label}\n{text}")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def slugify(name: str) -> str:
    slug = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "-", name).strip("-")
    return slug[:70] or "book"


def ingest_file(src: Path, out_dir: Path, size: int, overlap: int) -> int:
    extractor = EXTRACTORS.get(src.suffix.lower())
    if not extractor:
        print(f"  ! пропуск {src.name}: формат {src.suffix} не поддержан (нужен OCR/конвертация)")
        return 0
    print(f"  → {src.name} ({src.stat().st_size / 1024 / 1024:.1f} МБ)")
    pages = extractor(src)
    empty = sum(1 for _, text in pages if len(text.strip()) < 30)
    if pages and empty == len(pages):
        print("  ! похоже, это скан без текстового слоя — нужен OCR (например, Tesseract)")
        return 0
    if empty:
        print(f"  ~ пустых страниц: {empty} из {len(pages)}")
    chunks = chunk_book(pages, size, overlap)
    if not chunks:
        print("  ! не удалось выделить текст")
        return 0
    target = out_dir / f"{slugify(src.stem)}.md"
    write_markdown(target, src.stem, chunks)
    print(f"  ✓ {target.name}: {len(chunks)} фрагментов, {target.stat().st_size / 1024:.0f} КБ")
    return len(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description="Загрузка книг в RAG-базу знаний")
    parser.add_argument("files", nargs="*", help="конкретные файлы (иначе — все из --in)")
    parser.add_argument("--in", dest="src_dir", default=str(DEFAULT_IN), help="папка с книгами")
    parser.add_argument("--out", dest="out_dir", default=str(DEFAULT_OUT), help="папка для markdown")
    parser.add_argument("--chunk", type=int, default=900, help="размер фрагмента, символов")
    parser.add_argument("--overlap", type=int, default=120, help="перехлёст, символов")
    parser.add_argument("--no-index", action="store_true",
                        help="не пересчитывать векторный индекс после загрузки")
    args = parser.parse_args()

    src_dir = Path(args.src_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.files:
        files = [Path(f) for f in args.files]
    else:
        if not src_dir.exists():
            print(f"Нет папки {src_dir}. Создай её и положи туда книги.")
            return 1
        files = sorted(p for p in src_dir.iterdir()
                       if p.is_file() and p.suffix.lower() in EXTRACTORS)

    if not files:
        print(f"В {src_dir} нет файлов pdf/epub/docx/txt/md.")
        return 1

    total = 0
    for src in files:
        if not src.exists():
            print(f"  ! нет файла {src}")
            continue
        total += ingest_file(src, out_dir, args.chunk, args.overlap)
    print(f"\nГотово: {len(files)} файлов, {total} фрагментов в {out_dir}")

    if total and not args.no_index:
        try:
            sys.path.insert(0, str(ROOT))
            from services import rag, vector_store
            rag._load.cache_clear()
            chunks, _, _ = rag._load()
            indexed = asyncio.run(vector_store.index_chunks(chunks))
            print(f"Векторный индекс обновлён: {indexed} фрагментов "
                  f"(модель {vector_store.EMBED_MODEL}).")
        except Exception as e:  # нет Ollama/модели — не критично
            print(f"! векторный индекс не обновлён: {e}")
            print("  Запусти вручную: ..\\venv\\Scripts\\python.exe scripts\\index_rag.py")

    print("Перезапусти бота, чтобы RAG увидел новые материалы.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
