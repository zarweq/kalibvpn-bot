"""Админ-панель в Telegram: /admin."""
from __future__ import annotations

import asyncio
import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
from config import ADMIN_IDS, MASK_PRESETS
from services import (
    broadcast, check_mask_domain, current_mask, email_for, extend_access, fmt_date, get_plans,
    is_active, key_text, log, save_plans, send_safe, set_enabled, set_mask, status_line, xui,
)

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

PAGE_SIZE = 8


class AdminSG(StatesGroup):
    find = State()
    custom_days = State()
    broadcast = State()
    price = State()
    mask_custom = State()


def kb_of(*rows: tuple[str, str], width: int = 1):
    kb = InlineKeyboardBuilder()
    for text, data in rows:
        kb.button(text=text, callback_data=data)
    kb.adjust(width)
    return kb.as_markup()


BACK = ("« В админку", "adm:menu")


def user_title(user) -> str:
    return f"@{user['username']}" if user["username"] else str(user["tg_id"])


def money(rows) -> str:
    if not rows:
        return "—"
    parts = []
    for row in rows:
        total = row["total"] / 100 if row["currency"] == "RUB" else row["total"]
        sign = "₽" if row["currency"] == "RUB" else "⭐"
        parts.append(f"{total:g}{sign} ({row['cnt']} опл.)")
    return ", ".join(parts)


# ---------- меню ----------

MENU_TEXT = "🛠 <b>Админ-панель</b>"
MENU_KB = kb_of(
    ("📊 Статистика", "adm:stats"),
    ("👥 Пользователи", "adm:users:0"),
    ("🔍 Найти пользователя", "adm:find"),
    ("📢 Рассылка", "adm:bc"),
    ("💰 Цены", "adm:prices"),
    ("🎭 Маскировка", "adm:mask"),
    width=2,
)


@router.message(Command("admin"))
async def cmd_admin(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(MENU_TEXT, reply_markup=MENU_KB)


@router.callback_query(F.data == "adm:menu")
async def cb_menu(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.answer()
    await cq.message.edit_text(MENU_TEXT, reply_markup=MENU_KB)


@router.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Отменено.", reply_markup=MENU_KB)


# ---------- статистика ----------

@router.callback_query(F.data == "adm:stats")
async def cb_stats(cq: CallbackQuery):
    await cq.answer()
    s = await db.stats()
    u = s["users"]
    try:
        clients = [c for c in await xui.list_clients() if str(c.get("email", "")).startswith("tg")]
        active = sum(1 for c in clients if is_active(c))
        keys = f"{active} активных из {len(clients)}"
    except Exception:
        log.exception("panel error")
        keys = "панель недоступна"
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👤 Пользователей: <b>{u['total']}</b>\n"
        f"   новых сегодня: {u['today'] or 0}, за 7 дней: {u['week'] or 0}\n"
        f"🔑 Ключей бота: {keys}\n"
        f"🎁 Взяли пробный период: {u['trials'] or 0}\n"
        f"👥 Пришли по рефералке: {u['referred'] or 0}\n\n"
        "💰 <b>Выручка</b>\n"
        f"Сегодня: {money(s['revenue']['today'])}\n"
        f"30 дней: {money(s['revenue']['month'])}\n"
        f"Всего: {money(s['revenue']['all'])}"
    )
    await cq.message.edit_text(text, reply_markup=kb_of(("🔄 Обновить", "adm:stats"), BACK))


# ---------- пользователи ----------

@router.callback_query(F.data.startswith("adm:users:"))
async def cb_users(cq: CallbackQuery):
    await cq.answer()
    page = int(cq.data.rsplit(":", 1)[1])
    total = await db.count_users()
    users = await db.list_users(page * PAGE_SIZE, PAGE_SIZE)
    try:
        by_email = {c["email"]: c for c in await xui.list_clients()}
    except Exception:
        by_email = {}

    kb = InlineKeyboardBuilder()
    for user in users:
        record = by_email.get(user["email"]) if user["email"] else None
        mark = "✅" if is_active(record) else ("❌" if record else "▫️")
        kb.button(text=f"{mark} {user_title(user)}", callback_data=f"adm:u:{user['tg_id']}")
    kb.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=f"adm:users:{page - 1}")
    if (page + 1) * PAGE_SIZE < total:
        nav.button(text="▶️", callback_data=f"adm:users:{page + 1}")
    kb.attach(nav)
    kb.row(InlineKeyboardButton(text=BACK[0], callback_data=BACK[1]))
    pages = max(1, -(-total // PAGE_SIZE))
    await cq.message.edit_text(
        f"👥 <b>Пользователи</b> ({total}) — стр. {page + 1}/{pages}\n✅ активен  ❌ истёк/отключён  ▫️ без ключа",
        reply_markup=kb.as_markup(),
    )


async def user_card(tg_id: int) -> tuple[str, object]:
    user = await db.get_user(tg_id)
    if not user:
        return "Пользователь не найден.", kb_of(BACK)
    try:
        record = await xui.get_client(email_for(tg_id))
        status = status_line(record)
    except Exception:
        record, status = None, "панель недоступна"
    ref = await db.referral_stats(tg_id)
    paid = money(await db.user_payments(tg_id))
    referrer = user["referrer_id"]
    text = (
        f"👤 <b>{html.escape(user_title(user))}</b>\n"
        f"ID: <code>{tg_id}</code>\n"
        f"Статус: {status}\n"
        f"Пробный период: {'использован' if user['trial_used'] else 'нет'}\n"
        f"Оплаты: {paid}\n"
        f"Пригласил его: {referrer or '—'}\n"
        f"Он пригласил: {ref['invited']} (оплатили {ref['paid']}), бонусов {ref['bonus_days']} дн.\n"
        f"В боте с {fmt_date(user['created_at'] * 1000)}"
    )
    kb = InlineKeyboardBuilder()
    for days in (7, 30, 90):
        kb.button(text=f"+{days} дн.", callback_data=f"adm:add:{tg_id}:{days}")
    kb.button(text="✏️ Своё число дней", callback_data=f"adm:days:{tg_id}")
    if record:
        if record.get("enable", True):
            kb.button(text="⛔ Отключить", callback_data=f"adm:tog:{tg_id}:0")
        else:
            kb.button(text="✅ Включить", callback_data=f"adm:tog:{tg_id}:1")
        kb.button(text="🔑 Отправить ключ", callback_data=f"adm:send:{tg_id}")
    kb.button(text="👥 К списку", callback_data="adm:users:0")
    kb.button(text=BACK[0], callback_data=BACK[1])
    kb.adjust(3, 1, 2, 2)
    return text, kb.as_markup()


@router.callback_query(F.data.startswith("adm:u:"))
async def cb_user(cq: CallbackQuery):
    await cq.answer()
    text, markup = await user_card(int(cq.data.rsplit(":", 1)[1]))
    await cq.message.edit_text(text, reply_markup=markup)


async def give_days(bot, tg_id: int, days: int) -> str:
    record = await extend_access(tg_id, days)
    key = await key_text(tg_id)
    sent = await send_safe(bot, tg_id, f"🎁 Вам начислено +{days} дн. доступа.\n\n{key}", disable_web_page_preview=True)
    note = "" if sent else "\n(сообщение пользователю не доставлено)"
    return f"✅ +{days} дн. Активен до {fmt_date(record['expiryTime'])}{note}"


@router.callback_query(F.data.startswith("adm:add:"))
async def cb_add(cq: CallbackQuery):
    _, _, tg_id, days = cq.data.split(":")
    try:
        result = await give_days(cq.bot, int(tg_id), int(days))
    except Exception:
        log.exception("give days failed")
        await cq.answer("Ошибка панели, попробуйте позже", show_alert=True)
        return
    await cq.answer(result, show_alert=True)
    text, markup = await user_card(int(tg_id))
    await cq.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("adm:days:"))
async def cb_days(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await state.set_state(AdminSG.custom_days)
    await state.update_data(uid=int(cq.data.rsplit(":", 1)[1]))
    await cq.message.answer("Сколько дней начислить? Отправьте число (/cancel — отмена).")


@router.message(AdminSG.custom_days, ~F.text.startswith("/"))
async def st_days(msg: Message, state: FSMContext):
    if not (msg.text or "").strip().isdigit() or not 0 < int(msg.text) <= 3650:
        await msg.answer("Нужно число от 1 до 3650.")
        return
    uid = (await state.get_data())["uid"]
    await state.clear()
    try:
        await msg.answer(await give_days(msg.bot, uid, int(msg.text)))
    except Exception:
        log.exception("give days failed")
        await msg.answer("Ошибка панели, попробуйте позже.")
        return
    text, markup = await user_card(uid)
    await msg.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("adm:tog:"))
async def cb_toggle(cq: CallbackQuery):
    _, _, tg_id, enable = cq.data.split(":")
    tg_id, enable = int(tg_id), enable == "1"
    try:
        await set_enabled(tg_id, enable)
    except Exception:
        log.exception("toggle failed")
        await cq.answer("Ошибка панели, попробуйте позже", show_alert=True)
        return
    await cq.answer("Включён" if enable else "Отключён")
    text, markup = await user_card(tg_id)
    await cq.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("adm:send:"))
async def cb_send_key(cq: CallbackQuery):
    tg_id = int(cq.data.rsplit(":", 1)[1])
    key = await key_text(tg_id)
    ok = bool(key) and await send_safe(cq.bot, tg_id, key, disable_web_page_preview=True)
    await cq.answer("Ключ отправлен" if ok else "Не удалось отправить", show_alert=True)


@router.message(Command("give"))
async def cmd_give(msg: Message, command: CommandObject):
    try:
        tg_id, days = map(int, (command.args or "").split())
    except ValueError:
        await msg.answer("Использование: /give <tg_id> <дней>")
        return
    await msg.answer(await give_days(msg.bot, tg_id, days))


# ---------- поиск ----------

@router.callback_query(F.data == "adm:find")
async def cb_find(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await state.set_state(AdminSG.find)
    await cq.message.answer("Отправьте Telegram ID или @username (/cancel — отмена).")


@router.message(AdminSG.find, ~F.text.startswith("/"))
async def st_find(msg: Message, state: FSMContext):
    users = await db.find_users(msg.text or "")
    if not users:
        await msg.answer("Никого не нашёл. Попробуйте ещё раз или /cancel.")
        return
    await state.clear()
    if len(users) == 1:
        text, markup = await user_card(users[0]["tg_id"])
        await msg.answer(text, reply_markup=markup)
        return
    await msg.answer(
        "Найдено несколько:",
        reply_markup=kb_of(*[(user_title(u), f"adm:u:{u['tg_id']}") for u in users], BACK),
    )


# ---------- рассылка ----------

@router.callback_query(F.data == "adm:bc")
async def cb_broadcast(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await state.set_state(AdminSG.broadcast)
    await cq.message.answer(
        "Пришлите сообщение для рассылки — текст, фото, видео, что угодно. "
        "Я покажу, как оно будет выглядеть, и спрошу подтверждение. /cancel — отмена."
    )


@router.message(AdminSG.broadcast, ~F.text.startswith("/"))
async def st_broadcast(msg: Message, state: FSMContext):
    await state.update_data(chat_id=msg.chat.id, message_id=msg.message_id)
    total = await db.count_users()
    await msg.answer("👆 Так увидят пользователи.")
    await msg.answer(
        f"Отправить всем ({total} чел.)?",
        reply_markup=kb_of(("✅ Отправить", "adm:bc:go"), ("❌ Отмена", "adm:menu"), width=2),
    )


@router.callback_query(F.data == "adm:bc:go")
async def cb_broadcast_go(cq: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if "message_id" not in data:
        await cq.answer("Сообщение не найдено, начните заново", show_alert=True)
        return
    await cq.answer()
    await cq.message.edit_text("📢 Рассылка запущена, пришлю отчёт по окончании.")
    user_ids = await db.all_user_ids()

    async def run():
        ok, fail = await broadcast(
            user_ids, lambda uid: cq.bot.copy_message(uid, data["chat_id"], data["message_id"])
        )
        await send_safe(cq.bot, cq.from_user.id, f"📢 Рассылка завершена: доставлено {ok}, не доставлено {fail}.")

    asyncio.create_task(run())


# ---------- цены ----------

def prices_text(plans: dict) -> str:
    lines = ["💰 <b>Тарифы</b>\n"]
    for p in plans.values():
        lines.append(f"• {p['title']} ({p['days']} дн.): {p['stars']}⭐ / {p['rub']}₽")
    lines.append("\nВыберите тариф, чтобы изменить цену.")
    return "\n".join(lines)


@router.callback_query(F.data == "adm:prices")
async def cb_prices(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await state.clear()
    plans = await get_plans()
    await cq.message.edit_text(
        prices_text(plans),
        reply_markup=kb_of(*[(p["title"], f"adm:price:{pid}") for pid, p in plans.items()], BACK),
    )


@router.callback_query(F.data.startswith("adm:price:"))
async def cb_price(cq: CallbackQuery, state: FSMContext):
    pid = cq.data.rsplit(":", 1)[1]
    plan = (await get_plans()).get(pid)
    if not plan:
        await cq.answer()
        return
    await cq.answer()
    await state.set_state(AdminSG.price)
    await state.update_data(pid=pid)
    await cq.message.answer(
        f"Тариф «{plan['title']}»: сейчас {plan['stars']}⭐ и {plan['rub']}₽.\n"
        "Отправьте новую цену двумя числами: <code>звёзды рубли</code>, например <code>200 199</code>.\n/cancel — отмена."
    )


@router.message(AdminSG.price, ~F.text.startswith("/"))
async def st_price(msg: Message, state: FSMContext):
    parts = (msg.text or "").split()
    if len(parts) != 2 or not all(p.isdigit() and int(p) > 0 for p in parts):
        await msg.answer("Нужно два положительных числа: звёзды и рубли. Например: <code>200 199</code>")
        return
    pid = (await state.get_data())["pid"]
    await state.clear()
    plans = await get_plans()
    plans[pid]["stars"], plans[pid]["rub"] = int(parts[0]), int(parts[1])
    await save_plans(plans)
    await msg.answer(
        "✅ Цена обновлена.\n\n" + prices_text(plans),
        reply_markup=kb_of(*[(p["title"], f"adm:price:{k}") for k, p in plans.items()], BACK),
    )


# ---------- маскировка ----------

@router.callback_query(F.data == "adm:mask")
async def cb_mask(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await state.clear()
    try:
        inbounds = await xui.reality_inbounds()
    except Exception:
        log.exception("panel error")
        await cq.message.edit_text("Панель недоступна.", reply_markup=kb_of(BACK))
        return
    lines = ["🎭 <b>Маскировка Reality</b>\n", "Под какой сайт маскируется трафик каждого подключения:\n"]
    rows = []
    for ib in inbounds:
        mask = current_mask(ib)
        lines.append(f"• <b>{html.escape(ib['remark'])}</b> (порт {ib['port']}): {mask}")
        rows.append((f"{ib['remark']} — сменить", f"adm:mask:{ib['id']}"))
    lines.append("\n⚠️ После смены маскировки старые ключи перестают работать — бот предложит разослать новые.")
    await cq.message.edit_text("\n".join(lines), reply_markup=kb_of(*rows, BACK))


@router.callback_query(F.data.startswith("adm:mask:"))
async def cb_mask_inbound(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    iid = int(cq.data.rsplit(":", 1)[1])
    await state.update_data(iid=iid)
    kb = InlineKeyboardBuilder()
    for i, domain in enumerate(MASK_PRESETS):
        kb.button(text=domain, callback_data=f"adm:msel:{i}")
    kb.button(text="✏️ Свой домен", callback_data="adm:mcustom")
    kb.button(text="« Назад", callback_data="adm:mask")
    kb.adjust(2)
    await cq.message.edit_text("Выберите домен для маскировки или введите свой:", reply_markup=kb.as_markup())


async def propose_mask(target: Message, state: FSMContext, domain: str) -> None:
    wait = await target.answer(f"⏳ Проверяю {domain}…")
    ok, details = await check_mask_domain(domain)
    if not ok:
        await wait.edit_text(f"❌ {details}", reply_markup=kb_of(("« Выбрать другой", "adm:mask")))
        return
    await state.update_data(domain=domain.strip().lower())
    await wait.edit_text(
        f"✅ {domain} подходит ({details}).\n\n"
        "Применить? Xray перезапустится, <b>все текущие ключи этого подключения перестанут работать</b> "
        "до обновления у клиентов.",
        reply_markup=kb_of(("✅ Применить", "adm:mapply"), ("❌ Отмена", "adm:mask"), width=2),
    )


@router.callback_query(F.data.startswith("adm:msel:"))
async def cb_mask_preset(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await propose_mask(cq.message, state, MASK_PRESETS[int(cq.data.rsplit(":", 1)[1])])


@router.callback_query(F.data == "adm:mcustom")
async def cb_mask_custom(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await state.set_state(AdminSG.mask_custom)
    await cq.message.answer("Отправьте домен, например <code>www.microsoft.com</code> (/cancel — отмена).")


@router.message(AdminSG.mask_custom, ~F.text.startswith("/"))
async def st_mask_custom(msg: Message, state: FSMContext):
    await state.set_state(None)
    await propose_mask(msg, state, (msg.text or "").strip())


@router.callback_query(F.data == "adm:mapply")
async def cb_mask_apply(cq: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if "iid" not in data or "domain" not in data:
        await cq.answer("Начните заново", show_alert=True)
        return
    try:
        await set_mask(data["iid"], data["domain"])
    except Exception as e:
        log.exception("set mask failed")
        await cq.answer()
        await cq.message.edit_text(f"❌ Панель не приняла изменения: {html.escape(str(e))}", reply_markup=kb_of(BACK))
        return
    await cq.answer("Готово")
    await cq.message.edit_text(
        f"✅ Маскировка изменена на <b>{data['domain']}</b>.\n\n"
        "Разослать всем пользователям бота с активным доступом их новые ключи?\n"
        "Клиентов, которых вы добавляли в панели вручную, нужно обновить самостоятельно.",
        reply_markup=kb_of(("📨 Разослать новые ключи", "adm:sendkeys"), BACK),
    )


@router.callback_query(F.data == "adm:sendkeys")
async def cb_send_keys(cq: CallbackQuery):
    await cq.answer()
    await cq.message.edit_text("📨 Рассылаю новые ключи, пришлю отчёт по окончании.")
    try:
        by_email = {c["email"]: c for c in await xui.list_clients()}
    except Exception:
        log.exception("panel error")
        await cq.message.answer("Панель недоступна, попробуйте позже.")
        return
    user_ids = [u["tg_id"] for u in await db.users_with_clients() if is_active(by_email.get(u["email"]))]

    async def send_key(uid: int):
        key = await key_text(uid)
        if not key:
            return
        await cq.bot.send_message(
            uid, "🔄 Мы обновили настройки сервера. Удалите старый ключ в приложении и добавьте новый:\n\n" + key,
            disable_web_page_preview=True,
        )

    async def run():
        ok, fail = await broadcast(user_ids, send_key)
        await send_safe(cq.bot, cq.from_user.id, f"📨 Новые ключи разосланы: {ok}, ошибок {fail}.")

    asyncio.create_task(run())
