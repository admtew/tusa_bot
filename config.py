"""Конфигурация бота. Все значения берутся из .env файла."""
import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
# Публичный HTTPS-адрес, на котором крутится Mini App (см. README)
WEBAPP_URL: str = os.getenv("WEBAPP_URL", "https://example.com").rstrip("/")
# Порт локального веб-сервера (API + статика Mini App)
PORT: int = int(os.getenv("PORT", "8080"))
# Путь к файлу базы данных
DB_PATH: str = os.getenv("DB_PATH", "tusa.db")

# --- Антифрод ---
# Telegram выдаёт ID последовательно: чем больше ID, тем свежее аккаунт.
# Рефералы с ID выше порога считаются "слишком свежими" и не засчитываются.
# Подкручивай по ситуации (см. README, раздел "Антифрод").
NEW_ID_THRESHOLD: int = int(os.getenv("NEW_ID_THRESHOLD", "8500000000"))

# За сколько часов до начала слать напоминание с адресом
ADDRESS_REVEAL_HOURS: int = int(os.getenv("ADDRESS_REVEAL_HOURS", "3"))

# Контакт поддержки (показывается в /support). Поменяй на свой.
SUPPORT_CONTACT: str = os.getenv("SUPPORT_CONTACT", "@your_support")

if not BOT_TOKEN:
    raise SystemExit("Заполни BOT_TOKEN в .env (токен из @BotFather)")
