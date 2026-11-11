# services/ai_agent.py — ИИ-аналитик (Этап 7) и разбор программ при импорте (Этап 8)
import json
import logging
import re
from typing import Any, Optional

import aiosqlite

import database as db
from config import AI_API_KEY, AI_PROVIDER, AI_MODEL, AI_BASE_URL
from services import rag, load_cascade

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


async def _chat(system: str, user_content: str, temperature: float = 0.3) -> Optional[str]:
    """Один запрос к OpenAI-совместимому чату. Возвращает текст ответа."""
    try:
        from openai import AsyncOpenAI
        base = AI_BASE_URL or None
        client = AsyncOpenAI(api_key=AI_API_KEY, base_url=base) if base else AsyncOpenAI(api_key=AI_API_KEY)
        resp = await client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user_content}],
            temperature=temperature,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.warning("LLM call failed: %s", e)
        return None


async def _call_llm(context_json: str) -> Optional[dict]:
    text = await _chat(SYSTEM_PROMPT, context_json)
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
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
        base = AI_BASE_URL or None
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


# ---------- Этап 10: LLM выбирает упражнения, каскад считает нагрузку ----------
SELECT_SYSTEM = """Ты — персональный тренер. На вход приходят профиль, дни тренировок
и список разрешённых упражнений для каждого дня. Для каждого дня выбери {per_day}
упражнений СТРОГО из списка этого дня; названия копируй посимвольно.
Порядок: сначала многосуставные, затем изоляция. Внутри дня не повторяй упражнения.
Верни СТРОГО JSON без markdown:
{"days": [{"name": "День 1", "focus": "Верх тела", "exercises": ["Название", "..."]}]}
Правила: не добавляй упражнения вне списка дня; учитывай ограничение {limitation};
названия и фокус дней бери из входных данных. Сделай дни непохожими: не повторяй
упражнения, выбранные для другого дня, если в списке есть альтернатива."""


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


async def _retrieve_knowledge(query: str, k: int = 4) -> list[dict]:
    """Сначала векторный индекс, при его отсутствии — лексический RAG."""
    try:
        from services import vector_store
        if await vector_store.count() > 0:
            hits = await vector_store.search(query, k)
            if hits:
                return hits
    except Exception as e:
        log.warning("Vector search failed: %s", e)
    return rag.retrieve(query, k)


async def build_program_context(user: dict[str, Any]) -> dict[str, Any]:
    """Профиль + история по упражнениям + последние тренировки + каталог."""
    uid = user["telegram_id"]
    workouts = await db.recent_workouts(uid, limit=8)
    health = await db.get_health_latest(uid, limit=7)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT s.exercise_name AS name, MAX(s.weight_kg) AS best_weight,
                      COUNT(*) AS sets, MAX(s.done_at) AS last_date
               FROM sets s JOIN workouts w ON w.id = s.workout_id
               WHERE w.user_id=? AND s.skipped=0
               GROUP BY s.exercise_name ORDER BY last_date DESC LIMIT 40""", (uid,))
        history = [dict(r) for r in await cur.fetchall()]
        for h in history:
            cur2 = await conn.execute(
                """SELECT s.weight_kg, s.reps FROM sets s
                   JOIN workouts w ON w.id = s.workout_id
                   WHERE w.user_id=? AND s.exercise_name=? AND s.skipped=0
                   ORDER BY s.done_at DESC, s.id DESC LIMIT 1""", (uid, h["name"]))
            row = await cur2.fetchone()
            h["last_weight"] = row[0] if row else None
            h["last_reps"] = row[1] if row else None
            cur3 = await conn.execute(
                """SELECT s.weight_kg, s.reps FROM sets s
                   JOIN workouts w ON w.id = s.workout_id
                   WHERE w.user_id=? AND s.exercise_name=? AND s.skipped=0
                   ORDER BY s.weight_kg DESC, s.reps DESC LIMIT 1""", (uid, h["name"]))
            best = await cur3.fetchone()
            h["best_reps"] = best[1] if best else None
        cur = await conn.execute(
            """SELECT id, name, muscle_group, equipment, is_compound, limitation_safe
               FROM exercises ORDER BY muscle_group, name""")
        catalog = [dict(r) for r in await cur.fetchall()]

    profile = {k: user.get(k) for k in (
        "gender", "birth_year", "height_cm", "weight_kg", "level", "goal",
        "days_per_week", "session_minutes", "limitations", "priority_zones")}
    catalog_map = {c["name"]: c for c in catalog}
    prescription = load_cascade.build_prescription(user, history, catalog_map)
    volume = load_cascade.volume_guidance(user)
    goal_words = {
        "strength": "сила 1ПМ низкие повторы отдых",
        "gain_muscle": "гипертрофия объём 8-12 повторов",
        "lose_weight": "похудение многоповторный отдых",
        "general": "общая форма объём",
    }.get(profile.get("goal") or "", "")
    query = (f"{goal_words} {profile.get('level')} объём подходы повторения разгрузка "
             f"восстановление {profile.get('limitations')} {profile.get('priority_zones')}")
    knowledge = await _retrieve_knowledge(query, k=4)
    return {
        "profile": profile,
        "volume": volume,
        "prescription": prescription,
        "knowledge": [{"title": k["title"], "source": k.get("source", ""),
                       "text": k["text"][:900]} for k in knowledge],
        "history": history,
        "recent_workouts": [{"date": w["started_at"][:10], "tonnage": w["total_tonnage"],
                             "duration_min": w["duration_min"], "rpe": w["rpe"]} for w in workouts],
        "health": health,
        "catalog": catalog,
    }


def _split_for(user: dict[str, Any]) -> list[tuple[str, str, list[str]]]:
    """Детерминированный сплит: дни, фокус и группы мышц дня."""
    from services.program_builder import _plan_structure
    level = user.get("level") or "beginner"
    days = max(2, min(6, int(user.get("days_per_week") or 3)))
    structure = _plan_structure(level, days)[:days]
    extra = {
        "legs": ["ноги", "задняя поверхность"],
        "chest_back": ["грудь", "спина"],
        "arms_shoulders": ["бицепс", "трицепс", "плечи"],
    }.get(user.get("priority_zones") or "any", [])
    return [(name, focus, list(dict.fromkeys(extra + groups))) for name, focus, groups in structure]


async def generate_program_spec(user: dict[str, Any]) -> Optional[dict[str, Any]]:
    """LLM выбирает упражнения по дням, а подходы/повторы/веса считает каскад."""
    if not AI_API_KEY:
        return None
    limitation = user.get("limitations") or "none"
    per_day = {"beginner": 5, "intermediate": 6, "advanced": 6}.get(user.get("level"), 5)
    ctx = await build_program_context(user)
    catalog = {c["name"]: c for c in ctx["catalog"]}
    prescription = {p["exercise"]: p for p in ctx["prescription"]}

    def is_safe(c: dict) -> bool:
        if limitation == "none":
            return True
        return limitation in [x.strip() for x in (c.get("limitation_safe") or "").split(",")]

    plan = _split_for(user)
    options: list[list[str]] = []
    for _, _, groups in plan:
        names = [c["name"] for c in ctx["catalog"] if c["muscle_group"] in groups and is_safe(c)]
        names.sort(key=lambda n: (not bool(catalog[n]["is_compound"]), n))
        options.append(names[:14])

    payload = {
        "profile": ctx["profile"],
        "volume": ctx["volume"],
        "knowledge": ctx["knowledge"],
        "prescription": ctx["prescription"][:15],
        "days": [{"name": n, "focus": f, "options": o} for (n, f, _), o in zip(plan, options)],
    }
    system = SELECT_SYSTEM.replace("{per_day}", str(per_day)).replace("{limitation}", limitation)
    text = await _chat(system, json.dumps(payload, ensure_ascii=False, default=str), temperature=0.4)
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        log.warning("LLM program JSON is invalid")
        return None

    llm_days = data.get("days") if isinstance(data.get("days"), list) else []
    reps_min, reps_max = (int(x) for x in ctx["volume"]["reps"])
    acc_min, acc_max = {"strength": (8, 12), "gain_muscle": (10, 15),
                        "lose_weight": (12, 15), "general": (10, 15)}.get(
                            user.get("goal") or "general", (8, 12))
    rest = int(sum(ctx["volume"]["rest_seconds"]) / 2)
    picked_total = 0
    clean_days = []

    for i, (day_name, focus, _groups) in enumerate(plan):
        allowed = options[i]
        picks: list[str] = []
        if i < len(llm_days) and isinstance(llm_days[i], dict):
            picks = [str(x).strip() for x in (llm_days[i].get("exercises") or [])]
        chosen: list[str] = []
        for name in picks:
            if name in allowed and name not in chosen:
                chosen.append(name)
        picked_total += len(chosen)
        for name in allowed:                     # добиваем день до нормы из разрешённого списка
            if len(chosen) >= per_day:
                break
            if name not in chosen:
                chosen.append(name)
        chosen = chosen[:per_day]

        exercises = []
        for name in chosen:
            c = catalog[name]
            compound = bool(c["is_compound"])
            if compound:
                sets, rmin, rmax, rst = 4, reps_min, reps_max, rest
            else:
                # изоляция и кор работают в более высоком диапазоне повторов
                sets, rmin, rmax, rst = 3, acc_min, acc_max, 60
            exercises.append({
                "id": c["id"], "name": name,
                "sets": sets, "reps_min": rmin, "reps_max": rmax, "rest": rst,
                "target_weight": prescription.get(name, {}).get("target_weight"),
                "is_compound": compound,
            })
        clean_days.append({"name": day_name[:40], "focus": focus[:40], "exercises": exercises})

    if picked_total < len(plan):
        log.warning("LLM picked only %s exercises — fallback to local generator", picked_total)
        return None
    return {"name": f"ИИ-программа ({user.get('goal')})", "days": clean_days}
