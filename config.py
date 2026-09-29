from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

PANEL_URL = os.environ["PANEL_URL"]
PANEL_TOKEN = os.environ["PANEL_TOKEN"]
PANEL_VERIFY_SSL = os.getenv("PANEL_VERIFY_SSL", "true").lower() == "true"
INBOUND_ID = int(os.environ["INBOUND_ID"])
SERVER_HOST = os.environ["SERVER_HOST"]
SUB_URL = os.getenv("SUB_URL", "").strip()

YOOKASSA_TOKEN = os.getenv("YOOKASSA_TOKEN", "").strip()
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "0"))
# Рефералка: пригласившему — за первую оплату друга, другу — бонус к первой покупке
REF_BONUS_DAYS = int(os.getenv("REF_BONUS_DAYS", "7"))
REF_FRIEND_DAYS = int(os.getenv("REF_FRIEND_DAYS", "3"))

DB_PATH = os.getenv("DB_PATH") or str(BASE_DIR / "bot.db")

DAY_MS = 86_400_000

# Тарифы по умолчанию. Цены меняются из админки и хранятся в базе
DEFAULT_PLANS = {
    "m1": {"title": "1 месяц", "days": 30, "stars": 150, "rub": 150},
    "m3": {"title": "3 месяца", "days": 90, "stars": 400, "rub": 400},
    "m12": {"title": "12 месяцев", "days": 365, "stars": 1400, "rub": 1400},
}

# Домены для маскировки Reality (нужны TLS 1.3 и HTTP/2)
MASK_PRESETS = [
    "www.samsung.com",
    "www.microsoft.com",
    "www.apple.com",
    "dl.google.com",
    "www.nvidia.com",
    "www.amd.com",
    "www.asus.com",
    "www.intel.com",
]

# Профиль бота: видно в профиле и в пустом чате до нажатия /start
BOT_SHORT_DESCRIPTION = "Быстрый и стабильный VPN. Ключ за минуту, оплата звёздами или картой."
BOT_DESCRIPTION = (
    "🛡 KalibVPN — быстрый и стабильный VPN на протоколе VLESS + Reality.\n\n"
    "• Работает там, где обычные VPN блокируют\n"
    "• YouTube, Instagram, ChatGPT и любые сайты без ограничений\n"
    "• iPhone, Android, Windows и macOS\n"
    f"• Бесплатный пробный период{f' {TRIAL_DAYS} дн.' if TRIAL_DAYS else ''}\n"
    "• Оплата Telegram Stars или картой\n\n"
    "Нажмите «Старт», чтобы получить ключ 👇"
)
