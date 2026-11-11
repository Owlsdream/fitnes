# handlers/plan.py — план недели и ближайшего дня (каскад горизонтов)
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

import database as db
from services.planner import ensure_day_plan, ensure_week_plan

router = Router()


async def show_plans(message: Message, user_id: int) -> None:
    user = await db.get_user(user_id)
    if not user or not user.get("gender"):
        await message.answer("Сначала пройди /start и заполни профиль.")
        return

    sched = await db.get_next_scheduled(user_id)
    today = date.today()
    if sched:
        planned = date.fromisoformat(sched["planned_date"])
        target = planned if planned > today else today
    else:
        target = today
    await message.answer("⏳ Собираю план недели и ближайшего дня…")

    week = await ensure_week_plan(user, target)
    day = await ensure_day_plan(user, target, sched)

    parts = [week["markdown_text"] if week else "Не удалось собрать план недели."]
    if day:
        parts.append(day["markdown_text"])
    text = "\n\n".join(parts)
    for i in range(0, len(text), 3800):
        await message.answer(text[i:i + 3800])


@router.message(Command("plan"))
async def cmd_plan(message: Message) -> None:
    await show_plans(message, message.from_user.id)


@router.callback_query(F.data == "menu:plan")
async def menu_plan(callback: CallbackQuery) -> None:
    await show_plans(callback.message, callback.from_user.id)
    await callback.answer()
