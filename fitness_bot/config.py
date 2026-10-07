# config.py — загрузка настроек из переменных окружения
import os
from dotenv import load_dotenv

load_dotenv()  # читает файл .env из корня проекта

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Создайте файл .env и укажите в нём BOT_TOKEN.")

# Ключ ИИ (OpenAI или Kimi) — опционально, без него бот работает
AI_API_KEY: str = os.getenv("AI_API_KEY", "")
AI_PROVIDER: str = os.getenv("AI_PROVIDER", "openai")  # openai | kimi
AI_MODEL: str = os.getenv("AI_MODEL", "gpt-4o-mini")

# Путь к файлу базы данных
DB_PATH: str = os.getenv("DB_PATH", "fitness.db")

# Опциональный белый список Telegram ID (через запятую). Пусто = доступ открыт всем.
_allowed = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: set[int] = {int(x) for x in _allowed.split(",") if x.strip().isdigit()}
