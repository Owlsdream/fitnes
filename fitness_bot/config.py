# config.py — загрузка настроек из переменных окружения
import os
from dotenv import load_dotenv

load_dotenv()  # читает файл .env из корня проекта

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Создайте файл .env и укажите в нём BOT_TOKEN.")

# ---------- Чат-модель ----------
AI_PROVIDER: str = (os.getenv("AI_PROVIDER", "openai") or "openai").lower()  # deepseek | openai | kimi | ollama

_DEFAULT_BASE = {
    "deepseek": "https://api.deepseek.com/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "ollama": "http://localhost:11434/v1",
    "openai": "",
}
_DEFAULT_MODEL = {
    "deepseek": "deepseek-chat",
    "kimi": "moonshot-v1-8k",
    "ollama": "qwen2.5-coder:14b",
    "openai": "gpt-4o-mini",
}

# ключ: для deepseek можно отдельно задать DEEPSEEK_API_KEY
AI_API_KEY: str = os.getenv("AI_API_KEY", "") or (
    os.getenv("DEEPSEEK_API_KEY", "") if AI_PROVIDER == "deepseek" else "")
AI_MODEL: str = os.getenv("AI_MODEL", "") or _DEFAULT_MODEL.get(AI_PROVIDER, "gpt-4o-mini")
# OpenAI-совместимый endpoint; для deepseek/kimi/ollama подставляется по умолчанию
AI_BASE_URL: str = os.getenv("AI_BASE_URL", "") or _DEFAULT_BASE.get(AI_PROVIDER, "")

# ---------- Эмбеддинги (векторный RAG) ----------
# Живут отдельно от чат-модели: DeepSeek не отдаёт эмбеддинги, поэтому по умолчанию — локальный Ollama.
EMBED_MODEL: str = os.getenv("EMBED_MODEL", "bge-m3")
EMBED_BASE_URL: str = os.getenv("EMBED_BASE_URL", "http://localhost:11434/v1")
EMBED_API_KEY: str = os.getenv("EMBED_API_KEY", "ollama")

# Путь к файлу базы данных
DB_PATH: str = os.getenv("DB_PATH", "fitness.db")

# Опциональный белый список Telegram ID (через запятую). Пусто = доступ открыт всем.
_allowed = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: set[int] = {int(x) for x in _allowed.split(",") if x.strip().isdigit()}
