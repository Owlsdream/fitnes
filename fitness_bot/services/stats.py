# services/stats.py — статистика и генерация графиков matplotlib (Этап 6)
import io
from datetime import datetime, timedelta
from collections import defaultdict

import aiosqlite
import matplotlib
matplotlib.use("Agg")  # рендерим в буфер без GUI — важно для сервера/Windows
import matplotlib.pyplot as plt

import database as db


async def weekly_tonnage(user_id: int, weeks: int = 8) -> list[tuple[str, float]]:
    """Суммарный тоннаж по неделям за последние `weeks` недель."""
    workouts = await db.recent_workouts(user_id, limit=200)
    per_week: dict[str, float] = defaultdict(float)
    now = datetime.now()
    for w in workouts:
        try:
            dt = datetime.fromisoformat(w["started_at"])
        except (ValueError, TypeError):
            continue
        if (now - dt).days > weeks * 7:
            continue
        iso = dt.isocalendar()
        key = f"{iso[0]}-W{iso[1]:02d}"
        per_week[key] += w["total_tonnage"] or 0
    return sorted(per_week.items())


def trend_label(values: list[float]) -> str:
    """Рост / падение / плато по последним значениям (нужно минимум 3 точки)."""
    if len(values) < 3:
        return "📊 данных мало (нужно ≥3 тренировок)"
    first, last = values[0], values[-1]
    if first == 0:
        return "➡️ плато"
    change = (last - first) / first
    if change > 0.05:
        return f"📈 рост {change * 100:+.0f}%"
    if change < -0.05:
        return f"📉 падение {change * 100:+.0f}%"
    return "➡️ плато"


def plot_weight_progress(exercise: str, history: list[dict]) -> bytes:
    """Линейный график рабочего веса в упражнении по тренировкам."""
    xs = list(range(1, len(history) + 1))
    ys = [h["top_weight"] or 0 for h in history]
    fig, ax = plt.subplots(figsize=(7, 4), dpi=110)
    ax.plot(xs, ys, marker="o", color="#2E86AB")
    ax.set_title(f"Прогресс: {exercise}")
    ax.set_xlabel("Тренировка")
    ax.set_ylabel("Рабочий вес, кг")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def plot_tonnage_bars(pairs: list[tuple[str, float]]) -> bytes:
    """Столбчатая диаграмма недельного тоннажа."""
    if not pairs:
        return None
    labels, vals = zip(*pairs)
    fig, ax = plt.subplots(figsize=(7, 4), dpi=110)
    ax.bar(labels, vals, color="#A23B72")
    ax.set_title("Недельный тоннаж, кг")
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def plot_calendar(workouts: list[dict], days: int = 35) -> bytes:
    """Календарь тренировок: точки по дням за последние N дней."""
    done_dates = set()
    for w in workouts:
        try:
            d = datetime.fromisoformat(w["started_at"]).date()
            done_dates.add(d)
        except (ValueError, TypeError):
            continue
    today = datetime.now().date()
    fig, ax = plt.subplots(figsize=(5, 3.5), dpi=110)
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        if d in done_dates:
            ax.scatter(i % 7, -(i // 7), s=250, c="#4CAF50")
        else:
            ax.scatter(i % 7, -(i // 7), s=250, facecolor="none", edgecolor="#ccc")
    ax.set_title("Календарь тренировок (зелёный = была тренировка)")
    ax.invert_yaxis()
    ax.set_xticks([]); ax.set_yticks([])
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


async def build_stats_report(user_id: int) -> tuple[str, list[tuple[str, bytes]]]:
    """Готовит текстовый отчёт + список файлов графиков (name, png-bytes)."""
    workouts = await db.recent_workouts(user_id, limit=10)
    if not workouts:
        return "Пока нет завершённых тренировок — начни с ▶️ Начать тренировку.", []

    total = sum(w["total_tonnage"] or 0 for w in workouts)
    avg_dur = sum(w["duration_min"] or 0 for w in workouts) / len(workouts)
    text = (f"📊 Статистика за последние {len(workouts)} тренировок:\n"
            f"Суммарный тоннаж: {total:.0f} кг\n"
            f"Средняя длительность: {avg_dur:.0f} мин\n")

    # Топ-упражнение по истории — тренд
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT s.exercise_name, COUNT(*) c FROM sets s JOIN workouts w ON w.id=s.workout_id
               WHERE w.user_id=? AND s.skipped=0 GROUP BY s.exercise_name ORDER BY c DESC LIMIT 1""",
            (user_id,))
        row = await cur.fetchone()
    images: list[tuple[str, bytes]] = []
    if row:
        ex_name = row[0]
        hist = await db.exercise_history(user_id, ex_name, limit=15)
        text += f"Чаще всего: {ex_name} ({row[1]} подходов)\n" \
                f"Тренд: {trend_label([h['top_weight'] or 0 for h in hist])}"
        if len(hist) >= 2:
            images.append((f"progress_{ex_name[:10]}.png", plot_weight_progress(ex_name, hist)))

    weeks = await weekly_tonnage(user_id)
    if weeks:
        images.append(("tonnage.png", plot_tonnage_bars(list(weeks))))
    images.append(("calendar.png", plot_calendar(workouts)))
    return text, [img for img in images if img[1]]
