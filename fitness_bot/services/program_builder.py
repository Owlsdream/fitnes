# services/program_builder.py — генерация программы под уровень, цель и ограничения
import logging
import random
from datetime import date, timedelta
from typing import Any

import aiosqlite

import database as db
from services import load_cascade

log = logging.getLogger(__name__)

# Три основных движения: каждое ровно один раз в неделю
MAIN_LIFTS = ["Приседания со штангой", "Жим лёжа", "Становая тяга"]
MAIN_DAY = {
    "Приседания со штангой": ("Низ 1 · присед", "Ноги",
                              ["ноги", "задняя поверхность", "квадрицепс", "икры", "кор"]),
    "Жим лёжа": ("Верх 1 · жим", "Верх тела",
                 ["грудь", "спина", "плечи", "трицепс", "бицепс"]),
    "Становая тяга": ("Низ 2 · тяга", "Спина и задняя поверхность",
                      ["спина", "задняя поверхность", "бицепс", "поясница", "кор"]),
}
CORE_NAME = "Планка"
GOAL_MONTHS = 2
MONTHLY_STEP = {"Приседания со штангой": 5.0, "Становая тяга": 5.0, "Жим лёжа": 2.5}

# Диапазоны повторов по цели для базовых упражнений
REPS_BY_GOAL = {
    "strength": (4, 6),        # сила: мало повторов на базе
    "gain_muscle": (8, 12),    # масса: гипертрофийный диапазон
    "lose_weight": (12, 15),   # похудение: много повторов
    "general": (8, 12),
}

REST_BY_GOAL = {
    "strength": 150,
    "gain_muscle": 90,
    "lose_weight": 60,
    "general": 90,
}


def _safe_for_limitations(ex: dict, limitation: str) -> bool:
    """Проверяет, что упражнение безопасно при ограничении пользователя."""
    if not limitation or limitation == "none":
        return True
    safe = (ex.get("limitation_safe") or "")
    return limitation in [s.strip() for s in safe.split(",")]


def _pick(pool: list[dict], used: set[str], limitation: str) -> dict | None:
    """Берёт из пула неиспользованное безопасное упражнение."""
    cands = [e for e in pool if e["name"] not in used and _safe_for_limitations(e, limitation)]
    return random.choice(cands) if cands else None


def _assigned_mains(day_index: int, days: int) -> list[str]:
    """Какое основное движение ставим в этот день (каждое — раз в неделю)."""
    if days >= 3:
        return [MAIN_LIFTS[day_index]] if day_index < len(MAIN_LIFTS) else []
    if days == 2:
        return [MAIN_LIFTS[0], MAIN_LIFTS[1]] if day_index == 0 else [MAIN_LIFTS[2]]
    return []


def _apply_main_lifts(chosen: list[dict], pool: list[dict], day_index: int, days: int,
                      limitation: str) -> list[dict]:
    """Убирает случайные основные движения и ставит назначенное на этот день."""
    assigned = _assigned_mains(day_index, days)
    by_name = {e["name"]: e for e in pool}
    chosen = [e for e in chosen if e["name"] not in MAIN_LIFTS]
    for main in assigned:
        ex = by_name.get(main)
        if ex and _safe_for_limitations(ex, limitation):
            chosen.insert(0, ex)
    return chosen


def _ensure_core(chosen: list[dict], pool: list[dict], limitation: str) -> list[dict]:
    """Кор есть в каждой тренировке (если позволяет ограничение)."""
    if any(e["name"] == CORE_NAME for e in chosen):
        return chosen
    core = next((e for e in pool if e["name"] == CORE_NAME), None)
    if core and _safe_for_limitations(core, limitation):
        chosen.append(core)
    return chosen


def _trim_day(chosen: list[dict], base_count: int) -> list[dict]:
    """Оставляет base_count упражнений плюс кор в конце."""
    core = [e for e in chosen if e["name"] == CORE_NAME]
    others = [e for e in chosen if e["name"] != CORE_NAME][:base_count]
    return others + core


def _fmt_day(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def _block_plan_text(goals: list[dict], start: date, end: date) -> str:
    lines = [f"🗓 План на 2 месяца: {_fmt_day(start)} — {_fmt_day(end)}", ""]
    if goals:
        lines.append("🎯 Целевые показатели:")
        for g in goals:
            lines.append(f"• {g['exercise']}: {g['start_weight']:g} → {g['target_weight']:g} кг")
    lines += [
        "",
        "Схема блока:",
        "• Недели 1–3 — накопление: 4–6 повторов, 3–5 подходов, вес растёт по самочувствию.",
        "• Неделя 4 — разгрузка: минус 40% объёма, вес −10%.",
        "• Недели 5–7 — интенсификация: рабочий вес ближе к цели, 3–5 повторов.",
        "• Неделя 8 — разгрузка и проверка показателей.",
        "",
        "Каждую неделю: присед, жим лёжа и становая — по одному разу, кор в каждой тренировке.",
        "Вес на тренировке подбирается по последнему результату: закрыл верх диапазона — добавляй шаг.",
    ]
    return "\n".join(lines)


async def load_exercise_pool() -> list[dict[str, Any]]:
    """Загружает весь справочник упражнений из БД."""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM exercises")
        return [dict(r) for r in await cur.fetchall()]


def _plan_structure(level: str, days: int) -> list[tuple[str, str, list[str]]]:
    """Возвращает список дней: (название, фокус, группы мышц дня)."""
    if level == "beginner":
        # Фулбоди A/B, чередование; новичку достаточно 3 тренировок в неделю
        full = [("День A", "Фулбоди A", ["ноги", "грудь", "спина", "плечи", "кор"]),
                ("День B", "Фулбоди B", ["задняя поверхность", "спина", "грудь", "бицепс", "трицепс"])]
        n = min(days, 3)
        return [full[i % 2] for i in range(n)]
    if level == "intermediate":
        upper = ["грудь", "спина", "плечи", "бицепс", "трицепс"]
        lower = ["ноги", "задняя поверхность", "икры", "кор"]
        n = 4 if days >= 4 else max(2, days)
        out = []
        for i in range(n):
            is_up = i % 2 == 0
            out.append((f"Верх {i // 2 + 1}" if is_up else f"Низ {i // 2 + 1}",
                        "Верх тела" if is_up else "Низ тела",
                        upper if is_up else lower))
        return out
    # advanced — сплит push/pull/legs
    push = ["грудь", "плечи", "трицепс"]
    pull = ["спина", "бицепс", "задняя поверхность"]
    legs = ["ноги", "задняя поверхность", "икры", "кор"]
    split = [("Push (жим)", "Толкающие", push), ("Pull (тяга)", "Тяговые", pull),
             ("Legs (ноги)", "Ноги", legs)]
    n = min(max(days, 3), 5)
    return [split[i % 3] for i in range(n)]


async def generate_program(user: dict[str, Any]) -> str:
    """Старая схема генерации: сплит и подбор упражнений кодом.

    Отличие — целевые веса считаются по прогрессу (e1RM, последний вес, шаг прогрессии).
    """
    return await _generate_local(user)


async def _save_spec(user: dict[str, Any], spec: dict[str, Any], source: str = "ai") -> str:
    """Сохраняет программу, собранную LLM (уже провалидированную)."""
    program_id = await db.create_program(user["telegram_id"], spec["name"], source)
    lines: list[str] = []
    for order, day in enumerate(spec["days"], start=1):
        day_id = await db.add_day(program_id, day["name"], order, day["focus"])
        parts: list[str] = []
        for idx, ex in enumerate(day["exercises"], start=1):
            await db.add_day_exercise(day_id, ex["id"], None, ex["sets"], ex["reps_min"],
                                      ex["reps_max"], ex["rest"], idx, ex["is_compound"],
                                      ex.get("target_weight"))
            tw = f" @ {ex['target_weight']:g} кг" if ex.get("target_weight") else ""
            parts.append(f"{idx}. {ex['name']} — {ex['sets']}×{ex['reps_min']}-{ex['reps_max']}{tw}")
        lines.append(f"📅 {day['name']} ({day['focus']}):\n" + "\n".join(parts))
    head = "🤖 Программа собрана ИИ" if source == "ai" else "✅ Программа сгенерирована"
    return f"{head} (уровень: {user['level']}, цель: {user['goal']}):\n\n" + "\n\n".join(lines)


async def _generate_local(user: dict[str, Any]) -> str:
    """Создаёт программу в БД по профилю. Возвращает человекочитаемое описание."""
    level = user["level"]
    goal = user["goal"]
    days = user["days_per_week"] or 3
    limitation = user.get("limitations") or "none"
    zone = user.get("priority_zones") or "any"

    pool = await load_exercise_pool()
    by_group: dict[str, list[dict]] = {}
    for e in pool:
        by_group.setdefault(e["muscle_group"], []).append(e)

    # целевые веса по прогрессу истории (каскад: e1RM → %1RM → шаг прогрессии)
    history: list[dict] = []
    progress: dict[str, float] = {}
    try:
        history = await db.exercise_progress(user["telegram_id"])
        pool_map = {e["name"]: e for e in pool}
        progress = {p["exercise"]: p["target_weight"]
                    for p in load_cascade.build_prescription(user, history, pool_map)}
    except Exception as e:
        log.warning("Progress calculation failed: %s", e)

    structure = _plan_structure(level, days)

    program_id = await db.create_program(user["telegram_id"], f"Программа ({goal})")

    # цели на 2 месяца по основным движениям
    goals: list[dict] = []
    for name in MAIN_LIFTS:
        start_w = progress.get(name)
        if not start_w:
            continue
        target = round((start_w + MONTHLY_STEP[name] * GOAL_MONTHS) / 2.5) * 2.5
        goals.append({"exercise": name, "start_weight": start_w, "target_weight": target})
    if goals:
        await db.save_goals(program_id, goals)

    block_start = date.today()
    block_end = block_start + timedelta(days=8 * 7 - 1)
    await db.delete_plans(user["telegram_id"], "block")
    await db.save_plan(user["telegram_id"], "block", block_start.isoformat(),
                       block_end.isoformat(), _block_plan_text(goals, block_start, block_end))

    reps_min, reps_max = REPS_BY_GOAL.get(goal, (8, 12))
    rest = REST_BY_GOAL.get(goal, 90)

    lines: list[str] = []
    for order, (day_name, focus, groups) in enumerate(structure, start=1):
        # первые три дня посвящены основным движениям — название и группы под них
        if days >= 3 and order <= len(MAIN_LIFTS):
            day_name, focus, groups = MAIN_DAY[MAIN_LIFTS[order - 1]]
        day_id = await db.add_day(program_id, day_name, order, focus)
        used: set[str] = set()
        chosen: list[dict] = []

        # Количество упражнений по уровню
        base_count = {"beginner": 5, "intermediate": 6, "advanced": 7}[level]
        # Приоритетная зона добавляет группу мышц
        if zone == "legs":
            groups = ["ноги", "задняя поверхность"] + groups
        elif zone == "chest_back":
            groups = ["грудь", "спина"] + groups
        elif zone == "arms_shoulders":
            groups = ["бицепс", "трицепс", "плечи"] + groups

        # Сначала база по группам: по возможности многосуставные
        for g in groups:
            group_pool = by_group.get(g, [])
            compounds = [e for e in group_pool if e["is_compound"]]
            ex = _pick(compounds or group_pool, used, limitation)
            if ex:
                chosen.append(ex)
                used.add(ex["name"])
            if len(chosen) >= base_count:
                break
        # Добиваем изоляцией, но только из групп этого дня (не тащим махи в день ног)
        filler = [e for e in pool
                  if not e["is_compound"] and e["muscle_group"] in groups]
        if goal != "lose_weight":
            filler = [e for e in filler if e["muscle_group"] != "кардио"]
        while len(chosen) < base_count:
            ex = _pick(filler, used, limitation)
            if not ex:
                break
            chosen.append(ex)
            used.add(ex["name"])

        # Корректировка под цель
        extra_isolation = 0
        if goal == "lose_weight":
            extra_isolation = 2
            cardio = _pick(by_group.get("кардио", []), used, limitation)
            if cardio:
                chosen.append(cardio)
        elif goal == "strength":
            pass  # диапазоны уже заданы ниже

        # основные движения: по одному разу в неделю, кор — в каждой тренировке
        chosen = _apply_main_lifts(chosen, pool, order - 1, days, limitation)
        chosen = _ensure_core(chosen, pool, limitation)
        chosen = _trim_day(chosen, base_count)

        text_parts = []
        for idx, ex in enumerate(chosen, start=1):
            is_comp = bool(ex["is_compound"])
            if ex["name"] == CORE_NAME:
                s, rmin, rmax = 3, 30, 45          # планка — секунды
            elif not is_comp:
                # изоляция работает в более высоком диапазоне повторов
                s, rmin, rmax = (3, 8, 12) if goal in ("strength", "gain_muscle") else (3, 12, 15)
            elif goal == "strength" and is_comp:
                s, rmin, rmax = 5, 4, 6          # силовая 5×5 на базе
            elif goal == "gain_muscle" and is_comp:
                s, rmin, rmax = 4, 8, 12         # +1 подход к базе
            elif goal == "lose_weight":
                s, rmin, rmax = 3, 12, 15
            else:
                s, rmin, rmax = 3, reps_min, reps_max
            if extra_isolation and not is_comp and idx > len(chosen) - extra_isolation - 1:
                s = 3
            target = progress.get(ex["name"])
            await db.add_day_exercise(day_id, ex["id"], None, s, rmin, rmax,
                                      rest, idx, is_comp, target)
            tw = f" @ {target:g} кг" if target else ""
            text_parts.append(f"{idx}. {ex['name']} — {s}×{rmin}-{rmax}{tw}")
        lines.append(f"📅 {day_name} ({focus}):\n" + "\n".join(text_parts))

    head = f"✅ Программа на 2 месяца (уровень: {level}, цель: {goal}):\n"
    if goals:
        head += "🎯 Цели: " + ", ".join(
            f"{g['exercise']} {g['start_weight']:g}→{g['target_weight']:g} кг" for g in goals) + "\n"
    return head + "\n" + "\n\n".join(lines)


async def schedule_program(user: dict[str, Any], program_id: int, weeks: int = 8) -> int:
    """Создаёт календарный план на `weeks` недель для готовой программы."""
    days = await db.get_days(program_id)
    if not days:
        return 0
    return await db.create_schedule(
        user["telegram_id"], program_id,
        [d["id"] for d in days],
        int(user.get("days_per_week") or 3),
        date.today(), weeks)
