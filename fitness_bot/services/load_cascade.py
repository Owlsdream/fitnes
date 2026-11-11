# services/load_cascade.py — каскадный расчёт нагрузок: объём → интенсивность → вес
"""Детерминированная часть генерации программы.

LLM выбирает структуру и упражнения, а рабочие веса считает этот модуль:
так модель не выдумывает цифры, а опирается на историю и правила прогрессии.
"""
from typing import Any, Optional

# целевая интенсивность (% от 1ПМ) и диапазон повторов по цели
INTENSITY = {
    "strength": (0.82, 0.88),
    "gain_muscle": (0.68, 0.78),
    "lose_weight": (0.55, 0.65),
    "general": (0.65, 0.75),
}
REPS = {
    "strength": (4, 6),
    "gain_muscle": (8, 12),
    "lose_weight": (12, 15),
    "general": (8, 12),
}
REST = {
    "strength": (120, 180),
    "gain_muscle": (60, 120),
    "lose_weight": (45, 75),
    "general": (60, 120),
}
WEEKLY_SETS = {
    "beginner": (8, 12),
    "intermediate": (12, 18),
    "advanced": (16, 22),
}
STEP_UPPER = 2.5
STEP_LOWER = 5.0
LOWER_GROUPS = {"ноги", "задняя поверхность", "икры"}


def estimate_1rm(weight: float, reps: int) -> float:
    """Формула Эпли."""
    return weight * (1 + reps / 30)


def round_plate(value: float) -> float:
    """Округление до шага блинов 2,5 кг."""
    return round(value / 2.5) * 2.5


def volume_guidance(user: dict[str, Any]) -> dict[str, Any]:
    """Недельный объём и параметры подходов под уровень и цель."""
    level = user.get("level") or "beginner"
    goal = user.get("goal") or "general"
    lo, hi = WEEKLY_SETS.get(level, (10, 14))
    if goal == "gain_muscle":
        lo, hi = lo + 2, hi + 2
    elif goal == "lose_weight":
        lo, hi = max(6, lo - 2), max(10, hi - 2)
    sets_lo, sets_hi = (3, 4) if level == "beginner" else (3, 5)
    return {
        "weekly_sets_per_muscle": f"{lo}-{hi}",
        "sets_per_exercise": f"{sets_lo}-{sets_hi}",
        "reps": list(REPS.get(goal, (8, 12))),
        "intensity_pct": [round(INTENSITY.get(goal, (0.65, 0.75))[0] * 100),
                          round(INTENSITY.get(goal, (0.65, 0.75))[1] * 100)],
        "rest_seconds": list(REST.get(goal, (60, 120))),
    }


def build_prescription(user: dict[str, Any], history: list[dict[str, Any]],
                       catalog: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Считает целевой вес по последней рабочей сессии (а не по рекорду)."""
    goal = user.get("goal") or "general"
    # берём нижнюю границу интенсивности: рабочий вес должен оставлять 2-3 повтора в запасе
    pct = INTENSITY.get(goal, (0.65, 0.75))[0]
    reps_min, reps_max = REPS.get(goal, (8, 12))
    out: list[dict[str, Any]] = []
    for h in history:
        # база — лучший e1RM за последние 6 месяцев, а не последняя (возможно, лёгкая) сессия
        e1rm = h.get("recent_e1rm")
        if not e1rm:
            base_w = h.get("best_weight") or 0
            base_r = max(1, min(30, int(h.get("best_reps") or 5)))
            if base_w <= 0:
                continue
            e1rm = estimate_1rm(float(base_w), base_r)
        e1rm = float(e1rm)
        target = round_plate(e1rm * pct)
        # прогрессия: если прошлая сессия закрыта на верхней границе повторов
        if (h.get("last_reps") or 0) >= reps_max and target > 0:
            info = catalog.get(h["name"], {})
            lower = (info.get("muscle_group") in LOWER_GROUPS) and bool(info.get("is_compound"))
            target += STEP_LOWER if lower else STEP_UPPER
        out.append({
            "exercise": h["name"],
            "e1rm": round(e1rm, 1),
            "target_weight": round_plate(target),
            "reps_min": reps_min,
            "reps_max": reps_max,
        })
    # самые «тяжёлые» упражнения — первыми, чтобы попали в промпт при обрезке
    out.sort(key=lambda x: -x["e1rm"])
    return out


def pick_target(prescription: list[dict[str, Any]]) -> dict[str, float]:
    return {p["exercise"]: p["target_weight"] for p in prescription}
