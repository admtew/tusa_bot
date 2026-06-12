"""Точка входа: Telegram-бот + веб-сервер Mini App + напоминания."""
import asyncio
import datetime as dt
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import db
from api import make_web_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tusa")

router = Router()


def webapp_kb(path: str = "", text: str = "Открыть party 🎉") -> InlineKeyboardMarkup:
    url = config.WEBAPP_URL + (f"#{path}" if path else "")
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))]]
    )


async def check_subscribed(bot: Bot, channel: str, user_id: int) -> bool:
    """Подписан ли user на @channel. Бот должен быть админом канала."""
    if not channel:
        return True
    try:
        member = await bot.get_chat_member(chat_id=f"@{channel}", user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:  # бот не админ / канал не найден — не блокируем гостя
        log.warning("check_subscribed(%s, %s) failed: %s", channel, user_id, e)
        return True


@router.message(CommandStart(deep_link=True))
async def start_deeplink(message: Message, command: CommandObject, bot: Bot) -> None:
    user = message.from_user
    is_new = db.upsert_user(user.id, user.username, user.first_name)
    payload = command.args or ""

    # --- реферальная ссылка: ref_<event_id>_<referrer_id> ---
    if payload.startswith("ref_"):
        try:
            _, eid, rid = payload.split("_", 2)
            event_id, referrer_id = int(eid), int(rid)
        except ValueError:
            await message.answer("Ссылка битая. Но все party — тут 👇", reply_markup=webapp_kb())
            return

        event = db.get_event(event_id)
        if not event:
            await message.answer("Этой party уже нет 😢 Но есть другие 👇", reply_markup=webapp_kb())
            return

        counted = False
        reason = ""
        if not is_new:
            reason = "ты уже был в боте — рефералка считается только за новых людей"
        elif user.id > config.NEW_ID_THRESHOLD:
            reason = "аккаунт слишком свежий"  # антифрод: новорег
        else:
            counted = db.add_referral(event_id, referrer_id, user.id)
            if not counted:
                reason = "этот переход уже был засчитан"

        text = (
            f"<b>{user.first_name}, тебя зовут на «{event['title']}»</b> 🎉\n\n"
            "Жми кнопку — детали и твой билет внутри."
        )
        if counted:
            text += "\n\nТот, кто тебя позвал, стал ближе к free-проходке 🔥"
        elif reason:
            log.info("referral not counted for %s: %s", user.id, reason)
        await message.answer(text, reply_markup=webapp_kb(f"event/{event_id}"))
        return

    # --- прямая ссылка на ивент: evt_<event_id> ---
    if payload.startswith("evt_"):
        try:
            event_id = int(payload[4:])
        except ValueError:
            event_id = 0
        if event_id and db.get_event(event_id):
            await message.answer("Вот эта party 👇", reply_markup=webapp_kb(f"event/{event_id}"))
            return

    await _send_welcome(message)


@router.message(CommandStart())
async def start_plain(message: Message) -> None:
    db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await _send_welcome(message)


async def _send_welcome(message: Message) -> None:
    name = message.from_user.first_name or "привет"
    await message.answer(
        f"<b>{name}, добро пожаловать в party</b> 🎉\n\n"
        "Все вечеринки города — в одном месте.\n"
        "Выбирай, забирай билет, зови друзей.\n\n"
        "Жми кнопку ниже 👇",
        reply_markup=webapp_kb(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Как это работает</b>\n\n"
        "🎟 <b>Гостям</b>\n"
        "Открой приложение, выбери party, забери билет. QR покажешь на входе — "
        "точный адрес придёт сюда незадолго до начала.\n\n"
        "🪩 <b>Организаторам</b>\n"
        "Кнопка «Создать»: своя афиша, free-вход за приведённых друзей, "
        "гостевой список и сканер QR. Платные билеты — через qtickets, "
        "бот сам поймает оплату (подключается в профиле).\n\n"
        "Вопросы — /support",
        reply_markup=webapp_kb(),
    )


@router.message(Command("support"))
async def cmd_support(message: Message) -> None:
    await message.answer(
        "<b>Поддержка</b> 💬\n\n"
        f"Нашёл баг или есть вопрос — пиши {config.SUPPORT_CONTACT}.\n"
        "Постараемся ответить быстро.",
    )


@router.message(Command("app"))
async def cmd_app(message: Message) -> None:
    await message.answer("Все party — внутри 👇", reply_markup=webapp_kb())


# ---------- напоминания ----------

async def send_reminders(bot: Bot) -> None:
    # за сутки: анонс
    for t in db.tickets_for_reminder(24, "rem24_sent"):
        when = dt.datetime.fromtimestamp(t["starts_at"]).strftime("%d.%m в %H:%M")
        try:
            await bot.send_message(
                t["user_id"],
                f"<b>Завтра party!</b> 🎉\n<b>{t['title']}</b>\n{when}, {t['area']}\n\n"
                "Билет — в приложении, вкладка «Билеты».",
                reply_markup=webapp_kb("tickets", "Мой билет 🎟"),
            )
        except Exception as e:
            log.warning("reminder24 to %s failed: %s", t["user_id"], e)
        db.mark_reminded(t["code"], "rem24_sent")

    # незадолго до начала: точный адрес
    for t in db.tickets_for_reminder(config.ADDRESS_REVEAL_HOURS, "rem3_sent"):
        when = dt.datetime.fromtimestamp(t["starts_at"]).strftime("%H:%M")
        addr = t["address"] or t["area"]
        try:
            await bot.send_message(
                t["user_id"],
                f"Сегодня! <b>{t['title']}</b> в {when} 🔥\n"
                f"📍 Адрес: {addr}\n\nПокажи QR-билет на входе. До встречи!",
                reply_markup=webapp_kb("tickets", "Мой билет 🎟"),
            )
        except Exception as e:
            log.warning("reminder3 to %s failed: %s", t["user_id"], e)
        db.mark_reminded(t["code"], "rem3_sent")


# ---------- запуск ----------

async def main() -> None:
    db.init()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    # кнопка меню слева от поля ввода — открывает Mini App
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="party 🎉", web_app=WebAppInfo(url=config.WEBAPP_URL))
    )

    # список команд (показывается по кнопке «/» в чате)
    from aiogram.types import BotCommand
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть party 🎉"),
        BotCommand(command="app", description="Все вечеринки города"),
        BotCommand(command="help", description="Как это работает"),
        BotCommand(command="support", description="Поддержка"),
    ])

    # веб-сервер Mini App + API
    web_app = make_web_app(bot)
    from aiohttp import web

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info("Mini App server on port %s", config.PORT)

    # планировщик: напоминания + опрос оплат qtickets
    from api import poll_qtickets_payments
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_reminders, "interval", minutes=5, args=[bot])
    scheduler.add_job(poll_qtickets_payments, "interval", minutes=2, args=[bot])
    scheduler.start()

    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
