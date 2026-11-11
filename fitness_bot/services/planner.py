# services/planner.py — каскад горизонтов: месяц (программа) → неделя → день
"""Планы строятся сверху вниз: день опирается на неделю, неделя — на программу.

Ключевое правило: если опоры нет или она непригодна, сначала достраивается она,
и только потом нижний горизонт. Модель не должна «догадываться» о содержимом.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

import database as db
from services.ai_agent import _chat, build_program_context

log = logging.getLogger(__name__)

MIN_PLAN_TEXT = 300
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

WEEK_SYSTEM = """Ты — персональный тренер. Составь план на тренировочную неделю.
Опирайся на месячную программу, последние тренировки и показатели восстановления.
Календарь недели уже задан отдельно: НЕ переписывай даты и дни недели заново
и не добавляй тренировки в другие дни.
Ответ — только текст на русском, 400–1200 знаков, без markdown-таблиц:
1) фокус недели; 2) интенсивность по запланированным дням; 3) что меняем при плохом
восстановлении; 4) одна строка про сон и питание."""

DAY_SYSTEM = """Ты — персональный тренер. Составь короткую вводную к тренировочному дню.
Ответ — только текст 250–700 знаков: разминка (3–4 пункта) и блок «на что обратить
внимание сегодня» (техника, запас повторов, сон, восстановление).
Не перечисляй упражнения, подходы и веса — они уже заданы отдельно, не указывай дату
и не пиши, что тренировка уже прошла."""


# ---------- проверки ----------
def plan_usable(plan: Optional[dict[str, Any]]) -> bool:
    """План существует и содержит осмысленный текст (а не заглушку)."""
    if not plan or not plan.get("id"):
        return False
    return len((plan.get("markdown_text") or "").strip()) >= MIN_PLAN_TEXT


def week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def _fmt(day: date) -> str:
    return day.strftime("%d.%m")


def _relative(day: date, today: date) -> str:
    delta = (today - day).days
    if delta <= 0:
        return "сегодня"
    if delta == 1:
        return "вчера"
    return f"{delta} дн. назад"


def _clean(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
    text = text.replace("**", "")
    text = re.sub(r"(?m)^#+\s*", "", text)
    return text or None


# ---------- контекст ----------
def _recent(ctx: dict[str, Any], today: date) -> list[dict[str, Any]]:
    out = []
    for w in ctx.get("recent_workouts", []):
        try:
            d = datetime.fromisoformat(w["date"]).date()
        except ValueError:
            continue
        out.append({**w, "label": _relative(d, today)})
    return out


async def _week_context(user: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    uid = user["telegram_id"]
    ctx = await build_program_context(user)
    program = await db.get_current_program(uid)
    program_days: list[dict[str, Any]] = []
    if program:
        for d in await db.get_days(program["id"]):
            exs = await db.get_day_exercises(d["id"])
            program_days.append({
                "name": d["day_name"], "focus": d["focus"],
                "exercises": [e["real_name"] or e["custom_name"] for e in exs],
            })
    sched = [s for s in await db.get_schedule(uid)
             if start <= date.fromisoformat(s["planned_date"]) <= end]
    return {
        "profile": ctx["profile"],
        "week": {"start": start.isoformat(), "end": end.isoformat()},
        "program_days": program_days,
        "scheduled": [{
            "date": s["planned_date"],
            "weekday": WEEKDAYS[date.fromisoformat(s["planned_date"]).weekday()],
            "day_name": s["day_name"], "focus": s["focus"], "status": s["status"],
        } for s in sched],
        "recent_workouts": _recent(ctx, date.today()),
        "health": ctx["health"][:7],
        "prescription": ctx["prescription"][:12],
        "knowledge": ctx["knowledge"][:2],
    }


async def _day_context(user: dict[str, Any], day: date, week_plan: dict[str, Any],
                        sched: Optional[dict[str, Any]]) -> dict[str, Any]:
    ctx = await build_program_context(user)
    workout: dict[str, Any] = {}
    if sched:
        exs = await db.get_day_exercises(sched["day_id"])
        workout = {
            "day_name": sched["day_name"], "focus": sched["focus"],
            "exercises": [e["real_name"] or e["custom_name"] for e in exs],
        }
    return {
        "profile": ctx["profile"],
        "date": day.isoformat(),
        "weekday": WEEKDAYS[day.weekday()],
        "week_plan": (week_plan.get("markdown_text") or "")[:1500],
        "workout": workout,
        "recent_workouts": _recent(ctx, date.today()),
        "health": ctx["health"][:7],
    }


# ---------- неделя ----------
def _fallback_week(sched: list[dict[str, Any]], start: date, end: date) -> str:
    lines = [f"🗓 Неделя {_fmt(start)}–{_fmt(end)}"]
    if sched:
        lines.append("План по дням:")
        for s in sched:
            d = date.fromisoformat(s["planned_date"])
            lines.append(f"• {WEEKDAYS[d.weekday()]} {_fmt(d)} — {s['day_name']} ({s['focus']})")
    else:
        lines.append("На эту неделю запланированных тренировок нет — восстановление и лёгкая активность.")
    lines += [
        "",
        "Фокус недели: техника и стабильность, без форсирования веса.",
        "Если сон меньше 6 часов или пульс покоя выше обычного на 10% — убери 1–2 подхода "
        "и не повышай рабочий вес.",
        "Питание: белок в каждом приёме пищи, вода и сон не меньше 7 часов.",
    ]
    return "\n".join(lines)


def _calendar_block(sched: list[dict[str, Any]]) -> str:
    """Календарь недели строит код, а не модель — даты не могут «поплыть»."""
    if not sched:
        return "Календарь недели: тренировок не запланировано."
    parts = []
    for s in sched:
        d = date.fromisoformat(s["planned_date"])
        parts.append(f"{WEEKDAYS[d.weekday()]} {_fmt(d)} — {s['day_name']} ({s['focus']})")
    return "Календарь недели: " + "; ".join(parts) + "."


async def ensure_week_plan(user: dict[str, Any], on_date: Optional[date] = None) -> Optional[dict[str, Any]]:
    """План недели; при отсутствии/непригодности — генерирует."""
    target = on_date or date.today()
    start, end = week_bounds(target)
    existing = await db.get_plan(user["telegram_id"], "week", start.isoformat())
    if plan_usable(existing):
        return existing

    sched = [s for s in await db.get_schedule(user["telegram_id"])
             if start <= date.fromisoformat(s["planned_date"]) <= end]
    calendar = _calendar_block(sched)
    text = None
    try:
        ctx = await _week_context(user, start, end)
        generated = _clean(await _chat(WEEK_SYSTEM, json.dumps(ctx, ensure_ascii=False, default=str), 0.4))
        if generated:
            text = f"{calendar}\n\n{generated}"
    except Exception as e:
        log.warning("Week plan generation failed: %s", e)
    if not text or len(text) < MIN_PLAN_TEXT:
        text = _fallback_week(sched, start, end)
    plan_id = await db.save_plan(user["telegram_id"], "week", start.isoformat(), end.isoformat(), text)
    return await db.get_plan(user["telegram_id"], "week", start.isoformat()) if plan_id else None


# ---------- день ----------
def _rest_text(day: date) -> str:
    return (f"🗓 {WEEKDAYS[day.weekday()]}, {_fmt(day)} — день отдыха.\n\n"
            "Восстановление — часть плана, а не пропуск тренировки.\n"
            "• Лёгкая активность 20–30 минут: прогулка, подвижность, растяжка.\n"
            "• Сон 7–9 часов, отбой в одно и то же время.\n"
            "• Белок в каждом приёме пищи, вода по потребности.\n"
            "• Если осталась сильная болезненность после прошлой тренировки — "
            "сократи активность и не добавляй нагрузку.\n"
            "Если появилась острая боль или симптомы болезни — не тренируйся и обратись к врачу.")


async def _exercise_block(user: dict[str, Any], sched: dict[str, Any]) -> tuple[str, list[str]]:
    """Упражнения и веса задаёт код — модель их не переписывает."""
    exs = await db.get_day_exercises(sched["day_id"])
    ctx = await build_program_context(user)
    prescription = {p["exercise"]: p["target_weight"] for p in ctx["prescription"]}
    lines, names = [], []
    for i, e in enumerate(exs, start=1):
        name = e["real_name"] or e["custom_name"]
        names.append(name)
        weight = e.get("target_weight") or prescription.get(name)
        tw = f" @ {weight:g} кг" if weight else ""
        lines.append(f"{i}. {name} — {e['sets']}×{e['reps_min']}–{e['reps_max']}{tw}, "
                     f"отдых {e['rest_seconds']} с")
    return "\n".join(lines), names


def _clean_note(note: Optional[str], names: list[str]) -> Optional[str]:
    """Оставляет из ответа модели только разминку/заметку, без списка упражнений."""
    if not note:
        return None
    # сначала ищем смысловой блок «на что обратить внимание»
    m = re.search(r"(?i)на что обратить внимание|заметк\w*", note)
    if m:
        note = note[m.start():]
    else:
        idx = note.lower().find("упражнения")
        if idx > 0:
            note = note[:idx]
    # выбрасываем строки, где модель повторяет упражнения или подходы
    kept = [ln for ln in note.splitlines()
            if not any(n.lower() in ln.lower() for n in names)]
    note = "\n".join(kept).strip()
    if len(note) < 80:
        return None
    low = note.lower()
    if any(n.lower() in low for n in names):
        return None
    return note


async def _fallback_day(user: dict[str, Any], day: date, sched: Optional[dict[str, Any]]) -> str:
    if not sched:
        return _rest_text(day)
    header = f"🗓 {WEEKDAYS[day.weekday()]}, {_fmt(day)} — {sched['day_name']} ({sched['focus']})"
    block, _ = await _exercise_block(user, sched)
    return (f"{header}\n\n"
            "Разминка: 5–10 минут, суставная разминка и 1–2 лёгких подхода.\n\n"
            f"Упражнения:\n{block}\n\n"
            "Работай с запасом 2–3 повтора. Если самочувствие плохое — убери 1–2 подхода, "
            "вес не повышай.")


async def ensure_day_plan(user: dict[str, Any], day: date,
                          sched: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """План дня; сначала гарантирует недельную опору."""
    if sched is None:
        schedule = await db.get_schedule(user["telegram_id"])
        sched = next((s for s in schedule if s["planned_date"] == day.isoformat()), None)

    existing = await db.get_plan(user["telegram_id"], "day", day.isoformat())
    if plan_usable(existing):
        return existing

    week = await ensure_week_plan(user, day)
    if not week:
        return None

    if not sched:
        text = _rest_text(day)
    else:
        header = f"🗓 {WEEKDAYS[day.weekday()]}, {_fmt(day)} — {sched['day_name']} ({sched['focus']})"
        block, names = await _exercise_block(user, sched)
        note = None
        try:
            ctx = await _day_context(user, day, week, sched)
            note = _clean_note(_clean(await _chat(DAY_SYSTEM, json.dumps(ctx, ensure_ascii=False, default=str), 0.5)), names)
        except Exception as e:
            log.warning("Day plan generation failed: %s", e)

        parts = [header,
                 "Разминка: 5–10 минут — суставная разминка и 1–2 лёгких подхода."]
        if note:
            parts.append(note)
        parts.append(f"Упражнения:\n{block}")
        text = "\n\n".join(parts)
        if len(text) < MIN_PLAN_TEXT:
            text += ("\n\nРаботай с запасом 2–3 повтора. При плохом самочувствии убери 1–2 подхода, "
                     "вес не повышай.")

    plan_id = await db.save_plan(user["telegram_id"], "day", day.isoformat(), day.isoformat(),
                                 text, parent_id=week["id"])
    return await db.get_plan(user["telegram_id"], "day", day.isoformat()) if plan_id else None
