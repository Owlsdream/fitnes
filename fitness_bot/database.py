# database.py — схема SQLite и асинхронные функции доступа к данным
import aiosqlite
from datetime import datetime
from typing import Any, Optional

from config import DB_PATH

# ---------- Схема БД ----------
SCHEMA = """
PRAGMA foreign_keys = ON;

-- Профили пользователей. telegram_id — первичный ключ:
-- каждый аккаунт Telegram = отдельная строка, данные не пересекаются.
CREATE TABLE IF NOT EXISTS users (
    telegram_id      INTEGER PRIMARY KEY,
    username         TEXT,
    first_name       TEXT,
    gender           TEXT,            -- male / female
    birth_year       INTEGER,         -- 1930–2015
    height_cm        REAL,            -- 100–230
    weight_kg        REAL,            -- 30–250
    level            TEXT,            -- beginner / intermediate / advanced
    goal             TEXT,            -- lose_weight / gain_muscle / strength / general
    days_per_week    INTEGER,         -- 2–6
    session_minutes  INTEGER,         -- 45 / 60 / 90
    limitations      TEXT,            -- none / back / knees / shoulders / elbows
    priority_zones   TEXT,            -- legs / chest_back / arms_shoulders / any
    created_at       TEXT DEFAULT (datetime('now')),
    is_active        INTEGER DEFAULT 1
);

-- Программы тренировок пользователя
CREATE TABLE IF NOT EXISTS programs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(telegram_id),
    name        TEXT NOT NULL,
    source      TEXT DEFAULT 'generated',   -- generated / imported
    created_at  TEXT DEFAULT (datetime('now')),
    is_current  INTEGER DEFAULT 1
);

-- Дни внутри программы (День A, День B, ...)
CREATE TABLE IF NOT EXISTS workout_days (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    program_id  INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
    day_name    TEXT NOT NULL,
    day_order   INTEGER NOT NULL,
    focus       TEXT                       -- ноги / верх / низ / фулбоди ...
);

-- Упражнения в дне
CREATE TABLE IF NOT EXISTS day_exercises (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    day_id          INTEGER NOT NULL REFERENCES workout_days(id) ON DELETE CASCADE,
    exercise_id     INTEGER REFERENCES exercises(id),
    custom_name     TEXT,                  -- для импортированных/новых упражнений
    sets            INTEGER NOT NULL,
    reps_min        INTEGER NOT NULL,
    reps_max        INTEGER NOT NULL,
    rest_seconds    INTEGER DEFAULT 90,
    target_weight   REAL,                  -- рекомендуемый вес (прогрессия)
    order_num       INTEGER NOT NULL,
    is_compound     INTEGER DEFAULT 0
);

-- Справочник упражнений
CREATE TABLE IF NOT EXISTS exercises (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    muscle_group TEXT NOT NULL,
    equipment   TEXT,
    is_compound INTEGER DEFAULT 0,
    limitation_safe TEXT   -- через запятую: какие ограничения «безопасно» учитывать
);

-- Проведённые тренировки
CREATE TABLE IF NOT EXISTS workouts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(telegram_id),
    program_id      INTEGER REFERENCES programs(id),
    day_id          INTEGER REFERENCES workout_days(id),
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    duration_min    INTEGER,
    total_tonnage   REAL DEFAULT 0,
    rpe             INTEGER,               -- субъективная оценка 1–10
    notes           TEXT
);

-- Подходы (лог каждой выполненной серии)
CREATE TABLE IF NOT EXISTS sets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_id  INTEGER NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
    day_exercise_id INTEGER REFERENCES day_exercises(id),
    exercise_name   TEXT NOT NULL,
    set_number  INTEGER NOT NULL,
    weight_kg   REAL NOT NULL,
    reps        INTEGER NOT NULL,
    skipped     INTEGER DEFAULT 0,
    done_at     TEXT DEFAULT (datetime('now'))
);

-- Метрики здоровья из Apple Health
CREATE TABLE IF NOT EXISTS health_metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(telegram_id),
    date        TEXT NOT NULL,             -- YYYY-MM-DD
    hrv_ms      REAL,                      -- вариабельность сердечного ритма
    resting_hr  REAL,                      -- пульс покоя
    sleep_hours REAL,
    steps       INTEGER,
    source      TEXT DEFAULT 'shortcuts',
    UNIQUE(user_id, date)
);

-- Рекомендации ИИ
CREATE TABLE IF NOT EXISTS ai_recommendations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(telegram_id),
    created_at  TEXT DEFAULT (datetime('now')),
    kind        TEXT,                      -- post_workout / weekly / anomaly
    summary     TEXT,
    payload_json TEXT,
    confidence  REAL,
    applied     INTEGER DEFAULT 0,
    valid       INTEGER DEFAULT 1          -- результат двойной валидации
);
"""


async def init_db() -> None:
    """Создаёт все таблицы при первом запуске."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


# ---------- Пользователи ----------
async def upsert_user(telegram_id: int, username: str | None, first_name: str | None) -> None:
    """Создаёт пользователя или обновляет его имя. Дубликатов не будет:
    telegram_id — PRIMARY KEY + ON CONFLICT DO UPDATE."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO users (telegram_id, username, first_name)
               VALUES (?, ?, ?)
               ON CONFLICT(telegram_id)
               DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
            (telegram_id, username, first_name),
        )
        await db.commit()


async def get_user(telegram_id: int) -> Optional[dict[str, Any]]:
    """Возвращает данные ТОЛЬКО одного пользователя по его telegram_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def save_onboarding(telegram_id: int, data: dict[str, Any]) -> None:
    """Сохраняет результаты 10-шагового опроса онбординга."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE users SET gender=?, birth_year=?, height_cm=?, weight_kg=?, level=?,
               goal=?, days_per_week=?, session_minutes=?, limitations=?, priority_zones=?
               WHERE telegram_id=?""",
            (data["gender"], data["birth_year"], data["height_cm"], data["weight_kg"],
             data["level"], data["goal"], data["days_per_week"], data["session_minutes"],
             data["limitations"], data["priority_zones"], telegram_id),
        )
        await db.commit()


# ---------- Программа ----------
async def set_current_program_none(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE programs SET is_current=0 WHERE user_id=?", (user_id,))
        await db.commit()


async def create_program(user_id: int, name: str, source: str = "generated") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE programs SET is_current=0 WHERE user_id=?", (user_id,))
        cur = await db.execute(
            "INSERT INTO programs (user_id, name, source, is_current) VALUES (?, ?, ?, 1)",
            (user_id, name, source),
        )
        await db.commit()
        return cur.lastrowid


async def add_day(program_id: int, day_name: str, day_order: int, focus: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO workout_days (program_id, day_name, day_order, focus) VALUES (?, ?, ?, ?)",
            (program_id, day_name, day_order, focus),
        )
        await db.commit()
        return cur.lastrowid


async def add_day_exercise(day_id: int, exercise_id: int | None, custom_name: str | None,
                           sets: int, reps_min: int, reps_max: int,
                           rest_seconds: int, order_num: int, is_compound: bool) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO day_exercises
               (day_id, exercise_id, custom_name, sets, reps_min, reps_max,
                rest_seconds, order_num, is_compound)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (day_id, exercise_id, custom_name, sets, reps_min, reps_max,
             rest_seconds, order_num, int(is_compound)),
        )
        await db.commit()
        return cur.lastrowid


async def get_current_program(user_id: int) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM programs WHERE user_id=? AND is_current=1 LIMIT 1", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_days(program_id: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM workout_days WHERE program_id=? ORDER BY day_order", (program_id,))
        return [dict(r) for r in await cur.fetchall()]


async def get_day_exercises(day_id: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT de.*, e.name AS real_name, e.muscle_group, e.limitation_safe
               FROM day_exercises de LEFT JOIN exercises e ON de.exercise_id = e.id
               WHERE de.day_id=? ORDER BY de.order_num""", (day_id,))
        return [dict(r) for r in await cur.fetchall()]


async def seed_exercises(rows: list[tuple]) -> None:
    """Заполняет справочник упражнений (идемпотентно)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT OR IGNORE INTO exercises (name, muscle_group, equipment, is_compound, limitation_safe)
               VALUES (?, ?, ?, ?, ?)""", rows)
        await db.commit()


# ---------- Тренировки ----------
async def start_workout(user_id: int, program_id: int | None, day_id: int | None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO workouts (user_id, program_id, day_id, started_at) VALUES (?, ?, ?, ?)",
            (user_id, program_id, day_id, datetime.now().isoformat(timespec="seconds")),
        )
        await db.commit()
        return cur.lastrowid


async def log_set(workout_id: int, day_exercise_id: int | None, exercise_name: str,
                  set_number: int, weight_kg: float, reps: int, skipped: bool = False) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO sets (workout_id, day_exercise_id, exercise_name,
                                 set_number, weight_kg, reps, skipped)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (workout_id, day_exercise_id, exercise_name, set_number, weight_kg, reps, int(skipped)),
        )
        await db.commit()


async def finish_workout(workout_id: int, duration_min: int, tonnage: float, rpe: int | None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE workouts SET finished_at=?, duration_min=?, total_tonnage=?, rpe=?
               WHERE id=?""",
            (datetime.now().isoformat(timespec="seconds"), duration_min, tonnage, rpe, workout_id),
        )
        await db.commit()


async def last_exercise_result(user_id: int, exercise_name: str) -> Optional[dict[str, Any]]:
    """Последний результат по упражнению — для показа прогресса и рекордов."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT s.weight_kg, s.reps, s.done_at FROM sets s
               JOIN workouts w ON w.id = s.workout_id
               WHERE w.user_id=? AND s.exercise_name=? AND s.skipped=0
               ORDER BY s.done_at DESC LIMIT 1""", (user_id, exercise_name))
        row = await cur.fetchone()
        return dict(row) if row else None


async def exercise_history(user_id: int, exercise_name: str, limit: int = 20) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT w.started_at, SUM(s.weight_kg * s.reps) AS volume,
                      MAX(s.weight_kg) AS top_weight
               FROM sets s JOIN workouts w ON w.id=s.workout_id
               WHERE w.user_id=? AND s.exercise_name=? AND s.skipped=0
               GROUP BY w.id ORDER BY w.started_at DESC LIMIT ?""",
            (user_id, exercise_name, limit))
        rows = [dict(r) for r in await cur.fetchall()]
        return list(reversed(rows))


async def recent_workouts(user_id: int, limit: int = 8) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM workouts WHERE user_id=? AND finished_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT ?", (user_id, limit))
        return [dict(r) for r in await cur.fetchall()]


# ---------- Здоровье ----------
async def save_health_metric(user_id: int, date: str, **fields: Any) -> None:
    """UPSERT метрик за день: обновляем только присланные поля."""
    cols = ", ".join(k for k in fields if k != "date")
    updates = ", ".join(f"{k}=excluded.{k}" for k in fields if k != "date")
    params = [user_id, date] + [v for k, v in fields.items() if k != "date"]
    placeholders = ", ".join(["?"] * len(params))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"""INSERT INTO health_metrics (user_id, date, {cols})
                VALUES ({placeholders})
                ON CONFLICT(user_id, date) DO UPDATE SET {updates}""",
            tuple(params),
        )
        await db.commit()


async def get_health_latest(user_id: int, limit: int = 14) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM health_metrics WHERE user_id=? ORDER BY date DESC LIMIT ?",
            (user_id, limit))
        return [dict(r) for r in await cur.fetchall()]
