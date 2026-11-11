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
from services.program_builder import schedule_program

router = Router()

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _fmt_date(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{WEEKDAYS[d.weekday()]}, {d.strftime('%d.%m.%Y')}"


async def begin_workout(message: Message, user_id: int, state: FSMContext) -> None:
    """Показывает ближайшую непройденную тренировку из плана на месяц."""
    program = await db.get_current_program(user_id)
    if not program:
        await message.answer("Сначала пройди /start и получи программу.")
        return

    # программы, созданные до появления календаря, получают план автоматически
    if not await db.has_schedule(user_id):
        user = await db.get_user(user_id)
        if user:
            await schedule_program(user, program["id"])

    sched = await db.get_next_scheduled(user_id)
    if not sched:
        counts = await db.count_scheduled(user_id)
        if counts.get("done", 0):
            await message.answer(
                f"🎉 Все тренировки плана закрыты ({counts['done']} шт.).\n"
                "Сгенерировать новый план на месяц?",
                reply_markup=kb.new_month_kb)
        else:
            await message.answer("В плане нет тренировок. Нажми /start и пройди опрос.")
        return

    counts = await db.count_scheduled(user_id)
    total = sum(counts.values())
    done = counts.get("done", 0)
    await message.answer(
        f"📅 Следующая тренировка: <b>{sched['day_name']}</b> — {sched['focus']}\n"
        f"{_fmt_date(sched['planned_date'])}\n"
        f"Пройдено {done} из {total}.\n\nНачинаем?",
        reply_markup=kb.start_workout_kb(sched["id"]))


@router.message(Command("workout"))
async def cmd_workout(message: Message, state: FSMContext) -> None:
    await begin_workout(message, message.from_user.id, state)


@router.callback_query(F.data.startswith("wk:start:"))
async def start_scheduled(callback: CallbackQuery, state: FSMContext) -> None:
    """Запуск конкретной запланированной тренировки."""
    scheduled_id = int(callback.data.split(":")[2])
    sched = await db.get_scheduled(scheduled_id)
    user_id = callback.from_user.id
    if not sched or sched["user_id"] != user_id or sched["status"] != "pending":
        await callback.answer("Эта тренировка уже неактуальна.", show_alert=True)
        return

    exercises = await db.get_day_exercises(sched["day_id"])
    if not exercises:
        await callback.answer("В этом дне нет упражнений.", show_alert=True)
        return

    workout_id = await db.start_workout(user_id, sched["program_id"], sched["day_id"])
    await state.set_state(Workout.logging)
    await state.update_data(workout_id=workout_id, day_id=sched["day_id"],
                            scheduled_id=scheduled_id, started_at=datetime.now(),
                            tonnage=0.0, ex_index=0, set_index=0)
    await callback.message.edit_text(f"🏋️ {sched['day_name']} — {sched['focus']}. Поехали!")
    await _show_current_exercise(callback.message, user_id, state)
    await callback.answer()


@router.callback_query(F.data.startswith("wk:skipday:"))
async def skip_scheduled(callback: CallbackQuery, state: FSMContext) -> None:
    scheduled_id = int(callback.data.split(":")[2])
    sched = await db.get_scheduled(scheduled_id)
    if not sched or sched["user_id"] != callback.from_user.id or sched["status"] != "pending":
        await callback.answer("Эта тренировка уже неактуальна.", show_alert=True)
        return
    await db.complete_scheduled(scheduled_id, None, "skipped")
    await callback.message.edit_text(f"⏭ Пропущено: {sched['day_name']} ({_fmt_date(sched['planned_date'])})")
    await begin_workout(callback.message, callback.from_user.id, state)
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
    rpe = int(callback.data.split(":")[2])
    data = await state.get_data()
    started = datetime.fromisoformat(str(data["started_at"]))
    duration = max(1, int((datetime.now() - started).total_seconds() // 60))
    tonnage = data.get("tonnage", 0.0)
    await db.finish_workout(data["workout_id"], duration, tonnage, rpe)
    if data.get("scheduled_id"):
        await db.complete_scheduled(data["scheduled_id"], data["workout_id"])
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
    counts = await db.count_scheduled(callback.from_user.id)
    pending = counts.get("pending", 0)
    tail = (f"\n\n📅 Осталось тренировок в плане: {pending}" if pending
            else "\n\n🎉 План месяца закрыт! Можно сгенерировать новый.")
    await callback.message.edit_text(
        f"🏁 Тренировка завершена!\n"
        f"⏱ Длительность: {duration} мин\n"
        f"🏋️ Общий тоннаж: {tonnage:.0f} кг{delta_txt}\n"
        f"💭 RPE: {rpe}/10\n{pr}{tail}",
        reply_markup=kb.new_month_kb if pending == 0 else kb.main_menu,
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
