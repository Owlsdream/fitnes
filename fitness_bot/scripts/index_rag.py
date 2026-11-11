#!/usr/bin/env python
"""Пересчитывает векторный индекс RAG по всем markdown-файлам базы знаний.

Запускать после добавления/правки материалов и после ingest_books.py:
    ..\\venv\\Scripts\\python.exe scripts\\index_rag.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import rag, vector_store  # noqa: E402


async def main() -> None:
    rag._load.cache_clear()
    chunks, _, total = rag._load()
    print(f"Фрагментов в базе знаний: {total}")
    if not chunks:
        print("База знаний пуста — нечего индексировать.")
        return
    count = await vector_store.index_chunks(chunks)
    print(f"Проиндексировано {count} фрагментов моделью {vector_store.EMBED_MODEL}.")


if __name__ == "__main__":
    asyncio.run(main())
