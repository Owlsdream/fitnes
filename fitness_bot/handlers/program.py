# handlers/program.py — просмотр программы, план на месяц и импорт через ИИ (Этап 8)
from datetime import datetime

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

import keyboards as kb
import database as db
from states import Import

router = Router()

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _short_date(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"


@router.callback_query(F.data == "menu:program")
async def show_program(callback: CallbackQuery) -> None:
    """Программа и календарь: что пройдено, а что ещё предстоит."""
    user_id = callback.from_user.id
    program = await db.get_current_program(user_id)
    if not program:
        await callback.message.answer("Программы ещё нет. Нажми /start и пройди опрос.")
        await callback.answer()
        return

    sched = await db.get_schedule(user_id)
    if not sched:
        user = await db.get_user(user_id)
        if user:
            from services.program_builder import schedule_program
            await schedule_program(user, program["id"])
            sched = await db.get_schedule(user_id)
    done = [s for s in sched if s["status"] == "done"]
    pending = [s for s in sched if s["status"] == "pending"]
    goals = await db.get_goals(program["id"])
    block = await db.latest_plan(user_id, "block")

    lines = [f"📋 {program['name']}",
             f"✅ Пройдено: {len(done)} · 🔜 Осталось: {len(pending)}"]
    if goals:
        lines.append("🎯 " + ", ".join(
            f"{g['exercise']} {g['start_weight']:g}→{g['target_weight']:g} кг" for g in goals))
    if block:
        lines.append("\n" + block["markdown_text"])
    if pending:
        lines.append("\n🔜 Непройденные тренировки:")
        for s in pending[:10]:
            lines.append(f"  • {_short_date(s['planned_date'])} — {s['day_name']} ({s['focus']})")
        if len(pending) > 10:
            lines.append(f"  … и ещё {len(pending) - 10}")
    if done:
        lines.append("\n✔️ Завершённые:")
        for s in done[-5:]:
            d = datetime.strptime(s["planned_date"], "%Y-%m-%d")
            lines.append(f"  • {d.strftime('%d.%m.%Y')} — {s['day_name']}")
    if not sched:
        lines.append("\nКалендарь ещё не создан — нажми «▶️ Начать тренировку».")

    await callback.message.answer("\n".join(lines),
                                  reply_markup=kb.new_month_kb if not pending else kb.rebuild_kb)
    await callback.answer()


@router.callback_query(F.data.in_({"prog:new", "prog:new_yes"}))
async def new_month(callback: CallbackQuery, state: FSMContext) -> None:
    """Закрывает незавершённые тренировки и генерирует новый месяц."""
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    if not user or not user.get("gender"):
        await callback.message.answer("Сначала пройди /start и заполни профиль.")
        await callback.answer()
        return

    counts = await db.count_scheduled(user_id)
    if counts.get("pending", 0) and callback.data == "prog:new":
        await callback.message.answer(
            "Пересобрать программу? Текущий план на месяц будет заменён.",
            reply_markup=kb.confirm_rebuild_kb)
        await callback.answer()
        return

    from services.program_builder import generate_program, schedule_program
    await callback.message.answer("⏳ Собираю новую программу по истории тренировок…")
    await generate_program(user)
    program = await db.get_current_program(user_id)
    planned = await schedule_program(user, program["id"]) if program else 0
    await callback.message.answer(f"🆕 Новая программа на 2 месяца: {planned} тренировок.")
    from handlers.workout import begin_workout
    await begin_workout(callback.message, user_id, state)
    await callback.answer()


# ---------- Этап 8: импорт программы через ИИ ----------
@router.message(F.text == "/import")
async def cmd_import(message: Message, state: FSMContext) -> None:
    """Пользователь отправляет /import, затем текст/фото/файл с программой."""
    await state.set_state(Import.waiting_text)
    await message.answer(
        "📥 Пришли программу текстом, фото или файлом.\n"
        "Я распознаю упражнения, сопоставлю их с базой и предложу подтвердить."
    )


@router.message(Import.waiting_text, F.text)
async def handle_import(message: Message, state: FSMContext) -> None:
    """Разбор присланного текста программы (ИИ или локальный парсер)."""
    from services.ai_agent import parse_program_text, fallback_parse
    raw = message.text or ""

    parsed = await parse_program_text(raw) if raw.strip() else None
    if not parsed:
        parsed = fallback_parse(raw)
    if not parsed:
        await state.clear()
        await message.answer("Не удалось распознать программу. Попробуй формат:\n"
                             "День A: Приседания 3x10, Жим лёжа 4x8 ...")
        return

    # Сохраняем предпросмотр в FSM до подтверждения (остаёмся в состоянии Import)
    await state.update_data(imported=parsed)
    text = "🔍 Распознанная программа:\n\n"
    for day in parsed["days"]:
        text += f"📅 {day['name']}\n"
        for e in day["exercises"]:
            conf = e.get("confidence", 1.0)
            flag = "" if conf >= 0.7 else f" ⚠️ (low confidence {conf:.1f})"
            text += f"  • {e['name']}: {e['sets']}x{e['reps']}{flag}\n"
    await message.answer(text, reply_markup=kb.confirm_import_kb)


@router.message(Import.waiting_text, F.photo | F.document)
async def handle_import_file(message: Message, state: FSMContext) -> None:
    """Фото/файл: без AI_API_KEY просим продублировать текст."""
    await message.answer(
        "Получил файл 📎. Для распознавания изображений нужен AI_API_KEY в .env.\n"
        "Пока что пришли программу текстом.")


@router.callback_query(Import.waiting_text, F.data.startswith("imp:"))
async def confirm_import(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if callback.data == "imp:fix":
        await callback.message.answer("Ок, пришли исправленную программу снова через /import.")
        await callback.answer()
        return

    parsed = data.get("imported")
    if not parsed:
        await callback.message.answer("Данные импорта потеряны, повтори /import.")
        await callback.answer()
        return

    program_id = await db.create_program(callback.from_user.id,
                                         parsed.get("name", "Импортированная"), source="imported")
    for i, day in enumerate(parsed["days"], start=1):
        day_id = await db.add_day(program_id, day["name"], i, day.get("focus", ""))
        for j, e in enumerate(day["exercises"], start=1):
            await db.add_day_exercise(day_id, None, e["name"], e["sets"],
                                      e["reps"], e["reps"], e.get("rest", 90), j, False)
    await callback.message.answer("✅ Программа импортирована и стала текущей.")
    await callback.answer()
