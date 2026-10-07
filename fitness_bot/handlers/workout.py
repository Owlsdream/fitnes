# handlers/workout.py — пошаговое проведение тренировки (Этап 4)
from datetime import datetime
import aiosqlite
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import keyboards as kb
import database as db
from states import Workout
from services.progression import suggest_weight

router = Router()


async def begin_workout(message: Message, user_id: int, state: FSMContext) -> None:
    """Выбор дня программы и старт тренировки."""
    program = await db.get_current_program(user_id)
    if not program:
        await message.answer("Сначала пройди /start и получи программу.")
        return
    days = await db.get_days(program["id"])
    rows = [[InlineKeyboardButton(text=f"{d['day_name']} — {d['focus']}",
                                  callback_data=f"wk:day:{d['id']}")] for d in days]
    await message.answer("Какой день тренируем?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Command("workout"))
async def cmd_workout(message: Message, state: FSMContext) -> None:
    await begin_workout(message, message.from_user.id, state)


@router.callback_query(F.data.startswith("wk:day:"))
async def pick_day(callback: CallbackQuery, state: FSMContext) -> None:
    day_id = int(callback.data.split(":")[2])
    user_id = callback.from_user.id
    program = await db.get_current_program(user_id)
    exercises = await db.get_day_exercises(day_id)
    if not exercises:
        await callback.message.answer("В этом дне нет упражнений.")
        await callback.answer()
        return

    workout_id = await db.start_workout(user_id, program["id"] if program else None, day_id)
    await state.set_state(Workout.logging)
    await state.update_data(workout_id=workout_id, day_id=day_id, started_at=datetime.now(),
                            tonnage=0.0, ex_index=0, set_index=0)
    await callback.message.answer("🏋️ Тренировка начата! Поехали.")
    await _show_current_exercise(callback.message, user_id, state)
    await callback.answer()


async def _current_ex(user_id: int, state: FSMContext):
    data = await state.get_data()
    exercises = await db.get_day_exercises(data["day_id"])
    idx = data["ex_index"]
    if idx >= len(exercises):
        return None, exercises, data
    return exercises[idx], exercises, data


async def _show_current_ex_message(msg: Message, user_id: int, state: FSMContext) -> None:
    ex, _, data = await _current_ex(user_id, state)
    if ex is None:
        await _finish_prompt(msg, state)
        return
    name = ex["real_name"] or ex["custom_name"]
    last = await db.last_exercise_result(user_id, name)
    target = await suggest_weight(user_id, name, ex)
    progress = ""
    if last:
        progress = f"\n⬆️ Прошлый результат: {last['weight_kg']} кг × {last['reps']}"
    await msg.answer(
        f"🎯 {name}\n"
        f"Цель: {ex['sets']} подходов × {ex['reps_min']}-{ex['reps_max']} повторов\n"
        f"Рекомендуемый вес: {target} кг{progress}\n\n"
        f"Подход {data['set_index'] + 1}/{ex['sets']} — пришли «вес повтор» "
        f"(например: 60 8)",
        reply_markup=kb.skip_kb,
    )


async def _show_current_exercise(msg: Message, user_id: int, state: FSMContext) -> None:
    await _show_current_ex_message(msg, user_id, state)


def _parse_set_input(text: str) -> tuple[float, int] | None:
    """Разбирает строку вида '60 8' → (60.0, 8)."""
    parts = text.replace(",", ".").split()
    if len(parts) != 2:
        return None
    try:
        w = float(parts[0]); r = int(float(parts[1]))
    except ValueError:
        return None
    if 0 <= w <= 500 and 1 <= r <= 100:
        return w, r
    return None


@router.message(Workout.logging)
async def log_set(message: Message, state: FSMContext) -> None:
    parsed = _parse_set_input(message.text or "")
    if not parsed:
        await message.answer("Формат: «вес повтор», например: 60 8")
        return
    weight, reps = parsed
    data = await state.get_data()
    ex, _, _ = await _current_ex(message.from_user.id, state)
    name = ex["real_name"] or ex["custom_name"]

    await db.log_set(data["workout_id"], ex["id"], name, data["set_index"] + 1, weight, reps)
    tonnage = data.get("tonnage", 0.0) + weight * reps
    set_index = data["set_index"] + 1

    if set_index >= ex["sets"]:
        # Переход к следующему упражнению
        await state.update_data(ex_index=data["ex_index"] + 1, set_index=0, tonnage=tonnage)
        await _show_current_exercise(message, message.from_user.id, state)
    else:
        await state.update_data(set_index=set_index, tonnage=tonnage)
        await message.answer(f"✅ Записано: {weight} кг × {reps}. "
                             f"Тоннаж: {tonnage:.0f} кг. Следующий подход:")
        await _show_current_exercise(message, message.from_user.id, state)


@router.callback_query(Workout.logging, F.data == "wk:skip")
async def skip_ex(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    ex, _, _ = await _current_ex(callback.from_user.id, state)
    name = ex["real_name"] or ex["custom_name"]
    await db.log_set(data["workout_id"], ex["id"], name, 0, 0, 0, skipped=True)
    await state.update_data(ex_index=data["ex_index"] + 1, set_index=0)
    await callback.message.answer(f"⏭ Пропущено: {name}")
    await _show_current_exercise(callback.message, callback.from_user.id, state)
    await callback.answer()


@router.callback_query(Workout.logging, F.data == "wk:replace")
async def replace_ex(callback: CallbackQuery, state: FSMContext) -> None:
    """Замена упражнения на аналог из той же группы мышц."""
    from data.exercises import SUBSTITUTES
    ex, _, _ = await _current_ex(callback.from_user.id, state)
    group = ex.get("muscle_group") or ""
    subs = [s for s in SUBSTITUTES.get(group, []) if s != (ex["real_name"] or ex["custom_name"])]
    if not subs:
        await callback.message.answer("Для этого упражнения замен в базе нет.")
    else:
        rows = [[InlineKeyboardButton(text=s, callback_data=f"wk:rep:{s}")] for s in subs]
        await callback.message.answer("На что заменить?",
                                      reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(Workout.logging, F.data.startswith("wk:rep:"))
async def do_replace(callback: CallbackQuery, state: FSMContext) -> None:
    new_name = callback.data.split(":", 2)[2]
    data = await state.get_data()
    ex, _, _ = await _current_ex(callback.from_user.id, state)
    # Логируем старый слот как пропущенный, новый впишем в название подходов
    await db.log_set(data["workout_id"], ex["id"], ex["real_name"] or ex["custom_name"],
                     0, 0, 0, skipped=True)
    ex["custom_name"] = new_name
    ex["real_name"] = None
    await callback.message.answer(f"🔄 Заменили на: {new_name}. Работаем!")
    await _show_current_exercise(callback.message, callback.from_user.id, state)
    await callback.answer()


@router.callback_query(Workout.logging, F.data == "wk:finish")
async def early_finish(callback: CallbackQuery, state: FSMContext) -> None:
    await _finish_prompt(callback.message, state)
    await callback.answer()


async def _finish_prompt(msg: Message, state: FSMContext) -> None:
    await state.set_state(Workout.rpe)
    await msg.answer("Оцени тренировку по RPE от 1 до 10:", reply_markup=kb.rpe_kb)


@router.callback_query(Workout.rpe, F.data.startswith("wk:rpe:"))
async def finish_with_rpe(callback: CallbackQuery, state: FSMContext) -> None:
    rpe = int(callback.data.split(":")[1])
    data = await state.get_data()
    started = datetime.fromisoformat(str(data["started_at"]))
    duration = max(1, int((datetime.now() - started).total_seconds() // 60))
    tonnage = data.get("tonnage", 0.0)
    await db.finish_workout(data["workout_id"], duration, tonnage, rpe)
    await state.clear()

    # Сводка и сравнение с предыдущей тренировкой
    history = await db.recent_workouts(callback.from_user.id, limit=2)
    delta_txt = ""
    if len(history) >= 2:
        prev = history[1]["total_tonnage"] or 0
        if prev > 0:
            delta = (tonnage - prev) / prev * 100
            delta_txt = f"\n📈 Изменение к прошлой тренировке: {delta:+.1f}%"
    pr = await _personal_record(callback.from_user.id, data)
    await callback.message.edit_text(
        f"🏁 Тренировка завершена!\n"
        f"⏱ Длительность: {duration} мин\n"
        f"🏋️ Общий тоннаж: {tonnage:.0f} кг{delta_txt}\n"
        f"💭 RPE: {rpe}/10\n{pr}",
        reply_markup=kb.main_menu,
    )
    await callback.answer()


async def _personal_record(user_id: int, data: dict) -> str:
    """Ищет лучший подход этой тренировки и сравнивает с историческим рекордом."""
    out: list[str] = []
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT s.exercise_name, MAX(s.weight_kg) FROM sets s
               WHERE s.workout_id=? AND s.skipped=0 GROUP BY s.exercise_name""",
            (data["workout_id"],))
        best = await cur.fetchall()
        for name, w in best:
            cur2 = await conn.execute(
                """SELECT MAX(s2.weight_kg) FROM sets s2 JOIN workouts w2 ON w2.id=s2.workout_id
                   WHERE w2.user_id=? AND s2.exercise_name=? AND w2.id<>?""",
                (user_id, name, data["workout_id"]))
            row = await cur2.fetchone()
            if row and row[0] is not None and w > row[0]:
                out.append(f"🥇 Новый рекорд: {name} {w} кг!")
    return "\n".join(out)
