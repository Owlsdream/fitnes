# services/vector_store.py — векторный индекс базы знаний (эмбеддинги + косинус)
"""Локальная векторная база без внешних сервисов.

Эмбеддинги считает модель через OpenAI-совместимый endpoint (по умолчанию
Ollama + bge-m3), векторы хранятся в SQLite (таблица rag_vectors), поиск —
косинусное сходство через numpy. При желании этот модуль заменяется на
ChromaDB/Pinecone без изменения остального кода: интерфейс index/search/count.
"""
import os
from typing import Any

import aiosqlite
import numpy as np

import database as db
from config import EMBED_API_KEY, EMBED_BASE_URL, EMBED_MODEL

BATCH = 16
SCHEMA = """
CREATE TABLE IF NOT EXISTS rag_vectors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT,
    title      TEXT,
    text       TEXT NOT NULL,
    embedding  BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


def _client():
    from openai import AsyncOpenAI
    base = EMBED_BASE_URL or None
    return AsyncOpenAI(api_key=EMBED_API_KEY or "ollama", base_url=base) if base \
        else AsyncOpenAI(api_key=EMBED_API_KEY)


async def embed_texts(texts: list[str]) -> np.ndarray:
    """Считает эмбеддинги батчами. Возвращает матрицу (n, dim)."""
    client = _client()
    vectors: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        resp = await client.embeddings.create(model=EMBED_MODEL, input=texts[i:i + BATCH])
        vectors.extend(item.embedding for item in resp.data)
    return np.asarray(vectors, dtype=np.float32)


async def count() -> int:
    try:
        async with aiosqlite.connect(db.DB_PATH) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM rag_vectors")
            return (await cur.fetchone())[0]
    except Exception:
        return 0


async def index_chunks(chunks: list[dict[str, Any]], replace: bool = True) -> int:
    """Пересчитывает и сохраняет векторы для чанков базы знаний."""
    if not chunks:
        return 0
    vectors = await embed_texts([c["text"] for c in chunks])
    dim = int(vectors.shape[1])
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(SCHEMA)
        if replace:
            await conn.execute("DELETE FROM rag_vectors")
        await conn.executemany(
            "INSERT INTO rag_vectors (source, title, text, embedding, dim) VALUES (?, ?, ?, ?, ?)",
            [(c.get("source", ""), c.get("title", ""), c["text"],
              np.asarray(vec, dtype=np.float32).tobytes(), dim)
             for c, vec in zip(chunks, vectors)])
        await conn.commit()
    return len(chunks)


async def search(query: str, k: int = 4) -> list[dict[str, Any]]:
    """Топ-k фрагментов по косинусному сходству. Пустой список, если индекс пуст."""
    if not query.strip():
        return []
    async with aiosqlite.connect(db.DB_PATH) as conn:
        try:
            cur = await conn.execute("SELECT source, title, text, embedding FROM rag_vectors")
            rows = await cur.fetchall()
        except Exception:
            return []
    if not rows:
        return []
    matrix = np.stack([np.frombuffer(r[3], dtype=np.float32) for r in rows])
    qvec = (await embed_texts([query]))[0]
    norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(qvec) + 1e-9
    sims = (matrix @ qvec) / norms
    order = np.argsort(-sims)[:max(k * 4, k)]

    # разнообразие: не больше двух фрагментов из одного файла
    picked: list[int] = []
    per_source: dict[str, int] = {}
    for i in order:
        src = rows[i][0]
        if per_source.get(src, 0) >= 2:
            continue
        picked.append(int(i))
        per_source[src] = per_source.get(src, 0) + 1
        if len(picked) >= k:
            break
    return [{"source": rows[i][0], "title": rows[i][1], "text": rows[i][2],
             "score": round(float(sims[i]), 4)} for i in picked]
