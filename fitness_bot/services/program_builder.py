# services/program_builder.py — генерация программы под уровень, цель и ограничения
import random
from typing import Any

import aiosqlite

import database as db

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

    structure = _plan_structure(level, days)

    program_id = await db.create_program(user["telegram_id"], f"Программа ({goal})")
    reps_min, reps_max = REPS_BY_GOAL.get(goal, (8, 12))
    rest = REST_BY_GOAL.get(goal, 90)

    lines: list[str] = []
    for order, (day_name, focus, groups) in enumerate(structure, start=1):
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

        # Сначала база по группам
        for g in groups:
            ex = _pick(by_group.get(g, []), used, limitation)
            if ex:
                chosen.append(ex)
                used.add(ex["name"])
            if len(chosen) >= base_count:
                break
        # Добиваем изоляцией до нужного числа
        all_pool = [e for e in pool if not e["is_compound"]]
        while len(chosen) < base_count:
            ex = _pick(all_pool, used, limitation)
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

        text_parts = []
        for idx, ex in enumerate(chosen, start=1):
            is_comp = bool(ex["is_compound"])
            if goal == "strength" and is_comp:
                s, rmin, rmax = 5, 4, 6          # силовая 5×5 на базе
            elif goal == "gain_muscle" and is_comp:
                s, rmin, rmax = 4, 8, 12         # +1 подход к базе
            elif goal == "lose_weight":
                s, rmin, rmax = 3, 12, 15
            else:
                s, rmin, rmax = 3, reps_min, reps_max
            if extra_isolation and not is_comp and idx > len(chosen) - extra_isolation - 1:
                s = 3
            await db.add_day_exercise(day_id, ex["id"], None, s, rmin, rmax,
                                      rest, idx, is_comp)
            text_parts.append(f"{idx}. {ex['name']} — {s}×{rmin}-{rmax}")
        lines.append(f"📅 {day_name} ({focus}):\n" + "\n".join(text_parts))

    return f"✅ Программа сгенерирована (уровень: {level}, цель: {goal}):\n\n" + "\n\n".join(lines)
