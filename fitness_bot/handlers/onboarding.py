# handlers/onboarding.py — 10-шаговый опрос (FSM)
from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

import keyboards as kb
import database as db
from states import Onboarding

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Приветствие + главное меню. Регистрация/создание строки пользователя."""
    u = message.from_user
    await db.upsert_user(u.id, u.username, u.first_name)
    await state.clear()
    await message.answer(
        f"👋 Привет, {u.first_name}!\n\n"
        "Я — твой персональный фитнес-тренер.\n"
        "Для начала пройдём короткий опрос из 10 шагов — "
        "это займёт пару минут, зато программа будет точной.\n\n"
        "В любой момент можно нажать /start или кнопки ниже.",
        reply_markup=kb.main_menu,
    )


# ---------- Шаг 1: Пол ----------
@router.callback_query(F.data == "menu:profile")
async def show_profile(callback: CallbackQuery) -> None:
    """Кнопка «⚙️ Профиль» показывает сохранённые данные."""
    user = await db.get_user(callback.from_user.id)
    if not user or not user.get("gender"):
        text = "Профиль пуст. Нажми /start и пройди опрос."
    else:
        labels = {"male": "мужской", "female": "женский",
                  "beginner": "новичок", "intermediate": "средний", "advanced": "опытный",
                  "lose_weight": "похудение", "gain_muscle": "набор массы",
                  "strength": "сила", "general": "общая форма"}
        def L(v): return labels.get(v, v)
        text = (
            f"👤 {user['first_name'] or ''} (ID: {user['telegram_id']})\n"
            f"Пол: {L(user['gender'])}\n"
            f"Год рождения: {user['birth_year']}\n"
            f"Рост: {user['height_cm']} см | Вес: {user['weight_kg']} кг\n"
            f"Уровень: {L(user['level'])} | Цель: {L(user['goal'])}\n"
            f"Тренировок в неделю: {user['days_per_week']} ({user['session_minutes']} мин)\n"
            f"Ограничения: {L(user['limitations'])} | Зоны: {L(user['priority_zones'])}"
        )
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data == "menu:start")
async def start_from_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Из главного меню сразу запускаем онбординг, если профиль пуст."""
    user = await db.get_user(callback.from_user.id)
    if not user or not user.get("gender"):
        await callback.message.answer("Шаг 1 из 10. Твой пол?")
        await state.set_state(Onboarding.gender)
        await callback.message.edit_reply_markup(reply_markup=kb.gender_kb)
    else:
        from handlers.workout import begin_workout
        await begin_workout(callback.message, callback.from_user.id, state)
    await callback.answer()


@router.callback_query(Onboarding.gender, F.data.startswith("ob:gender:"))
async def ob_gender(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(gender=callback.data.split(":")[1])
    await state.set_state(Onboarding.birth_year)
    await callback.message.edit_text("Шаг 2 из 10. Год рождения? (например, 1995)")
    await callback.answer()


# ---------- Шаг 2: Год рождения (число, валидация 1930–2015) ----------
@router.message(Onboarding.birth_year)
async def ob_birth(message: Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Введи год числом, например 1995.")
        return
    year = int(message.text.strip())
    if not 1930 <= year <= 2015:
        await message.answer("Год должен быть в диапазоне 1930–2015. Попробуй ещё раз.")
        return
    await state.update_data(birth_year=year)
    await state.set_state(Onboarding.height)
    await message.answer("Шаг 3 из 10. Рост в сантиметрах? (100–230)")


# ---------- Шаг 3: Рост (100–230) ----------
@router.message(Onboarding.height)
async def ob_height(message: Message, state: FSMContext) -> None:
    try:
        h = float(message.text.strip().replace(",", "."))
    except (ValueError, AttributeError):
        await message.answer("Введи рост числом, например 178.")
        return
    if not 100 <= h <= 230:
        await message.answer("Рост должен быть в диапазоне 100–230 см.")
        return
    await state.update_data(height_cm=h)
    await state.set_state(Onboarding.weight)
    await message.answer("Шаг 4 из 10. Вес в килограммах? (30–250)")


# ---------- Шаг 4: Вес (float, 30–250) ----------
@router.message(Onboarding.weight)
async def ob_weight(message: Message, state: FSMContext) -> None:
    try:
        w = float(message.text.strip().replace(",", "."))
    except (ValueError, AttributeError):
        await message.answer("Введи вес числом, например 72.5.")
        return
    if not 30 <= w <= 250:
        await message.answer("Вес должен быть в диапазоне 30–250 кг.")
        return
    await state.update_data(weight_kg=w)
    await state.set_state(Onboarding.level)
    await message.answer("Шаг 5 из 10. Твой уровень?", reply_markup=kb.level_kb)


# ---------- Шаги 5–10: кнопочные ----------
@router.callback_query(Onboarding.level, F.data.startswith("ob:level:"))
async def ob_level(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(level=callback.data.split(":")[1])
    await state.set_state(Onboarding.goal)
    await callback.message.edit_text("Шаг 6 из 10. Твоя цель?", reply_markup=kb.goal_kb)
    await callback.answer()


@router.callback_query(Onboarding.goal, F.data.startswith("ob:goal:"))
async def ob_goal(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(goal=callback.data.split(":")[1])
    await state.set_state(Onboarding.days)
    await callback.message.edit_text("Шаг 7 из 10. Сколько дней в неделю готов тренироваться?",
                                     reply_markup=kb.days_kb)
    await callback.answer()


@router.callback_query(Onboarding.days, F.data.startswith("ob:days:"))
async def ob_days(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(days_per_week=int(callback.data.split(":")[1]))
    await state.set_state(Onboarding.minutes)
    await callback.message.edit_text("Шаг 8 из 10. Сколько минут на тренировку?",
                                     reply_markup=kb.minutes_kb)
    await callback.answer()


@router.callback_query(Onboarding.minutes, F.data.startswith("ob:min:"))
async def ob_minutes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(session_minutes=int(callback.data.split(":")[1]))
    await state.set_state(Onboarding.limitations)
    await callback.message.edit_text("Шаг 9 из 10. Есть ограничения по здоровью?",
                                     reply_markup=kb.limits_kb)
    await callback.answer()


@router.callback_query(Onboarding.limitations, F.data.startswith("ob:lim:"))
async def ob_limits(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(limitations=callback.data.split(":")[1])
    await state.set_state(Onboarding.zones)
    await callback.message.edit_text("Шаг 10 из 10. Приоритетные зоны?",
                                     reply_markup=kb.zones_kb)
    await callback.answer()


@router.callback_query(Onboarding.zones, F.data.startswith("ob:zone:"))
async def ob_zones(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(priority_zones=callback.data.split(":")[1])
    data = await state.get_data()
    await state.clear()

    # Сохраняем профиль
    await db.save_onboarding(callback.from_user.id, data)

    # Генерируем программу
    from services.program_builder import generate_program
    user = await db.get_user(callback.from_user.id)
    report = await generate_program(user)
    await callback.message.edit_text(report)
    await callback.message.answer("Что дальше?", reply_markup=kb.main_menu)
    await callback.answer()
