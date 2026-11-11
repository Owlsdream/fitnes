# database.py — схема SQLite и асинхронные функции доступа к данным
import aiosqlite
from datetime import date, datetime, timedelta
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

-- План тренировок на месяц: что уже проведено, а что ещё предстоит
CREATE TABLE IF NOT EXISTS scheduled_workouts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(telegram_id),
    program_id   INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
    day_id       INTEGER NOT NULL REFERENCES workout_days(id) ON DELETE CASCADE,
    planned_date TEXT NOT NULL,              -- YYYY-MM-DD
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending / done / skipped
    workout_id   INTEGER REFERENCES workouts(id),
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE(program_id, day_id, planned_date)
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

-- Планы по горизонтам: месяц (программа) → неделя → день
CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(telegram_id),
    scope         TEXT NOT NULL,             -- block | week | day (month — это program)
    period_start  TEXT NOT NULL,             -- YYYY-MM-DD
    period_end    TEXT NOT NULL,
    parent_id     INTEGER REFERENCES plans(id),
    markdown_text TEXT NOT NULL,
    created_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, scope, period_start)
);

-- Целевые показатели программы на 2 месяца (основные движения)
CREATE TABLE IF NOT EXISTS program_goals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    program_id   INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
    exercise     TEXT NOT NULL,
    start_weight REAL,
    target_weight REAL,
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE(program_id, exercise)
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
                           rest_seconds: int, order_num: int, is_compound: bool,
                           target_weight: float | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO day_exercises
               (day_id, exercise_id, custom_name, sets, reps_min, reps_max,
                rest_seconds, order_num, is_compound, target_weight)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (day_id, exercise_id, custom_name, sets, reps_min, reps_max,
             rest_seconds, order_num, int(is_compound), target_weight),
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


# ---------- План на месяц ----------
async def create_schedule(user_id: int, program_id: int, day_ids: list[int],
                          days_per_week: int, start: date, weeks: int = 4) -> int:
    """Раскладывает дни программы по календарю на `weeks` недель.

    Тренировки распределяются равномерно: первая — в день старта,
    последняя — в последний день периода, между ними равные промежутки.
    """
    total = max(1, weeks * days_per_week)
    span = weeks * 7
    offsets = [round(i * (span - 1) / (total - 1)) if total > 1 else 0 for i in range(total)]
    async with aiosqlite.connect(DB_PATH) as db:
        # новый план заменяет прежний календарь (сами тренировки остаются в истории)
        await db.execute("DELETE FROM scheduled_workouts WHERE user_id=?", (user_id,))
        # планы недели/дня устаревают; план блока (2 месяца) живёт до пересборки программы
        await db.execute("DELETE FROM plans WHERE user_id=? AND scope IN ('week','day')", (user_id,))
        await db.executemany(
            """INSERT OR IGNORE INTO scheduled_workouts
               (user_id, program_id, day_id, planned_date)
               VALUES (?, ?, ?, ?)""",
            [(user_id, program_id, day_ids[i % len(day_ids)],
              (start + timedelta(days=offsets[i])).isoformat()) for i in range(total)])
        await db.commit()
    return total


async def has_schedule(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM scheduled_workouts WHERE user_id=? LIMIT 1", (user_id,))
        return await cur.fetchone() is not None


async def get_schedule(user_id: int, status: str | None = None) -> list[dict[str, Any]]:
    """Все запланированные тренировки пользователя с названием дня программы."""
    sql = """SELECT s.*, d.day_name, d.focus, d.day_order
             FROM scheduled_workouts s JOIN workout_days d ON d.id = s.day_id
             WHERE s.user_id=?"""
    params: list[Any] = [user_id]
    if status:
        sql += " AND s.status=?"
        params.append(status)
    sql += " ORDER BY s.planned_date, s.id"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, tuple(params))
        return [dict(r) for r in await cur.fetchall()]


async def get_next_scheduled(user_id: int) -> Optional[dict[str, Any]]:
    """Ближайшая непройденная тренировка (просроченные идут первыми)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT s.*, d.day_name, d.focus, d.day_order, p.name AS program_name
               FROM scheduled_workouts s
               JOIN workout_days d ON d.id = s.day_id
               JOIN programs p ON p.id = s.program_id
               WHERE s.user_id=? AND s.status='pending'
               ORDER BY s.planned_date, s.id LIMIT 1""", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_scheduled(scheduled_id: int) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT s.*, d.day_name, d.focus, p.name AS program_name
               FROM scheduled_workouts s
               JOIN workout_days d ON d.id = s.day_id
               JOIN programs p ON p.id = s.program_id
               WHERE s.id=?""", (scheduled_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def complete_scheduled(scheduled_id: int, workout_id: int | None = None,
                             status: str = "done") -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE scheduled_workouts SET status=?, workout_id=COALESCE(?, workout_id) WHERE id=?",
            (status, workout_id, scheduled_id))
        await db.commit()


async def count_scheduled(user_id: int) -> dict[str, int]:
    """Счётчики по статусам: pending / done / skipped."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT status, COUNT(*) FROM scheduled_workouts WHERE user_id=? GROUP BY status",
            (user_id,))
        return {status: n for status, n in await cur.fetchall()}


# ---------- Планы по горизонтам (week / day) ----------
async def save_plan(user_id: int, scope: str, period_start: str, period_end: str,
                    text: str, parent_id: int | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO plans (user_id, scope, period_start, period_end, parent_id, markdown_text)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, scope, period_start) DO UPDATE SET
                 period_end=excluded.period_end,
                 parent_id=excluded.parent_id,
                 markdown_text=excluded.markdown_text,
                 created_at=datetime('now')""",
            (user_id, scope, period_start, period_end, parent_id, text))
        await db.commit()
        cur = await db.execute(
            "SELECT id FROM plans WHERE user_id=? AND scope=? AND period_start=?",
            (user_id, scope, period_start))
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def get_plan(user_id: int, scope: str, period_start: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM plans WHERE user_id=? AND scope=? AND period_start=?",
            (user_id, scope, period_start))
        row = await cur.fetchone()
        return dict(row) if row else None


# ---------- Прогресс по упражнениям (для генерации весов) ----------
async def exercise_progress(user_id: int, limit: int = 60) -> list[dict[str, Any]]:
    """Лучший и последний вес/повторы по упражнению — база для прогрессии."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """WITH ranked AS (
                   SELECT s.exercise_name AS name, s.weight_kg, s.reps, s.done_at,
                          ROW_NUMBER() OVER (PARTITION BY s.exercise_name
                                             ORDER BY s.done_at DESC, s.id DESC) AS rn_last,
                          ROW_NUMBER() OVER (PARTITION BY s.exercise_name
                                             ORDER BY s.weight_kg DESC, s.reps DESC) AS rn_best
                   FROM sets s JOIN workouts w ON w.id = s.workout_id
                   WHERE w.user_id=? AND s.skipped=0
               )
               SELECT name,
                      MAX(weight_kg) AS best_weight,
                      MAX(CASE WHEN rn_best=1 THEN reps END) AS best_reps,
                      MAX(CASE WHEN rn_last=1 THEN weight_kg END) AS last_weight,
                      MAX(CASE WHEN rn_last=1 THEN reps END) AS last_reps,
                      MAX(CASE WHEN done_at >= date('now','-180 day')
                               THEN weight_kg*(1.0+reps/30.0) END) AS recent_e1rm,
                      COUNT(*) AS sets,
                      MAX(done_at) AS last_date
               FROM ranked GROUP BY name
               ORDER BY last_date DESC LIMIT ?""",
            (user_id, limit))
        return [dict(r) for r in await cur.fetchall()]


# ---------- Цели программы на 2 месяца ----------
async def save_goals(program_id: int, goals: list[dict[str, Any]]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT INTO program_goals (program_id, exercise, start_weight, target_weight)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(program_id, exercise) DO UPDATE SET
                 start_weight=excluded.start_weight,
                 target_weight=excluded.target_weight""",
            [(program_id, g["exercise"], g.get("start_weight"), g.get("target_weight"))
             for g in goals])
        await db.commit()


async def get_goals(program_id: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM program_goals WHERE program_id=? ORDER BY id", (program_id,))
        return [dict(r) for r in await cur.fetchall()]


async def latest_plan(user_id: int, scope: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM plans WHERE user_id=? AND scope=? ORDER BY period_start DESC LIMIT 1",
            (user_id, scope))
        row = await cur.fetchone()
        return dict(row) if row else None


async def delete_plans(user_id: int, scope: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM plans WHERE user_id=? AND scope=?", (user_id, scope))
        await db.commit()
