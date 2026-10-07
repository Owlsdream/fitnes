# handlers/program.py — просмотр программы и импорт через ИИ (Этап 8)
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

import keyboards as kb
import database as db
from states import Import

router = Router()


@router.callback_query(F.data == "menu:program")
async def show_program(callback: CallbackQuery) -> None:
    """Кнопка «🏋️ Моя программа» — показывает текущую программу по дням."""
    program = await db.get_current_program(callback.from_user.id)
    if not program:
        await callback.message.answer("Программы ещё нет. Нажми /start и пройди опрос.")
        await callback.answer()
        return
    days = await db.get_days(program["id"])
    lines = [f"📋 {program['name']} (источник: {program['source']})"]
    for day in days:
        exs = await db.get_day_exercises(day["id"])
        lines.append(f"\n📅 {day['day_name']} — {day['focus']}")
        for e in exs:
            name = e["real_name"] or e["custom_name"]
            lines.append(f"  • {name}: {e['sets']}×{e['reps_min']}-{e['reps_max']}, "
                         f"отдых {e['rest_seconds']}с"
                         + (f", цель {e['target_weight']} кг" if e.get("target_weight") else ""))
    await callback.message.answer("\n".join(lines))
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
