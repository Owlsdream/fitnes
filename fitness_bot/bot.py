# bot.py — точка входа: создание бота, регистрация роутеров, запуск polling
import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent, Message

import config
import database as db
from data.exercises import EXERCISES
from handlers import onboarding, program, workout, stats
from services.ai_agent import ask_for_user

router_ai = Router()


@router_ai.message(Command("ai"))
async def cmd_ai(message: Message) -> None:
    """Ручной запрос ИИ-разбора (Этап 7). Без AI_API_KEY вежливо отвечаем."""
    text = await ask_for_user(message.from_user.id, kind="manual")
    await message.answer(text or "ИИ-аналитик недоступен: проверь AI_API_KEY в .env.")


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("fitness-bot")

    # 1. Инфраструктура: БД + справочник упражнений
    await db.init_db()
    await db.seed_exercises(EXERCISES)
    log.info("База данных готова (%s)", config.DB_PATH)

    # 2. Bot и Dispatcher (aiogram 3.x)
    bot = Bot(token=config.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    # Ограничение доступа: если ALLOWED_USER_IDS задан — бот отвечает только «своим»
    if config.ALLOWED_USER_IDS:
        from aiogram import BaseMiddleware

        class AllowlistMiddleware(BaseMiddleware):
            async def __call__(self, handler, event, data):
                uid = getattr(getattr(event, "from_user", None), "id", None)
                if uid in config.ALLOWED_USER_IDS:
                    return await handler(event, data)
                if isinstance(event, Message):
                    await event.answer("Доступ ограничен: бот приватный.")
                return None

        dp.message.wrap_outer_middleware(AllowlistMiddleware(), {})
        dp.callback_query.wrap_outer_middleware(AllowlistMiddleware(), {})

    # 3. Регистрация роутеров: онбординг → программа/импорт → тренировка → статистика/health → ИИ
    dp.include_router(onboarding.router)
    dp.include_router(program.router)
    dp.include_router(workout.router)
    dp.include_router(stats.router)
    dp.include_router(router_ai)

    # 4. Глобальный обработчик ошибок — бот не должен падать
    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        log.exception("Необработанная ошибка: %s", event.exception)
        return True

    # 5. Команды в меню Telegram
    await bot.set_my_commands([
        BotCommand(command="/start", description="Начать / главное меню"),
        BotCommand(command="/workout", description="Начать тренировку"),
        BotCommand(command="/import", description="Импортировать программу"),
        BotCommand(command="/ai", description="ИИ-разбор прогресса"),
    ])

    log.info("Бот запущен (polling)…")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")
