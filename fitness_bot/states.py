# states.py — FSM-состояния для онбординга и тренировки
from aiogram.fsm.state import State, StatesGroup


class Onboarding(StatesGroup):
    gender = State()          # 1. Пол
    birth_year = State()      # 2. Год рождения
    height = State()          # 3. Рост
    weight = State()          # 4. Вес
    level = State()           # 5. Уровень
    goal = State()            # 6. Цель
    days = State()            # 7. Дней в неделю
    minutes = State()         # 8. Минут на тренировку
    limitations = State()     # 9. Ограничения
    zones = State()           # 10. Приоритетные зоны


class Workout(StatesGroup):
    logging = State()         # идёт логирование подходов
    rpe = State()             # финальная оценка RPE


class Import(StatesGroup):
    waiting_text = State()    # ждём текст/фото программы


class AI(StatesGroup):
    waiting = State()
