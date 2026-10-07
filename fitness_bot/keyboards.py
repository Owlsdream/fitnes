# keyboards.py — инлайн-клавиатуры бота
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

main_menu = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🏋️ Моя программа", callback_data="menu:program"),
     InlineKeyboardButton(text="▶️ Начать тренировку", callback_data="menu:start")],
    [InlineKeyboardButton(text="📊 Статистика", callback_data="menu:stats"),
     InlineKeyboardButton(text="⚙️ Профиль", callback_data="menu:profile")],
])


def choice_kb(prefix: str, options: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Универсальная клавиатура выбора: [(текст, значение), ...]"""
    rows = [[InlineKeyboardButton(text=t, callback_data=f"{prefix}:{v}") for t, v in options]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


gender_kb = choice_kb("ob:gender", [("👨 Мужской", "male"), ("👩 Женский", "female")])
level_kb = choice_kb("ob:level", [("🌱 Новичок", "beginner"),
                                  ("🚀 Средний", "intermediate"),
                                  ("🏆 Опытный", "advanced")])
goal_kb = choice_kb("ob:goal", [("🔥 Похудение", "lose_weight"),
                                ("💪 Набор массы", "gain_muscle"),
                                ("🏋️ Сила", "strength"),
                                ("✨ Общая форма", "general")])
days_kb = choice_kb("ob:days", [(str(n), str(n)) for n in range(2, 7)])
minutes_kb = choice_kb("ob:min", [("45 мин", "45"), ("60 мин", "60"), ("90 мин", "90")])
limits_kb = choice_kb("ob:lim", [("✅ Нет", "none"), ("🩹 Поясница", "back"),
                                 ("🦵 Колени", "knees"), ("💪 Плечи", "shoulders"),
                                 ("🦾 Локти", "elbows")])
zones_kb = choice_kb("ob:zone", [("🦵 Ноги", "legs"), ("🫁 Грудь+спина", "chest_back"),
                                 ("💪 Руки+плечи", "arms_shoulders"), ("🤷 Всё равно", "any")])

skip_kb = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="⏭ Пропустить", callback_data="wk:skip"),
    InlineKeyboardButton(text="🔄 Заменить", callback_data="wk:replace"),
    InlineKeyboardButton(text="🏁 Закончить", callback_data="wk:finish"),
]])

rpe_kb = choice_kb("wk:rpe", [(str(n), str(n)) for n in range(1, 11)])

confirm_import_kb = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Подтвердить", callback_data="imp:ok"),
    InlineKeyboardButton(text="✏️ Исправить", callback_data="imp:fix"),
]])
