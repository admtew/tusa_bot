"""Точка входа: Telegram-бот + веб-сервер Mini App + напоминания."""
import asyncio
import datetime as dt
import logging
import time

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
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

# куда вёл diolink до показа шлагбаума (открыть после подписки)
_pending_start: dict[int, str] = {}

# anti-spam: throttle команд (user_id -> last_command_time)
_cmd_throttle: dict[int, float] = {}
CMD_COOLDOWN = 0.8  # секунды между командами


def _throttled(user_id: int) -> bool:
    """True если слишком частое нажатие команд."""
    import time
    now = time.time()
    last = _cmd_throttle.get(user_id, 0)
    if now - last < CMD_COOLDOWN:
        return True
    _cmd_throttle[user_id] = now
    return False


def webapp_kb(path: str = "", text: str = "Открыть party 🎉") -> InlineKeyboardMarkup:
    url = config.WEBAPP_URL + (f"#{path}" if path else "")
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))]]
    )


def gate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал",
                              url=f"https://t.me/{config.REQUIRED_CHANNEL}")],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="checksub")],
    ])


async def gate_passed(bot: Bot, user_id: int) -> bool:
    """Доступ только подписчикам обязательного канала."""
    if not config.REQUIRED_CHANNEL:
        return True
    return await check_subscribed(bot, config.REQUIRED_CHANNEL, user_id)


async def send_gate(message: Message) -> None:
    await message.answer(
        "<b>Почти готово!</b> 🔒\n\n"
        f"Чтобы пользоваться AFTERS, подпишись на наш канал "
        f"<a href=\"https://t.me/{config.REQUIRED_CHANNEL}\">@{config.REQUIRED_CHANNEL}</a> — "
        "там все главные вечеринки и анонсы.\n\n"
        "Подпишись и нажми «Я подписался» 👇",
        reply_markup=gate_kb(),
        disable_web_page_preview=True,
    )


# кэш проверки подписки: (channel,user) -> (подписан?, время). Резко снижает
# число обращений к Telegram при наплыве (открытие карточек, рефералы и т.п.).
_subcheck_cache: dict[tuple[str, int], tuple[bool, float]] = {}
_SUBCHECK_TTL = 45  # сек


async def check_subscribed(bot: Bot, channel: str, user_id: int) -> bool:
    """Подписан ли user на @channel. Бот должен быть админом канала.
    Результат кэшируется на ~45с, чтобы не дёргать Telegram на каждый запрос."""
    if not channel:
        return True
    key = (channel, user_id)
    hit = _subcheck_cache.get(key)
    if hit and (time.time() - hit[1]) < _SUBCHECK_TTL:
        return hit[0]
    try:
        member = await bot.get_chat_member(chat_id=f"@{channel}", user_id=user_id)
        ok = member.status in ("member", "administrator", "creator")
        _subcheck_cache[key] = (ok, time.time())
        # подчищаем кэш, если разросся
        if len(_subcheck_cache) > 20000:
            _subcheck_cache.clear()
        return ok
    except Exception as e:  # бот не админ / канал не найден — не блокируем гостя
        log.warning("check_subscribed(%s, %s) failed: %s", channel, user_id, e)
        return True


async def bot_is_admin(bot: Bot, channel: str) -> bool:
    """Является ли бот админом канала (нужно для проверки подписок)."""
    channel = (channel or "").lstrip("@")
    if not channel:
        return False
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id=f"@{channel}", user_id=me.id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        log.info("bot_is_admin(%s) -> no access: %s", channel, e)
        return False


@router.message(CommandStart(deep_link=True))
async def start_deeplink(message: Message, command: CommandObject, bot: Bot) -> None:
    user = message.from_user
    if _throttled(user.id):
        return
    is_new = db.upsert_user(user.id, user.username, user.first_name)
    payload = command.args or ""
    if not await gate_passed(bot, user.id):
        # запомним, куда вёл диплинк, чтобы открыть после подписки
        _pending_start[user.id] = payload
        await send_gate(message)
        return

    # --- старые ссылки-приглашения ref_<event_id>_<...>: просто открываем событие ---
    if payload.startswith("ref_"):
        try:
            event_id = int(payload.split("_", 2)[1])
        except (ValueError, IndexError):
            event_id = 0
        if event_id and db.get_event(event_id):
            await message.answer(
                f"<b>{user.first_name}, тебя зовут на party</b> 🎉\nЖми кнопку — детали внутри.",
                reply_markup=webapp_kb(f"event/{event_id}"))
            return
        await message.answer("Этой party уже нет 😢 Но есть другие 👇", reply_markup=webapp_kb())
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

    # --- ссылка на профиль организатора: org_<id> ---
    if payload.startswith("org_"):
        try:
            org_id = int(payload[4:])
        except ValueError:
            org_id = 0
        if org_id and db.get_user(org_id):
            await message.answer("Профиль организатора 👇", reply_markup=webapp_kb(f"org/{org_id}"))
            return

    await _send_welcome(message)


@router.message(CommandStart())
async def start_plain(message: Message, bot: Bot) -> None:
    if _throttled(message.from_user.id):
        return
    db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if not await gate_passed(bot, message.from_user.id):
        await send_gate(message)
        return
    await _send_welcome(message)


# callback «Я подписался»
@router.callback_query(lambda c: c.data == "checksub")
async def on_checksub(call, bot: Bot) -> None:
    if not await gate_passed(bot, call.from_user.id):
        await call.answer("Пока не вижу подписки. Подпишись и нажми ещё раз 🙏", show_alert=True)
        return
    await call.answer("Готово! Добро пожаловать 🎉")
    try:
        await call.message.delete()
    except Exception:
        pass
    payload = _pending_start.pop(call.from_user.id, "")
    if payload.startswith("ref_") or payload.startswith("evt_"):
        try:
            eid = int(payload.split("_")[-1] if payload.startswith("evt_") else payload.split("_")[1])
            await bot.send_message(call.from_user.id, "Открываю 👇", reply_markup=webapp_kb(f"event/{eid}"))
            return
        except Exception:
            pass
    name = call.from_user.first_name or "Привет"
    await bot.send_message(
        call.from_user.id,
        f"<b>{name}, добро пожаловать в AFTERS</b> 🎉\n\n"
        "Все вечеринки города — в одном месте.\nЖми кнопку ниже 👇",
        reply_markup=webapp_kb(),
    )


async def _send_welcome(message: Message) -> None:
    name = message.from_user.first_name or "Привет"
    await message.answer(
        f"<b>{name}, добро пожаловать в AFTERS</b> 🎉\n"
        "Все вечеринки — в одном месте.",
        reply_markup=webapp_kb(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Нужна помощь?</b> 💬\n\n"
        f"По вопросам по приложению пиши помощнику: {config.SUPPORT_CONTACT}",
    )


@router.message(Command("support"))
async def cmd_support(message: Message) -> None:
    await message.answer(
        "<b>Поддержка</b> 💬\n\n"
        f"Нашёл баг или жалоба — пиши модератору: {config.MODERATOR_CONTACT}",
    )


@router.message(Command("faq"))
async def cmd_faq(message: Message) -> None:
    await message.answer(
        "<b>Частые вопросы</b>\n\n"
        "❓ <b>Как получить билет?</b>\n"
        "Открой приложение, выбери событие, нажми «Купить билет» или «Получить билет» — "
        "билет появится у тебя во вкладке «Билеты».\n\n"
        "❓ <b>Где мой билет?</b>\n"
        "Во вкладке «Билеты» в приложении — там его можно открыть и посмотреть.\n\n"
        "❓ <b>Зачем добавлять данные?</b>\n"
        "Данные нужны для оформления билета и напоминаний.\n\n"
        "🔒 <b>Ваши данные в безопасности</b>\n"
        "Мы не передаём ваши данные третьим лицам. Информация используется "
        "только для работы сервиса."
        + (f"\n\nОстались вопросы? Пиши владельцу: {config.OWNER_CONTACT}"
           if config.OWNER_CONTACT else ""),
    )


@router.message(Command("tickets"))
async def cmd_tickets(message: Message, bot: Bot) -> None:
    if not await gate_passed(bot, message.from_user.id):
        await send_gate(message)
        return
    me = message.from_user.id
    tickets = db.user_tickets(me)
    if not tickets:
        await message.answer("🎟 У тебя пока нет билетов. Загляни в афишу!",
                             reply_markup=webapp_kb())
        return
    lines = ["<b>🎟 Твои билеты:</b>\n"]
    me_username = (await bot.get_me()).username
    for t in tickets:
        f = dt.datetime.fromtimestamp(t["starts_at"])
        when = f.strftime("%d.%m в %H:%M")
        status = ""
        if t["kind"] == "paid_pending":
            status = " ⏳ ожидает подтверждения"
        elif t["status"] == "used":
            status = " ✅ использован"
        link = f"https://t.me/{me_username}?start=evt_{t['event_id']}"
        lines.append(f"• <a href=\"{link}\">{t['title']}</a> — {when}, {t['area'] or 'место уточняется'}{status}")
    lines.append("\nОткрой приложение, чтобы посмотреть билеты 👇")
    await message.answer("\n".join(lines), reply_markup=webapp_kb("tickets", "Мои билеты 🎟"),
                         disable_web_page_preview=True)


@router.message(Command("subscriptions"))
async def cmd_subscriptions(message: Message, bot: Bot) -> None:
    if not await gate_passed(bot, message.from_user.id):
        await send_gate(message)
        return
    me = message.from_user.id
    follows = db.user_follows(me)
    if not follows:
        await message.answer("📢 Ты пока ни на кого не подписан.\n"
                             "Открой профиль организатора в приложении и нажми «Подписаться».",
                             reply_markup=webapp_kb())
        return
    me_username = (await bot.get_me()).username
    lines = ["<b>📢 Твои подписки:</b>\n"]
    for f in follows:
        name = f["first_name"] or "Организатор"
        uname = f"@{f['username']}" if f["username"] else ""
        link = f"https://t.me/{me_username}?start=org_{f['org_id']}"
        lines.append(f"• <a href=\"{link}\">{name}</a> {uname}")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


@router.message(Command("app"))
async def cmd_app(message: Message, bot: Bot) -> None:
    if not await gate_passed(bot, message.from_user.id):
        await send_gate(message)
        return
    await message.answer("Все party — внутри 👇", reply_markup=webapp_kb())


# ---------- модерация (кнопки админа) ----------

@router.callback_query(lambda c: c.data and c.data.startswith(("mod_ok_", "mod_no_")))
async def on_moderation(call, bot: Bot) -> None:
    if call.from_user.id not in config.ADMIN_IDS:
        await call.answer("Только для модераторов", show_alert=True)
        return
    approve = call.data.startswith("mod_ok_")
    try:
        event_id = int(call.data.split("_")[-1])
    except ValueError:
        await call.answer("Битый id")
        return
    event = db.get_event(event_id)
    if not event:
        await call.answer("Событие не найдено")
        return
    if event["status"] != "pending":  # уже обработал другой модератор
        await call.answer("Уже обработано другим модератором", show_alert=True)
        return
    db.set_event_status(event_id, "active" if approve else "rejected")
    if approve:
        from api import notify_followers
        await notify_followers(bot, db.get_event(event_id))
    # уведомляем организатора
    try:
        if approve:
            await bot.send_message(
                event["org_id"],
                f"✅ Твоя party «{event['title']}» одобрена и опубликована в афише!",
                reply_markup=webapp_kb(f"event/{event_id}"),
            )
        else:
            await bot.send_message(
                event["org_id"],
                f"🚫 Party «{event['title']}» не прошла модерацию.\n"
                f"Вопросы — {config.SUPPORT_CONTACT}",
            )
    except Exception:
        pass
    mark = "✅ Одобрено" if approve else "🚫 Отклонено"
    try:
        await call.message.edit_text(f"{call.message.text}\n\n<b>{mark}</b>")
    except Exception:
        pass
    await call.answer(mark)


# ---------- ручной флоу билетов: одобрить/отклонить гостя (этап 2) ----------

@router.callback_query(lambda c: c.data and c.data.startswith(("tk_ok_", "tk_no_")))
async def on_ticket_decision(call, bot: Bot) -> None:
    approve = call.data.startswith("tk_ok_")
    code = call.data[6:]
    t = db.get_ticket(code)
    if not t:
        await call.answer("Заявка не найдена")
        return
    if t["org_id"] != call.from_user.id and call.from_user.id not in config.ADMIN_IDS:
        await call.answer("Это не твоё событие", show_alert=True)
        return
    if t["kind"] != "paid_pending":  # уже обработано
        await call.answer("Заявка уже обработана", show_alert=True)
        return
    if approve:
        db.approve_ticket(code)
        try:
            await bot.send_message(
                t["user_id"],
                f"✅ Вы идёте на «{t['title']}»! Билет — во вкладке «Билеты».",
                reply_markup=webapp_kb(f"event/{t['event_id']}", "Открыть событие 🎉"),
            )
        except Exception:
            pass
        mark = "✅ Гость одобрен"
    else:
        db.reject_ticket(code)  # тишина для гостя (по ТЗ)
        mark = "🚫 Отклонено (гостю не сообщаем)"
    try:
        cap = call.message.caption or call.message.text or ""
        if call.message.caption is not None:
            await call.message.edit_caption(caption=f"{cap}\n\n<b>{mark}</b>")
        else:
            await call.message.edit_text(f"{cap}\n\n<b>{mark}</b>")
    except Exception:
        pass
    await call.answer(mark)


# ---------- админ: верификация организатора (этап 1/5) ----------

@router.callback_query(lambda c: c.data and c.data.startswith(("vrf_ok_", "vrf_no_")))
async def on_verify_request(call, bot: Bot) -> None:
    if call.from_user.id not in config.ADMIN_IDS:
        await call.answer("Только для модераторов", show_alert=True)
        return
    approve = call.data.startswith("vrf_ok_")
    try:
        uid = int(call.data.split("_")[-1])
    except ValueError:
        await call.answer("Битый id")
        return
    if db.is_verified(uid):  # другой модератор уже выдал
        await call.answer("Уже выдано другим модератором", show_alert=True)
        return
    if approve:
        db.upsert_user(uid, None, None)
        db.set_verified(uid, True)
        try:
            await bot.send_message(uid, "✅ Тебе выдали галочку проверенного организатора! Твои события теперь публикуются сразу.")
        except Exception:
            pass
        mark = "✅ Галочка выдана"
    else:
        mark = "🚫 Отказано"
    try:
        await call.message.edit_text(f"{call.message.text}\n\n<b>{mark}</b>")
    except Exception:
        pass
    await call.answer(mark)


@router.message(Command("verify"))
async def cmd_verify(message: Message) -> None:
    if message.from_user.id not in config.ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Использование: <code>/verify &lt;tg_id&gt;</code> — выдать галочку доверия организатору.")
        return
    uid = int(parts[1])
    db.upsert_user(uid, None, None)
    db.set_verified(uid, True)
    await message.answer(f"✅ Организатор {uid} верифицирован — его события публикуются без модерации.")
    try:
        await message.bot.send_message(uid, "✅ Тебе выдали галочку доверия! Твои события теперь публикуются сразу.")
    except Exception:
        pass


@router.message(Command("unverify"))
async def cmd_unverify(message: Message) -> None:
    if message.from_user.id not in config.ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return
    db.set_verified(int(parts[1]), False)
    await message.answer(f"Галочка у {parts[1]} снята.")


# ---------- платное промо (только админ) ----------

@router.message(Command("feature"))
async def cmd_feature(message: Message) -> None:
    if message.from_user.id not in config.ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/feature ID [дней]</code> — поднять событие в топ афиши.")
        return
    eid = int(parts[1])
    days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 7
    e = db.get_event(eid)
    if not e:
        await message.answer("Событие не найдено.")
        return
    db.set_featured(eid, db.now() + days * 86400)
    await message.answer(f"🔥 «{e['title']}» в топе афиши на {days} дн.")
    try:
        await message.bot.send_message(
            e["org_id"], f"🔥 Твоё событие «{e['title']}» подняли в топ афиши на {days} дн!")
    except Exception:
        pass


@router.message(Command("unfeature"))
async def cmd_unfeature(message: Message) -> None:
    if message.from_user.id not in config.ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return
    db.set_featured(int(parts[1]), 0)
    await message.answer("Промо снято.")


@router.message(Command("promo"))
async def cmd_promo(message: Message, bot: Bot) -> None:
    """Промо-пуш по ВСЕМ пользователям бота (платный продукт)."""
    if message.from_user.id not in config.ADMIN_IDS:
        return
    text = (message.text or "")[len("/promo"):].strip()
    if not text and message.reply_to_message:
        text = message.reply_to_message.html_text or message.reply_to_message.text or ""
    if not text:
        await message.answer("Использование: <code>/promo текст</code> (или ответом на сообщение) — рассылка всем.")
        return
    from notify import broadcast
    ids = db.all_user_ids()
    await message.answer(f"📣 Шлю промо {len(ids)} пользователям…")
    sent = await broadcast(bot, ids, text)
    await message.answer(f"Готово: доставлено {sent} из {len(ids)}.")


# ---------- напоминания ----------

async def send_reminders(bot: Bot) -> None:
    # за 5 часов: напоминание с адресом (rem24_sent переиспользуем как «5ч отправлено»)
    for t in db.tickets_for_reminder(5, "rem24_sent"):
        when = dt.datetime.fromtimestamp(t["starts_at"]).strftime("%H:%M")
        addr = t["address"] or t["area"]
        try:
            await bot.send_message(
                t["user_id"],
                f"<b>Сегодня — {t['title']}</b> в {when} 🎉\n"
                f"📍 Адрес: {addr}\n\nТы идёшь — до встречи!",
                reply_markup=webapp_kb("tickets", "Открыть 🎟"),
            )
        except Exception as e:
            log.warning("reminder5 to %s failed: %s", t["user_id"], e)
        db.mark_reminded(t["code"], "rem24_sent")

    # за 2 часа: финальное напоминание с адресом (rem3_sent как «2ч отправлено»)
    for t in db.tickets_for_reminder(2, "rem3_sent"):
        when = dt.datetime.fromtimestamp(t["starts_at"]).strftime("%H:%M")
        addr = t["address"] or t["area"]
        try:
            await bot.send_message(
                t["user_id"],
                f"Через пару часов начинаем! <b>{t['title']}</b> в {when} 🔥\n"
                f"📍 {addr}",
                reply_markup=webapp_kb("tickets", "Открыть 🎟"),
            )
        except Exception as e:
            log.warning("reminder2 to %s failed: %s", t["user_id"], e)
        db.mark_reminded(t["code"], "rem3_sent")


# ---------- запуск ----------

async def main() -> None:
    db.init()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    # меню-кнопка = список команд (а не Mini App), иначе команды прячутся.
    # Само приложение открывается кнопкой в сообщениях.
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    # список команд (виден по кнопке «/» / «Меню» в чате)
    await bot.set_my_commands([
        BotCommand(command="start", description="🎉 Открыть AFTERS"),
        BotCommand(command="app", description="🎟 Все вечеринки города"),
        BotCommand(command="tickets", description="🎫 Мои билеты"),
        BotCommand(command="subscriptions", description="📢 Мои подписки"),
        BotCommand(command="faq", description="❓ Частые вопросы"),
        BotCommand(command="help", description="💬 Связаться с админом"),
        BotCommand(command="support", description="🛠 Поддержка"),
    ])

    # веб-сервер Mini App + API
    web_app = make_web_app(bot)
    from aiohttp import web

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info("Mini App server on port %s", config.PORT)

    # планировщик: напоминания + авто-перевод прошедших в past
    from api import _rate, _rate_write
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_reminders, "interval", minutes=5, args=[bot])
    scheduler.add_job(lambda: db.mark_past_events(), "interval", minutes=10)
    scheduler.add_job(lambda: db.purge_old_proofs(), "interval", hours=6)  # приватность: чистим скрины
    scheduler.add_job(lambda: (_rate.cleanup(), _rate_write.cleanup()), "interval", minutes=10)
    scheduler.start()
    db.mark_past_events()  # разово на старте
    db.purge_old_proofs()

    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
