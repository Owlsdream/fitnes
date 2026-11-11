#!/usr/bin/env python
"""Проверка настроенной чат-модели: провайдер, ключ и один тестовый запрос.

    ..\\venv\\Scripts\\python.exe scripts\\check_ai.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from services.ai_agent import _chat  # noqa: E402


async def main() -> None:
    print(f"provider : {config.AI_PROVIDER}")
    print(f"model    : {config.AI_MODEL}")
    print(f"base_url : {config.AI_BASE_URL or '(default)'}")
    print(f"api_key  : {'задан' if config.AI_API_KEY else 'пуст'}")
    if not config.AI_API_KEY:
        print("\nКлюч не задан — добавь AI_API_KEY в .env (для DeepSeek: sk-...).")
        return
    text = await _chat("Отвечай предельно кратко.", "Скажи одно слово: готов", 0)
    print(f"\nответ    : {text!r}")


if __name__ == "__main__":
    asyncio.run(main())
