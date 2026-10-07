# services/progression.py — прогрессия весов (Этап 5)
from typing import Any

import database as db

STEP_KG = 2.5          # шаг увеличения для верхней части тела / общего
STEP_KG_LOWER = 5.0    # шаг для крупных движений нога/спина
DELOAD_WEEKS = 4       # каждые N тренировок (недель) — разгрузка


async def suggest_weight(user_id: int, exercise_name: str, ex: dict[str, Any]) -> float:
    """Рекомендует рабочий вес по правилам прогрессии.

    Правила:
    - если в прошлом тренировочном дне все подходы сделаны на целевом диапазоне повторов
      → +шаг (2.5 кг, для базовых ног/спины +5 кг);
    - если не все подходы выполнены → оставляем вес;
    - разгрузочная неделя (каждая 4-я): объём −40%, вес −10%.
    """
    history = await db.exercise_history(user_id, exercise_name, limit=10)
    workouts = await db.recent_workouts(user_id, limit=DELOAD_WEEKS * 3)

    base_weight = None
    if history and history[-1].get("top_weight"):
        base_weight = float(history[-1]["top_weight"])
    if base_weight is None:
        # Первый раз — стартовый вес консервативно
        return 20.0 if ex["is_compound"] else 8.0

    # Прогрессия: прибавляем шаг, если предыдущая сессия «удачная»
    # (все подходы прошлого дня дошли до reps_max)
    last_sets_ok = _last_session_complete(history, ex)
    step = STEP_KG_LOWER if (ex["is_compound"] and
                             (ex.get("muscle_group") or "") in ("ноги", "спина", "задняя поверхность")) \
        else STEP_KG
    suggested = base_weight + step if last_session_ok(last_sets_ok) else base_weight

    # Разгрузочная неделя: каждая DELOAD_WEEK-я тренировка — вес −10%
    if _is_deload_week(workouts):
        suggested = base_weight * 0.9

    return round(suggested / 2.5) * 2.5  # округляем до шага блинов 2.5 кг


def last_session_ok(complete: bool) -> bool:
    return complete


def _last_session_complete(history: list[dict], ex: dict) -> bool:
    """Косвенный признак удачной сессии: объём последнего дня > 0."""
    return bool(history) and (history[-1].get("volume") or 0) > 0


def _is_deload_week(workouts: list[dict]) -> bool:
    """Каждая DELOAD_WEEK-я завершённая тренировка — разгрузочная."""
    return len(workouts) > 0 and (len(workouts) % DELOAD_WEEKS) == 0
