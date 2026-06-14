"""Конфигурация бота. Все значения берутся из .env файла."""
import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
# Публичный HTTPS-адрес, на котором крутится Mini App (см. README)
WEBAPP_URL: str = os.getenv("WEBAPP_URL", "https://example.com").rstrip("/")
# Порт локального веб-сервера (API + статика Mini App)
PORT: int = int(os.getenv("PORT", "8080"))
# Путь к файлу базы данных.
# Если подключён постоянный диск Railway (Volume) на /data — пишем туда,
# чтобы события и билеты НЕ стирались при каждом деплое.
_default_db = "/data/tusa.db" if os.path.isdir("/data") else "tusa.db"
DB_PATH: str = os.getenv("DB_PATH", _default_db)

# --- Антифрод ---
# Порог "слишком свежего" аккаунта для рефералок. 0 = выключено (рекомендуется,
# т.к. у настоящих новых аккаунтов Telegram тоже большие ID и порог их режет).
# Защита от накруток держится на: уникальности (1 человек = 1 реферал на событие)
# и обязательной подписке на канал в момент выдачи билета.
NEW_ID_THRESHOLD: int = int(os.getenv("NEW_ID_THRESHOLD", "0"))

# За сколько часов до начала открывать адрес в приложении (напоминания шлются за 5ч и 2ч)
ADDRESS_REVEAL_HOURS: int = int(os.getenv("ADDRESS_REVEAL_HOURS", "5"))

# Контакт поддержки (показывается в /support). Поменяй на свой.
SUPPORT_CONTACT: str = os.getenv("SUPPORT_CONTACT", "@workersant")

# Обязательная подписка на канал для доступа к боту (без @). Пусто = выключено.
# ВАЖНО: бот должен быть АДМИНОМ этого канала, иначе проверка не работает.
REQUIRED_CHANNEL: str = os.getenv("REQUIRED_CHANNEL", "afterspartyrus").lstrip("@")

# Админы-модераторы: tg id через запятую (узнать свой — @userinfobot).
# Если пусто — модерации нет, события публикуются сразу.
ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()
]

if not BOT_TOKEN:
    raise SystemExit("Заполни BOT_TOKEN в .env (токен из @BotFather)")
