from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault, CallbackQuery, LabeledPrice, Message,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import admin
import db
from config import (
    ADMIN_IDS, BOT_DESCRIPTION, BOT_SHORT_DESCRIPTION, BOT_TOKEN, DAY_MS, DB_PATH, REF_BONUS_DAYS,
    REF_FRIEND_DAYS, TRIAL_DAYS, YOOKASSA_TOKEN,
)
from services import (
    extend_access, fmt_date, get_plans, key_text, log, notify_admins, now_ms, reward_referrer,
    send_safe, xui,
)
from xui import XUIError

router = Router()

WELCOME = (
    "👋 Привет! Это <b>KalibVPN</b> — быстрый VPN, который работает там, где другие блокируют.\n\n"
    "Выберите, что сделать:"
)
HELP_TEXT = (
    "<b>Как подключиться</b>\n\n"
    "1. Установите приложение:\n"
    "   • iPhone — V2Box, v2RayTun или Happ (если в российском App Store их нет — "
    "нужен Apple ID другой страны);\n"
    "   • Android — v2RayTun, Hiddify или v2rayNG;\n"
    "   • компьютер — Hiddify или v2rayN.\n"
    "2. Нажмите «🔑 Мой ключ» и скопируйте ключ.\n"
    "3. В приложении нажмите «+» → импорт из буфера обмена.\n"
    "4. Выберите конфиг и нажмите «Подключиться».\n\n"
    "После продления ключ не меняется — ничего переустанавливать не нужно."
)
PANEL_DOWN = "Сервер временно недоступен, попробуйте позже"


# ---------- клавиатуры ----------

async def main_kb(tg_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 Мой ключ", callback_data="key")
    kb.button(text="💳 Купить / продлить", callback_data="buy")
    user = await db.get_user(tg_id)
    if TRIAL_DAYS > 0 and user and not user["trial_used"] and not user["uuid"]:
        kb.button(text=f"🎁 Пробный период {TRIAL_DAYS} дн.", callback_data="trial")
    kb.button(text="👥 Пригласить друга", callback_data="ref")
    kb.button(text="📖 Как подключиться", callback_data="help")
    if tg_id in ADMIN_IDS:
        kb.button(text="🛠 Админка", callback_data="adm:menu")
    kb.adjust(1)
    return kb.as_markup()


def plans_kb(plans: dict):
    kb = InlineKeyboardBuilder()
    for pid, p in plans.items():
        price = f"{p['rub']} ₽ / {p['stars']}⭐"
        kb.button(text=f"{p['title']} — {price}", callback_data=f"plan:{pid}")
    kb.button(text="« Назад", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def pay_methods_kb(pid: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Telegram Stars", callback_data=f"pay:stars:{pid}")
    if YOOKASSA_TOKEN:
        kb.button(text="💳 Картой (ЮKassa)", callback_data=f"pay:rub:{pid}")
    kb.button(text="« Назад", callback_data="buy")
    kb.adjust(1)
    return kb.as_markup()


# ---------- экраны, общие для команд и кнопок ----------

async def show_key(target: Message, tg_id: int) -> None:
    try:
        text = await key_text(tg_id)
    except (XUIError, OSError):
        log.exception("panel error")
        await target.answer(PANEL_DOWN)
        return
    if not text:
        kb = InlineKeyboardBuilder()
        kb.button(text="💳 Купить", callback_data="buy")
        await target.answer("У вас пока нет ключа — оформите подписку.", reply_markup=kb.as_markup())
        return
    await target.answer(text, disable_web_page_preview=True)


async def show_ref(target: Message, bot: Bot, tg_id: int) -> None:
    me = await bot.me()
    stats = await db.referral_stats(tg_id)
    link = f"https://t.me/{me.username}?start=ref{tg_id}"
    lines = [
        "<b>👥 Приглашайте друзей — получайте дни бесплатно</b>\n",
        f"За каждого друга, который оплатит подписку, вы получите <b>+{REF_BONUS_DAYS} дн.</b>",
    ]
    if REF_FRIEND_DAYS:
        lines.append(f"А друг получит <b>+{REF_FRIEND_DAYS} дн.</b> к первой покупке.")
    lines += [
        f"\nВаша ссылка:\n<code>{link}</code>\n",
        f"Приглашено: {stats['invited']}",
        f"Оплатили: {stats['paid']}",
        f"Получено бонусов: {stats['bonus_days']} дн.",
    ]
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Поделиться ссылкой", url=f"https://t.me/share/url?url={link}&text=Быстрый%20VPN%20—%20попробуй")
    await target.answer("\n".join(lines), reply_markup=kb.as_markup(), disable_web_page_preview=True)


# ---------- хендлеры ----------

@router.message(CommandStart())
async def cmd_start(msg: Message, command: CommandObject):
    tg_id = msg.from_user.id
    is_new = await db.touch_user(tg_id, msg.from_user.username)
    args = command.args or ""
    if is_new and args.startswith("ref") and args[3:].isdigit():
        referrer_id = int(args[3:])
        if referrer_id != tg_id and await db.get_user(referrer_id):
            await db.set_referrer(tg_id, referrer_id)
            await send_safe(
                msg.bot, referrer_id,
                f"👥 По вашей ссылке пришёл новый пользователь! Когда он оплатит подписку, вы получите +{REF_BONUS_DAYS} дн.",
            )
    await msg.answer(WELCOME, reply_markup=await main_kb(tg_id))


@router.callback_query(F.data == "menu")
async def cb_menu(cq: CallbackQuery):
    await cq.message.edit_text(WELCOME, reply_markup=await main_kb(cq.from_user.id))


@router.callback_query(F.data == "key")
async def cb_key(cq: CallbackQuery):
    await cq.answer()
    await show_key(cq.message, cq.from_user.id)


@router.message(Command("key"))
async def cmd_key(msg: Message):
    await show_key(msg, msg.from_user.id)


@router.callback_query(F.data == "ref")
async def cb_ref(cq: CallbackQuery):
    await cq.answer()
    await show_ref(cq.message, cq.bot, cq.from_user.id)


@router.message(Command("ref"))
async def cmd_ref(msg: Message):
    await db.touch_user(msg.from_user.id, msg.from_user.username)
    await show_ref(msg, msg.bot, msg.from_user.id)


@router.callback_query(F.data == "help")
async def cb_help(cq: CallbackQuery):
    await cq.answer()
    await cq.message.answer(HELP_TEXT)


@router.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(HELP_TEXT)


@router.callback_query(F.data == "trial")
async def cb_trial(cq: CallbackQuery):
    user = await db.get_user(cq.from_user.id)
    if TRIAL_DAYS <= 0 or not user or user["trial_used"] or user["uuid"]:
        await cq.answer("Пробный период недоступен", show_alert=True)
        return
    try:
        await extend_access(cq.from_user.id, TRIAL_DAYS)
    except (XUIError, OSError):
        log.exception("panel error")
        await cq.answer(PANEL_DOWN, show_alert=True)
        return
    await db.set_trial_used(cq.from_user.id)
    await cq.answer()
    await cq.message.answer(f"🎁 Пробный период на {TRIAL_DAYS} дн. активирован!")
    await show_key(cq.message, cq.from_user.id)


@router.callback_query(F.data == "buy")
async def cb_buy(cq: CallbackQuery):
    text = "Выберите тариф:"
    user = await db.get_user(cq.from_user.id)
    if REF_FRIEND_DAYS and user and user["referrer_id"] and not await db.payments_count(cq.from_user.id):
        text += f"\n\n🎁 Вас пригласил друг — к первой покупке добавим +{REF_FRIEND_DAYS} дн."
    await cq.message.edit_text(text, reply_markup=plans_kb(await get_plans()))


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(cq: CallbackQuery):
    pid = cq.data.split(":", 1)[1]
    plans = await get_plans()
    if pid not in plans:
        await cq.answer("Тариф больше недоступен", show_alert=True)
        return
    await cq.message.edit_text(f"Тариф: <b>{plans[pid]['title']}</b>\nСпособ оплаты:", reply_markup=pay_methods_kb(pid))


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(cq: CallbackQuery):
    _, method, pid = cq.data.split(":")
    plan = (await get_plans()).get(pid)
    if not plan or (method == "rub" and not YOOKASSA_TOKEN):
        await cq.answer("Тариф больше недоступен", show_alert=True)
        return
    title = f"VPN — {plan['title']}"
    description = f"Доступ к VPN на {plan['days']} дней"
    # Дни кладём в payload, чтобы смена тарифов в админке не повлияла на уже выставленные счета
    payload = f"{pid}:{plan['days']}:{cq.from_user.id}"
    if method == "stars":
        await cq.message.answer_invoice(
            title=title, description=description, payload=payload,
            currency="XTR", prices=[LabeledPrice(label=title, amount=plan["stars"])],
        )
    else:
        await cq.message.answer_invoice(
            title=title, description=description, payload=payload,
            currency="RUB", prices=[LabeledPrice(label=title, amount=plan["rub"] * 100)],
            provider_token=YOOKASSA_TOKEN,
        )
    await cq.answer()


@router.pre_checkout_query()
async def pre_checkout(pcq: PreCheckoutQuery):
    parts = pcq.invoice_payload.split(":")
    if len(parts) == 3 and parts[1].isdigit():
        await pcq.answer(ok=True)
    else:
        await pcq.answer(ok=False, error_message="Счёт устарел, откройте тарифы заново")


@router.message(F.successful_payment)
async def on_payment(msg: Message):
    sp = msg.successful_payment
    pid, days, _ = sp.invoice_payload.split(":")
    days = int(days)
    tg_id = msg.from_user.id
    user = await db.get_user(tg_id)
    first_payment = await db.payments_count(tg_id) == 0

    if not await db.add_payment(sp.telegram_payment_charge_id, tg_id, pid, sp.total_amount, sp.currency):
        return  # этот платёж уже обработан

    bonus = REF_FRIEND_DAYS if first_payment and user and user["referrer_id"] else 0
    try:
        await extend_access(tg_id, days + bonus)
    except Exception:
        log.exception("failed to extend after payment")
        await msg.answer("Оплата получена, но выдать ключ не удалось. Администратор уже уведомлён и всё исправит.")
        await notify_admins(
            msg.bot,
            f"⚠️ Оплата прошла, но доступ не выдан!\nuser: {tg_id} @{msg.from_user.username}\n"
            f"тариф: {pid}, {sp.total_amount} {sp.currency}\ncharge: {sp.telegram_payment_charge_id}\n"
            f"Выдать вручную: /give {tg_id} {days + bonus}",
        )
        return

    text = f"✅ Оплата получена! Доступ продлён на {days} дн."
    if bonus:
        text += f"\n🎁 +{bonus} дн. в подарок за приглашение."
    await msg.answer(text)
    await show_key(msg, tg_id)
    amount = sp.total_amount / 100 if sp.currency == "RUB" else sp.total_amount
    await notify_admins(msg.bot, f"💰 {amount:g} {sp.currency} — {pid} ({days} дн.) от {tg_id} @{msg.from_user.username}")
    if first_payment:
        await reward_referrer(msg.bot, tg_id)


# ---------- напоминания об окончании ----------

async def expiry_notifier(bot: Bot) -> None:
    while True:
        try:
            by_email = {c["email"]: c for c in await xui.list_clients()}
            now = now_ms()
            for user in await db.users_with_clients():
                exp = (by_email.get(user["email"]) or {}).get("expiryTime") or 0
                if exp <= 0 or user["notified_expiry"] == exp:
                    continue
                if now < exp <= now + 3 * DAY_MS:
                    kb = InlineKeyboardBuilder()
                    kb.button(text="💳 Продлить", callback_data="buy")
                    await send_safe(
                        bot, user["tg_id"],
                        f"⏰ Ваш доступ к VPN закончится {fmt_date(exp)}. Продлите, чтобы не потерять связь.",
                        reply_markup=kb.as_markup(),
                    )
                    await db.set_notified(user["tg_id"], exp)
        except Exception:
            log.exception("expiry notifier failed")
        await asyncio.sleep(3600)


async def setup_profile(bot: Bot) -> None:
    """Описание бота, короткое описание и меню команд."""
    try:
        await bot.set_my_description(BOT_DESCRIPTION)
        await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
        user_commands = [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="key", description="Мой ключ"),
            BotCommand(command="ref", description="Пригласить друга"),
            BotCommand(command="help", description="Как подключиться"),
        ]
        await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
        for admin_id in ADMIN_IDS:
            await bot.set_my_commands(
                user_commands + [BotCommand(command="admin", description="Админ-панель")],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
    except TelegramAPIError:
        log.exception("failed to set bot profile")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await db.init(DB_PATH)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(admin.router)
    dp.include_router(router)
    await setup_profile(bot)
    notifier = asyncio.create_task(expiry_notifier(bot))
    try:
        await dp.start_polling(bot)
    finally:
        # Без закрытия базы поток aiosqlite не даёт процессу завершиться при остановке
        notifier.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
