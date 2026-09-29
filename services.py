"""Логика, общая для клиентской части и админки."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import ssl
import time
from collections import defaultdict
from datetime import datetime
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

import db
from config import (
    ADMIN_IDS, DAY_MS, DEFAULT_PLANS, INBOUND_ID, PANEL_TOKEN, PANEL_URL, PANEL_VERIFY_SSL,
    REF_BONUS_DAYS, SERVER_HOST, SUB_URL,
)
from xui import XUI

log = logging.getLogger("vpn-bot")
xui = XUI(PANEL_URL, PANEL_TOKEN, verify=PANEL_VERIFY_SSL)
_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def now_ms() -> int:
    return int(time.time() * 1000)


def fmt_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y %H:%M")


def email_for(tg_id: int) -> str:
    return f"tg{tg_id}"


def is_active(record: dict | None) -> bool:
    if not record or not record.get("enable", True):
        return False
    exp = record.get("expiryTime") or 0
    return exp == 0 or exp > now_ms()


def status_line(record: dict | None) -> str:
    if not record:
        return "нет ключа"
    exp = record.get("expiryTime") or 0
    if not record.get("enable", True):
        return "⛔ Отключён"
    if exp == 0:
        return "✅ Бессрочно"
    if exp > now_ms():
        return f"✅ Активен до {fmt_date(exp)}"
    return f"❌ Истёк {fmt_date(exp)}"


# ---------- доступ ----------

async def extend_access(tg_id: int, days: int) -> dict:
    """Создаёт клиента в 3x-ui или продлевает существующего на days дней."""
    async with _locks[tg_id]:
        email = email_for(tg_id)
        record = await xui.get_client(email)
        if record:
            expiry = max(now_ms(), record.get("expiryTime") or 0) + days * DAY_MS
            await xui.update_client(record, expiryTime=expiry, enable=True)
            record.update(expiryTime=expiry, enable=True)
        else:
            record = await xui.add_client(INBOUND_ID, email, tg_id, now_ms() + days * DAY_MS)
        await db.set_client(tg_id, record["uuid"], email, record.get("subId", ""))
        return record


async def set_enabled(tg_id: int, enable: bool) -> dict | None:
    async with _locks[tg_id]:
        record = await xui.get_client(email_for(tg_id))
        if record:
            await xui.update_client(record, enable=enable)
            record["enable"] = enable
        return record


async def key_text(tg_id: int) -> str | None:
    user = await db.get_user(tg_id)
    if not user or not user["email"]:
        return None
    record = await xui.get_client(user["email"])
    if not record:
        return None

    parts = [f"<b>Ваш доступ</b>\n{status_line(record)}\n"]
    if SUB_URL and record.get("subId"):
        parts.append(f"🔄 Ссылка-подписка (рекомендуется):\n<code>{SUB_URL.rstrip('/')}/{record['subId']}</code>\n")
    for link in await xui.links(user["email"], SERVER_HOST):
        parts.append(f"🔑 Ключ:\n<code>{html.escape(link)}</code>\n")
    parts.append("Нажмите на ключ, чтобы скопировать, и импортируйте его в приложение.")
    return "\n".join(parts)


# ---------- тарифы ----------

async def get_plans() -> dict:
    return await db.get_setting("plans") or DEFAULT_PLANS


async def save_plans(plans: dict) -> None:
    await db.set_setting("plans", plans)


# ---------- уведомления и рассылки ----------

async def notify_admins(bot: Bot, text: str) -> None:
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, text)
        except TelegramAPIError:
            pass


async def send_safe(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except TelegramAPIError:
        return False


async def broadcast(user_ids: list[int], send: Callable[[int], Awaitable[object]]) -> tuple[int, int]:
    """Отправляет по одному сообщению каждому, соблюдая лимиты Telegram. Возвращает (успешно, ошибок)."""
    ok = fail = 0
    for uid in user_ids:
        for _ in range(2):
            try:
                await send(uid)
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramAPIError:
                fail += 1
                break
        else:
            fail += 1
        await asyncio.sleep(0.05)
    return ok, fail


# ---------- рефералка ----------

async def reward_referrer(bot: Bot, tg_id: int) -> None:
    """Начисляет бонус пригласившему за первую оплату друга."""
    user = await db.get_user(tg_id)
    referrer_id = user["referrer_id"] if user else None
    if not referrer_id or REF_BONUS_DAYS <= 0:
        return
    try:
        record = await extend_access(referrer_id, REF_BONUS_DAYS)
    except Exception:
        log.exception("referral bonus failed")
        await notify_admins(bot, f"⚠️ Не удалось начислить реферальный бонус {referrer_id}. Выдайте вручную через /admin")
        return
    await db.add_ref_bonus(referrer_id, REF_BONUS_DAYS)
    await send_safe(
        bot, referrer_id,
        f"🎉 Ваш друг оплатил подписку! Вам начислено +{REF_BONUS_DAYS} дн.\n"
        f"Доступ активен до {fmt_date(record['expiryTime'])}.",
    )


# ---------- маскировка Reality ----------

DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def current_mask(inbound: dict) -> str:
    rs = inbound["streamSettings"].get("realitySettings", {})
    target = rs.get("target") or rs.get("dest") or ""
    return target.rsplit(":", 1)[0] if target else ((rs.get("serverNames") or ["—"])[0])


async def check_mask_domain(domain: str) -> tuple[bool, str]:
    """Проверяет, что домен подходит для Reality: отвечает по TLS 1.3 и поддерживает HTTP/2."""
    domain = domain.strip().lower()
    if not DOMAIN_RE.match(domain):
        return False, "Некорректный домен. Пример: www.microsoft.com"
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    started = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(domain, 443, ssl=ctx, server_hostname=domain), 8)
    except Exception as e:
        return False, f"Не удалось подключиться к {domain}:443 по TLS ({type(e).__name__})"
    ping = int((time.monotonic() - started) * 1000)
    ssl_obj = writer.get_extra_info("ssl_object")
    version, alpn = ssl_obj.version(), ssl_obj.selected_alpn_protocol()
    writer.close()

    problems, notes = [], [f"пинг {ping} мс"]
    if alpn != "h2":
        problems.append("нет HTTP/2")
    if version == "TLSv1.3":
        notes.append("TLS 1.3 ✅")
    elif ssl.HAS_TLSv1_3:
        problems.append(f"нет TLS 1.3 (сайт отдал {version})")
    else:
        notes.append("TLS 1.3 не проверен: у машины, где запущен бот, устаревшая SSL-библиотека")
    if problems:
        return False, "Домен не подходит: " + ", ".join(problems)
    return True, ", ".join(notes)


async def set_mask(inbound_id: int, domain: str) -> None:
    inbound = await xui.get_inbound(inbound_id)
    rs = inbound["streamSettings"]["realitySettings"]
    rs["target"] = f"{domain}:443"
    if "dest" in rs:
        rs["dest"] = rs["target"]
    rs["serverNames"] = [domain]
    await xui.update_inbound(inbound)
