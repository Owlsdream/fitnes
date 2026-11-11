# services/rag.py — мини-RAG по базе знаний (BM25-подобный поиск без внешних зависимостей)
import math
import re
from functools import lru_cache
from pathlib import Path

KB_DIR = Path(__file__).resolve().parent.parent / "data" / "knowledge"
WORD_RE = re.compile(r"[а-яёa-z0-9]{3,}")


def _tokens(text: str) -> list[str]:
    return WORD_RE.findall(text.lower())


@lru_cache(maxsize=1)
def _load() -> tuple[list[dict], dict[str, int], int]:
    """Читает markdown-файлы и режет их по заголовкам '## '."""
    chunks: list[dict] = []
    for path in sorted(KB_DIR.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        source = path.relative_to(KB_DIR).as_posix()
        parts = re.split(r"(?m)^##\s+", text)
        for part in parts[1:]:
            lines = part.splitlines()
            title = lines[0].strip()
            body = "\n".join(lines[1:]).strip()
            chunks.append({"title": title, "text": f"{title}. {body}".strip(), "source": source})
    df: dict[str, int] = {}
    for c in chunks:
        for w in set(_tokens(c["text"])):
            df[w] = df.get(w, 0) + 1
    for c in chunks:
        toks = _tokens(c["text"])
        tf: dict[str, int] = {}
        for w in toks:
            tf[w] = tf.get(w, 0) + 1
        c["tf"] = tf
        c["len"] = len(toks) or 1
    return chunks, df, len(chunks)


def retrieve(query: str, k: int = 3) -> list[dict]:
    """Возвращает k наиболее релевантных фрагментов базы знаний."""
    chunks, df, n = _load()
    q = set(_tokens(query))
    scored: list[tuple[float, dict]] = []
    for c in chunks:
        score = 0.0
        for w in q:
            if w in c["tf"]:
                idf = math.log((n + 1) / (df.get(w, 0) + 1)) + 1
                score += (c["tf"][w] / c["len"]) * idf
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return [dict(c, score=round(s, 4)) for s, c in scored[:k]]
