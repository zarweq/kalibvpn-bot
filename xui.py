"""Минимальный клиент API панели 3x-ui v3 (MHSanaei/3x-ui)."""
from __future__ import annotations

import json
import secrets
import uuid
from urllib.parse import quote, urlsplit

import httpx

# Поля клиента, которые переносим из записи панели при обновлении
# (update заменяет запись целиком, поэтому передаём всё, что нужно сохранить)
_KEEP_FIELDS = (
    "email", "flow", "security", "limitIp", "limitHwid", "totalGB", "expiryTime", "enable",
    "tgId", "subId", "group", "comment", "reset", "resetDay", "resetWeekday", "resetMax",
    "trafficReset", "trafficResetDay",
)


class XUIError(Exception):
    pass


class XUI:
    def __init__(self, base_url: str, token: str, verify: bool = True):
        # base_url вместе с webBasePath, например https://1.2.3.4:2053/secretpath
        self.base = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            verify=verify,
            timeout=15,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        r = await self.client.request(method, f"{self.base}{path}", **kwargs)
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code != 200 or not isinstance(data, dict):
            raise XUIError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return data

    async def _call(self, method: str, path: str, **kwargs):
        data = await self._request(method, path, **kwargs)
        if not data.get("success"):
            raise XUIError(data.get("msg") or f"{method} {path} failed")
        return data.get("obj")

    async def get_client(self, email: str) -> dict | None:
        """Запись клиента из панели или None, если такого клиента нет."""
        data = await self._request("GET", f"/panel/api/clients/get/{quote(email)}")
        if not data.get("success"):
            return None
        return data["obj"]["client"]

    async def list_clients(self) -> list[dict]:
        return await self._call("GET", "/panel/api/clients/list") or []

    async def flow_for(self, inbound_id: int) -> str:
        for ib in await self._call("GET", "/panel/api/inbounds/options") or []:
            if ib.get("id") == inbound_id:
                return "xtls-rprx-vision" if ib.get("tlsFlowCapable") else ""
        raise XUIError(f"Инбаунд {inbound_id} не найден")

    async def add_client(self, inbound_id: int, email: str, tg_id: int, expiry_ms: int) -> dict:
        client = {
            "id": str(uuid.uuid4()),
            "flow": await self.flow_for(inbound_id),
            "email": email,
            "limitIp": 0,
            "totalGB": 0,
            "expiryTime": expiry_ms,
            "enable": True,
            "tgId": tg_id,
            "subId": secrets.token_hex(8),
            "comment": "telegram bot",
        }
        await self._call("POST", "/panel/api/clients/add", json={"client": client, "inboundIds": [inbound_id]})
        return await self.get_client(email)

    async def update_client(self, record: dict, **changes) -> None:
        """Меняет поля клиента (expiryTime, enable, ...), сохраняя остальные."""
        client = {k: record[k] for k in _KEEP_FIELDS if k in record}
        client["id"] = record["uuid"]
        client.update(changes)
        await self._call("POST", f"/panel/api/clients/update/{quote(record['email'])}", json=client)

    async def get_inbound(self, inbound_id: int) -> dict:
        inbound = await self._call("GET", f"/panel/api/inbounds/get/{inbound_id}")
        for key in ("settings", "streamSettings", "sniffing"):
            if isinstance(inbound.get(key), str):  # старые версии отдают JSON-строкой
                inbound[key] = json.loads(inbound[key] or "{}")
        return inbound

    async def update_inbound(self, inbound: dict) -> None:
        # Список клиентов сервер при update игнорирует, клиенты не пострадают
        body = {k: v for k, v in inbound.items() if k != "clientStats"}
        await self._call("POST", f"/panel/api/inbounds/update/{inbound['id']}", json=body)

    async def reality_inbounds(self) -> list[dict]:
        result = []
        for ib in await self._call("GET", "/panel/api/inbounds/options") or []:
            full = await self.get_inbound(ib["id"])
            if full["streamSettings"].get("security") == "reality":
                result.append(full)
        return result

    async def links(self, email: str, public_host: str) -> list[str]:
        links = await self._call("GET", f"/panel/api/clients/links/{quote(email)}") or []
        # Если бот ходит в панель по 127.0.0.1, панель может подставить его в ссылку — меняем на публичный адрес
        out = []
        for link in links:
            host = urlsplit(link).hostname
            if host in ("127.0.0.1", "localhost", "::1"):
                link = link.replace(f"@{host}:", f"@{public_host}:", 1)
            out.append(link)
        return out
