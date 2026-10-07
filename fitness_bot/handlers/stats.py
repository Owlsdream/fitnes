# handlers/stats.py — кнопка «📊 Статистика» (Этап 6) и команды Apple Health (Этап 9)
import io
import json
from datetime import date, datetime
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile

import database as db
from services.stats import build_stats_report

router = Router()


@router.callback_query(F.data == "menu:stats")
async def show_stats(callback: CallbackQuery) -> None:
    text, images = await build_stats_report(callback.from_user.id)
    await callback.message.answer(text)
    for name, png in images:
        await callback.message.answer_photo(
            BufferedInputFile(png, filename=name))
    await callback.answer()


# ---------- Этап 9: команды Apple Health (iOS Shortcuts / Health Auto Export) ----------
# Форматы:
#   /hrv 62          — вариабельность сердечного ритма, мс
#   /sleep 7.5       — часы сна
#   /rhr 54          — пульс покоя
#   /steps 8500      — шаги
@router.message(Command("hrv"))
async def cmd_hrv(message: Message) -> None:
    await _save_metric(message, "hrv_ms", float)


@router.message(Command("sleep"))
async def cmd_sleep(message: Message) -> None:
    await _save_metric(message, "sleep_hours", float)


@router.message(Command("rhr"))
async def cmd_rhr(message: Message) -> None:
    await _save_metric(message, "resting_hr", float)


@router.message(Command("steps"))
async def cmd_steps(message: Message) -> None:
    await _save_metric(message, "steps", int)


async def _save_metric(message: Message, field: str, cast) -> None:
    """Общий парсер команд вида /metric <число> [<YYYY-MM-DD>]."""
    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer(f"Формат: /{field} <число> [дата YYYY-MM-DD]")
        return
    try:
        value = cast(args[1])
    except ValueError:
        await message.answer("Значение должно быть числом.")
        return
    date_str = args[2] if len(args) > 2 and _is_date(args[2]) else date.today().isoformat()
    await db.save_health_metric(message.from_user.id, date_str, **{field: value})
    await message.answer(f"✅ Сохранено: {field}={value} за {date_str}")


def _is_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


# ---------- JSON-файл от Health Auto Export (Вариант B) ----------
@router.message(F.text == "/healthfile")
async def cmd_health_file_placeholder(message: Message) -> None:
    await message.answer("Пришли JSON-файл экспорта — я разберу его.")


@router.message(F.document)
async def handle_health_json(message: Message) -> None:
    """Разбор JSON из Health Auto Export: {metrics: [{type, data: [{date, value}]}]}."""
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".json"):
        return
    buf = io.BytesIO()
    await message.bot.download_file(doc.file_id, buf)
    try:
        payload = json.loads(buf.getvalue().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        await message.answer("Не удалось прочитать JSON.")
        return

    mapping = {"hrv": "hrv_ms", "restingHeartRate": "resting_hr",
               "sleepAnalysis": "sleep_hours", "stepCount": "steps"}
    saved = 0
    for item in payload.get("metrics", []):
        field = mapping.get(item.get("type", ""))
        if not field:
            continue
        for point in item.get("data", [])[-14:]:
            date = str(point.get("date", ""))[:10]
            val = point.get("value")
            if _is_date(date) and isinstance(val, (int, float)):
                await db.save_health_metric(message.from_user.id, date, **{field: val})
                saved += 1
    await message.answer(f"✅ Импорт здоровья завершён: {saved} записей.")
