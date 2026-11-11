# Деплой фитнес-бота на сервер

## Что нужно

- VPS с Ubuntu 22.04/24.04, **1–2 vCPU, 1–2 ГБ RAM, 10 ГБ диска** (бот лёгкий: SQLite + polling).
- Доступ по SSH (root или sudo-пользователь).
- Сервер должен уметь ходить на `api.telegram.org` и `api.deepseek.com`. Если Telegram с хоста недоступен — берите VPS вне РФ или настройте прокси.
- Токен бота и ключ DeepSeek из локального `.env`.

## Вариант A — systemd (без Docker)

```bash
# 1. Пользователь и зависимости
sudo adduser --disabled-password --gecos "" fitbot
sudo apt update && sudo apt install -y python3.12 python3.12-venv git

# 2. Код
sudo git clone https://github.com/Owlsdream/fitnes.git /opt/fitnes
sudo cp -r /opt/fitnes/fitness_bot /opt/fitness_bot
sudo chown -R fitbot:fitbot /opt/fitness_bot

# 3. Окружение
sudo -u fitbot python3 -m venv /opt/fitness_bot/venv
sudo -u fitbot /opt/fitness_bot/venv/bin/pip install -r /opt/fitness_bot/requirements.txt

# 4. .env (скопировать с локальной машины или заполнить на месте)
scp .env fitbot@SERVER:/opt/fitness_bot/.env
ssh fitbot@SERVER "chmod 600 /opt/fitness_bot/.env"
```

В `.env` на сервере:

```
BOT_TOKEN=...
AI_PROVIDER=deepseek
AI_API_KEY=sk-...
AI_MODEL=deepseek-chat
AI_BASE_URL=
EMBED_MODEL=bge-m3
EMBED_BASE_URL=http://localhost:11434/v1
EMBED_API_KEY=ollama
DB_PATH=fitness.db
```

`EMBED_*` нужны только если на сервере будет Ollama. Без неё векторный поиск
отключается сам, RAG работает в лексическом режиме — бот продолжает работать.

```bash
# 5. История тренировок (по желанию)
scp fitness.db fitbot@SERVER:/opt/fitness_bot/fitness.db

# 6. systemd
sudo cp /opt/fitness_bot/deploy/fitness-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fitness-bot
sudo systemctl status fitness-bot
journalctl -u fitness-bot -f
```

## Вариант B — Docker

```bash
sudo apt install -y docker.io docker-compose-v2
cd /opt/fitness_bot
# .env и fitness.db уже на месте
sudo docker compose up -d --build
sudo docker compose logs -f
```

Тома в `docker-compose.yml` монтируют `fitness.db`, `data/books` и
`data/knowledge/books` с хоста — база и книги переживают пересборку образа.

## Ollama для векторного RAG (опционально)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull bge-m3
```

Нужно ~2 ГБ RAM. Если памяти мало — не ставьте: бот откатится на лексический поиск.
Индекс пересчитывается командой:

```bash
/opt/fitness_bot/venv/bin/python /opt/fitness_bot/scripts/index_rag.py
```

## Обновления

```bash
cd /opt/fitnes && sudo git pull
sudo rsync -a --exclude .env --exclude fitness.db --exclude venv \
    /opt/fitnes/fitness_bot/ /opt/fitness_bot/
sudo systemctl restart fitness-bot
```

## Бэкап базы

```bash
# crontab -e
0 4 * * * cp /opt/fitness_bot/fitness.db /opt/backup/fitness-$(date +\%F).db
```

## Важно

- Не запускайте бота одновременно локально и на сервере: два polling-клиента
  конфликтуют (Telegram отдаёт 409). Перед запуском на сервере остановите локальный.
- `.env` в git не попадает; на сервере права `600`.
- Логи — `journalctl -u fitness-bot` или `docker compose logs`.
