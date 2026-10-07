# services/ai_agent.py — ИИ-аналитик (Этап 7) и разбор программ при импорте (Этап 8)
import json
import logging
import re
from typing import Any, Optional

import aiosqlite

import database as db
from config import AI_API_KEY, AI_PROVIDER, AI_MODEL

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — спортивный аналитик. Получаешь JSON с данными тренировок,
профилем и метриками здоровья пользователя. Верни СТРОГО JSON без markdown:
{
 "summary": "короткий вывод на русском",
 "adjustments": [
   {"exercise": "точное название из базы", "sets_delta": int, "reps_delta": int,
    "weight_delta_kg": float, "reason": "почему"}
 ],
 "confidence": 0.0-1.0
}
Правила: используй только упражнения из присланного списка; изменение объёма <= 10%;
учитывай ограничения пользователя; при плохом восстановлении (HRV вниз, сон <6ч) — снижай нагрузку."""


async def build_context(user_id: int) -> dict[str, Any]:
    """Собирает JSON-контекст: профиль, последние тренировки, тренды, здоровье."""
    user = await db.get_user(user_id)
    workouts = await db.recent_workouts(user_id, limit=10)
    health = await db.get_health_latest(user_id, limit=14)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT s.exercise_name, SUM(s.weight_kg*s.reps) v, MAX(s.weight_kg) w
               FROM sets s JOIN workouts ww ON ww.id=s.workout_id
               WHERE ww.user_id=? AND s.skipped=0 GROUP BY s.exercise_name""", (user_id,))
        trends = [{"exercise": r[0], "volume": r[1], "top_weight": r[2]} for r in await cur.fetchall()]
    return {"profile": user, "workouts": workouts, "trends": trends, "health": health}


async def ask_for_user(user_id: int, kind: str = "weekly") -> Optional[str]:
    """Полный цикл: контекст → запрос → двойная валидация → текст рекомендации."""
    if not AI_API_KEY:
        return None
    ctx = await build_context(user_id)
    result = await _call_llm(json.dumps(ctx, ensure_ascii=False, default=str))
    if not result:
        return None
    ok, why = await validate_recommendation(user_id, result)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO ai_recommendations (user_id, kind, summary, payload_json, confidence, valid)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, kind, result.get("summary", ""), json.dumps(result, ensure_ascii=False),
             float(result.get("confidence", 0)), int(ok)))
        await conn.commit()
    low_conf = float(result.get("confidence", 1)) < 0.6
    header = "💡 Предложение (требует подтверждения):" if (low_conf or not ok) else "🤖 Рекомендация:"
    body = "\n".join(f"• {a['exercise']}: подходы {a.get('sets_delta', 0):+d}, "
                     f"повторы {a.get('reps_delta', 0):+d}, вес {a.get('weight_delta_kg', 0):+g} кг"
                     for a in result.get("adjustments", []))
    note = "" if ok else f"\n⚠️ Валидация: {why}"
    return f"{header}\n{result.get('summary', '')}\n{body}{note}"


async def _call_llm(context_json: str) -> Optional[dict]:
    try:
        from openai import AsyncOpenAI
        base = "https://api.moonshot.cn/v1" if AI_PROVIDER == "kimi" else None
        client = AsyncOpenAI(api_key=AI_API_KEY, base_url=base) if base else AsyncOpenAI(api_key=AI_API_KEY)
        resp = await client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": context_json}],
            temperature=0.3,
        )
        text = resp.choices[0].message.content.strip()
        # Вырезаем JSON из возможных ```...``` оберток
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        log.warning("LLM call failed: %s", e)
        return None


async def validate_recommendation(user_id: int, rec: dict) -> tuple[bool, str]:
    """Двойная валидация: существование упражнений, ограничения, лимит объёма ≤10%."""
    user = await db.get_user(user_id)
    limitation = (user or {}).get("limitations") or "none"
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cur = await conn.execute("SELECT name, limitation_safe FROM exercises")
        known = {r[0]: (r[1] or "") for r in await cur.fetchall()}
    for a in rec.get("adjustments", []):
        name = a.get("exercise", "")
        if name not in known:
            return False, f"упражнение «{name}» не найдено в базе"
        if limitation != "none" and limitation not in [s.strip() for s in known[name].split(",")] \
                and _adds_load(a):
            return False, f"«{name}» конфликтует с ограничением ({limitation})"
        # Лимит изменения объёма ≤10%
        if abs(int(a.get("sets_delta", 0))) > 1 or abs(float(a.get("weight_delta_kg", 0))) > 10:
            return False, "превышен лимит изменения объёма (>10%)"
    return True, "ok"


def _adds_load(a: dict) -> bool:
    return int(a.get("sets_delta", 0)) > 0 or float(a.get("weight_delta_kg", 0)) > 0


# ---------- Этап 8: распознавание программы ----------
async def parse_program_text(text: str) -> Optional[dict]:
    """Просит LLM разобрать произвольный текст программы в структурированный JSON."""
    if not AI_API_KEY:
        return None
    prompt = ("Разбери тренировочную программу и верни JSON: "
              '{"name": str, "days": [{"name": str, "exercises": '
              '[{"name": str, "sets": int, "reps": int, "rest": int, "confidence": 0..1}]}]}. '
              "Сопоставь названия с распространёнными упражнениями, confidence — уверенность.\n\n"
              + text[:4000])
    try:
        from openai import AsyncOpenAI
        base = "https://api.moonshot.cn/v1" if AI_PROVIDER == "kimi" else None
        client = AsyncOpenAI(api_key=AI_API_KEY, base_url=base) if base else AsyncOpenAI(api_key=AI_API_KEY)
        resp = await client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "user", "content": prompt}], temperature=0.1)
        raw = resp.choices[0].message.content.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else None
        if data and data.get("days"):
            return data
    except Exception as e:
        log.warning("Program parse failed: %s", e)
    return None


def fallback_parse(text: str) -> Optional[dict]:
    """Локальный разбор без ИИ: строки вида 'День A: Приседания 3x10, Жим лёжа 4x8'."""
    days = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m_day = re.match(r"(день\s*\w+|day\s*\w+)\s*[:\-]\s*(.+)", line, re.I)
        name, rest = (m_day.group(1), m_day.group(2)) if m_day else ("Программа", line)
        exs = []
        for part in re.split(r"[,\n;]+", rest):
            m_ex = re.match(r"(.+?)\s*(\d+)\s*[xх×]\s*(\d+)", part.strip(), re.I)
            if m_ex:
                exs.append({"name": m_ex.group(1).strip().capitalize(),
                            "sets": int(m_ex.group(2)), "reps": int(m_ex.group(3)),
                            "rest": 90, "confidence": 0.8})
        if exs:
            days.append({"name": name.title(), "focus": "", "exercises": exs})
    return {"name": "Импортированная программа", "days": days} if days else None
