"""Централизованный сервис уведомлений (этап 3).
Защита от FloodWait, ретраи, рассылка батчами.
"""
import asyncio
import logging

from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest

log = logging.getLogger("tusa.notify")

BATCH_SIZE = 25          # сообщений в пачке
BATCH_PAUSE = 1.0        # пауза между пачками, сек
PER_MSG_PAUSE = 0.04     # ~25 сообщений/сек — в пределах лимитов Telegram
MAX_RETRY = 3


async def send_one(bot, chat_id: int, text: str, reply_markup=None) -> bool:
    """Одно сообщение с обработкой FloodWait и ретраями."""
    for attempt in range(MAX_RETRY):
        try:
            await bot.send_message(chat_id, text, reply_markup=reply_markup)
            return True
        except TelegramRetryAfter as e:            # FloodWait — ждём сколько просит Telegram
            wait = getattr(e, "retry_after", 2) + 0.5
            log.warning("FloodWait %ss for chat %s", wait, chat_id)
            await asyncio.sleep(wait)
        except TelegramForbiddenError:             # юзер заблокировал бота — пропускаем
            return False
        except TelegramBadRequest:                 # чат не найден и т.п.
            return False
        except Exception as e:
            log.warning("send_one to %s attempt %s failed: %s", chat_id, attempt, e)
            await asyncio.sleep(1.0)
    return False


async def broadcast(bot, user_ids, text: str, reply_markup=None) -> int:
    """Массовая рассылка батчами. Возвращает число успешно доставленных."""
    ids = list(dict.fromkeys(int(u) for u in user_ids if u))   # уникальные
    sent = 0
    for i in range(0, len(ids), BATCH_SIZE):
        batch = ids[i:i + BATCH_SIZE]
        for uid in batch:
            if await send_one(bot, uid, text, reply_markup):
                sent += 1
            await asyncio.sleep(PER_MSG_PAUSE)
        if i + BATCH_SIZE < len(ids):
            await asyncio.sleep(BATCH_PAUSE)
    log.info("broadcast: %s/%s delivered", sent, len(ids))
    return sent
