#!/usr/bin/env python3
import os
import re
import time
import json
import html
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, request, jsonify

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram-bot")

TELEGRAM_API = "https://api.telegram.org"
REQUIRED_CHANNEL_LINK = "https://t.me/+KsLnyjMV579jM2Ix"
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "5127721601"))
ADMIN_IDS = {5075629326, 5127721601}

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
WEBHOOK_ONLY = os.getenv("WEBHOOK_ONLY", "true").lower() == "true"
PORT = int(os.getenv("PORT", "8000"))

CANCELLABLE_STEPS = {
    "tienda_cup", "tienda_confirm_number", "tienda_transfer",
    "compra_amount", "compra_waiting_screenshot",
    "venta_amount", "venta_payment_method", "venta_waiting_screenshot",
    "sm_waiting_screenshot", "svc_waiting_screenshot",
    "compra_sm_amount", "compra_sm_waiting_screenshot",
}
WAITING_SCREENSHOT_STEPS = {
    "sm_waiting_screenshot", "compra_waiting_screenshot", "venta_waiting_screenshot",
    "svc_waiting_screenshot", "compra_sm_waiting_screenshot",
}

app = Flask(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def js_parse_int(value: Any, default: int = 0) -> int:
    try:
        s = str(value).strip()
        m = re.match(r"^[+-]?\d+", s)
        return int(m.group(0)) if m else default
    except Exception:
        return default


def js_parse_float(value: Any, default: float = 0.0) -> float:
    try:
        s = str(value).strip().replace(",", ".")
        m = re.match(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)", s)
        return float(m.group(0)) if m else default
    except Exception:
        return default


def clean_service_id(text: str) -> str:
    return re.sub(r"\s+", "_", text.strip().lower())


def e(value: Any) -> str:
    return html.escape(str(value), quote=False)


class Database:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.key = key
        self.base = f"{self.url}/rest/v1"
        self.session = requests.Session()
        self.session.headers.update({
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _request(self, method: str, table: str, params: Optional[Dict[str, Any]] = None,
                 json_body: Any = None, extra_headers: Optional[Dict[str, str]] = None,
                 timeout: int = 20) -> Any:
        headers = dict(extra_headers or {})
        res = self.session.request(method, f"{self.base}/{table}", params=params,
                                   json=json_body, headers=headers, timeout=timeout)
        if res.status_code >= 400:
            raise RuntimeError(f"Database error {res.status_code}: {res.text[:500]}")
        if not res.text:
            return None
        try:
            return res.json()
        except Exception:
            return res.text

    def select(self, table: str, select: str = "*", filters: Optional[Dict[str, str]] = None,
               order: Optional[str] = None, single: bool = False, head: bool = False,
               count: bool = False) -> Any:
        params: Dict[str, Any] = {"select": select}
        if filters:
            params.update(filters)
        if order:
            params["order"] = order
        headers: Dict[str, str] = {}
        if single:
            headers["Accept"] = "application/vnd.pgrst.object+json"
        if count:
            headers["Prefer"] = "count=exact"
        method = "HEAD" if head else "GET"
        res = self.session.request(method, f"{self.base}/{table}", params=params, headers=headers, timeout=20)
        if res.status_code == 406 and single:
            return None
        if res.status_code >= 400:
            raise RuntimeError(f"Database error {res.status_code}: {res.text[:500]}")
        if head:
            content_range = res.headers.get("Content-Range", "")
            total = 0
            if "/" in content_range:
                try:
                    total = int(content_range.rsplit("/", 1)[1])
                except Exception:
                    total = 0
            return total
        return res.json() if res.text else None

    def upsert(self, table: str, rows: Any, on_conflict: str) -> Any:
        headers = {"Prefer": "resolution=merge-duplicates,return=minimal"}
        params = {"on_conflict": on_conflict}
        return self._request("POST", table, params=params, json_body=rows, extra_headers=headers)

    def insert(self, table: str, row: Dict[str, Any]) -> Any:
        return self._request("POST", table, json_body=row, extra_headers={"Prefer": "return=minimal"})

    def update(self, table: str, values: Dict[str, Any], filters: Dict[str, str]) -> Any:
        return self._request("PATCH", table, params=filters, json_body=values, extra_headers={"Prefer": "return=minimal"})

    def delete(self, table: str, filters: Dict[str, str]) -> Any:
        return self._request("DELETE", table, params=filters, extra_headers={"Prefer": "return=minimal"})


DB: Optional[Database] = None


def get_db() -> Database:
    global DB
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not configured")
    if DB is None:
        DB = Database(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return DB


class Telegram:
    def __init__(self, token: str):
        self.token = token
        self.base = f"{TELEGRAM_API}/bot{token}"
        self.session = requests.Session()

    def call(self, method: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 25) -> Dict[str, Any]:
        res = self.session.post(f"{self.base}/{method}", json=payload or {}, timeout=timeout)
        try:
            data = res.json()
        except Exception:
            data = {"ok": False, "description": res.text}
        if not res.ok or not data.get("ok", False):
            log.warning("Telegram %s failed: %s", method, data)
        return data

    def send_message(self, chat_id: int, text: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if extra:
            payload.update(extra)
        return self.call("sendMessage", payload)

    def forward_message(self, chat_id: int, from_chat_id: int, message_id: int) -> Dict[str, Any]:
        return self.call("forwardMessage", {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id})

    def answer_callback(self, callback_query_id: str, text: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self.call("answerCallbackQuery", payload)

    def get_me(self) -> Dict[str, Any]:
        return self.call("getMe")

    def get_updates(self, offset: int, timeout: int) -> Dict[str, Any]:
        return self.call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}, timeout=timeout + 10)

    def set_webhook(self, url: str) -> Dict[str, Any]:
        return self.call("setWebhook", {"url": url, "allowed_updates": ["message", "callback_query"]})


TG: Optional[Telegram] = Telegram(BOT_TOKEN) if BOT_TOKEN else None


def get_tg() -> Telegram:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")
    global TG
    if TG is None:
        TG = Telegram(BOT_TOKEN)
    return TG


def eq(value: Any) -> str:
    return f"eq.{value}"


def load_config(db: Database) -> Dict[str, Any]:
    rows = db.select("bot_config", "*") or []
    bot_config = {r.get("key"): r.get("value") for r in rows}
    svc_rows = db.select("bot_services", "*", filters={"active": "eq.true"}, order="sort_order.asc") or []
    return {
        "ADMIN_CUP_CARD": bot_config.get("admin_cup_card") or "9204-0699-9692-9675",
        "ADMIN_CONFIRM_NUMBER": bot_config.get("admin_confirm_number") or "58613666",
        "ADMIN_MI_TRANSFER": bot_config.get("admin_mi_transfer") or "58613666",
        "ADMIN_USDT_WALLET": bot_config.get("admin_usdt_wallet") or "0xD64Ea37111d1926C1015091a6D241996946A29B0",
        "BUY_RATE": bot_config.get("buy_rate") or 600,
        "SELL_RATE": bot_config.get("sell_rate") or 640,
        "SM_PACKAGES": bot_config.get("sm_packages") or [
            {"sm": 120, "cup": 400}, {"sm": 240, "cup": 1000}, {"sm": 370, "cup": 1300}
        ],
        "SERVICES": [s for s in svc_rows if s.get("category") == "service"],
        "TELEGRAM_PREMIUM": [s for s in svc_rows if s.get("category") == "telegram_premium"],
        "SM_BUY_RATE": bot_config.get("sm_buy_rate") or 2.5,
    }


def upsert_user_state(db: Database, chat_id: int, username: Optional[str], first_name: Optional[str], step: str) -> None:
    db.upsert("telegram_user_state", {
        "chat_id": chat_id,
        "username": username,
        "first_name": first_name,
        "step": step,
        "updated_at": now_iso(),
    }, "chat_id")


def send_main_menu(tg: Telegram, chat_id: int, text: str) -> None:
    tg.send_message(chat_id, text, {"reply_markup": {
        "keyboard": [[{"text": "🛍️ Tienda"}, {"text": "👤 Cuenta"}], [{"text": "🎧 Soporte"}]],
        "resize_keyboard": True,
        "is_persistent": True,
    }})


def send_tienda_menu(tg: Telegram, chat_id: int, text: str) -> None:
    tg.send_message(chat_id, text, {"reply_markup": {
        "keyboard": [
            [{"text": "📦 Servicios"}, {"text": "💵 Venta de SM"}],
            [{"text": "💰 Venta de moneda"}, {"text": "🪙 Compra de moneda"}],
            [{"text": "📲 Compra de SM"}],
            [{"text": "🔙 Volver"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }})


def inline_cancel(target: str = "cancel_to_tienda", text: str = "❌ Cancelar") -> Dict[str, Any]:
    return {"reply_markup": {"inline_keyboard": [[{"text": text, "callback_data": target}]]}}


def store_purchase(db: Database, chat_id: int, description: str, contact_message: str) -> None:
    db.upsert("bot_config", {
        "key": f"purchase_{chat_id}",
        "value": {"description": description, "contactMessage": contact_message},
        "updated_at": now_iso(),
    }, "key")


def forward_photo_to_admin(tg: Telegram, from_chat_id: int, message_id: int, caption: str) -> None:
    tg.send_message(ADMIN_CHAT_ID, caption)
    tg.forward_message(ADMIN_CHAT_ID, from_chat_id, message_id)
    tg.send_message(ADMIN_CHAT_ID, f"⚖️ <b>Marca el resultado del negocio con el usuario {from_chat_id}:</b>", {
        "reply_markup": {"inline_keyboard": [
            [{"text": "✅ Negocio exitoso", "callback_data": f"deal_ok_{from_chat_id}"}],
            [{"text": "❌ Negocio fallido", "callback_data": f"deal_fail_{from_chat_id}"}],
        ]}
    })


def user_label(message: Dict[str, Any], chat_id: int) -> str:
    frm = message.get("from") or {}
    username = frm.get("username")
    first_name = frm.get("first_name")
    return f"@{username}" if username else (first_name or f"Chat {chat_id}")


def handle_message(tg: Telegram, db: Database, message: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    chat_id = int(message.get("chat", {}).get("id"))
    text = message.get("text") or ""
    frm = message.get("from") or {}
    username = frm.get("username")
    first_name = frm.get("first_name")

    admin_cup_card = cfg["ADMIN_CUP_CARD"]
    admin_confirm_number = cfg["ADMIN_CONFIRM_NUMBER"]
    admin_mi_transfer = cfg["ADMIN_MI_TRANSFER"]
    admin_usdt_wallet = cfg["ADMIN_USDT_WALLET"]
    buy_rate = float(cfg["BUY_RATE"])
    sell_rate = float(cfg["SELL_RATE"])
    sm_packages = cfg["SM_PACKAGES"]
    services = cfg["SERVICES"]
    telegram_premium = cfg["TELEGRAM_PREMIUM"]
    sm_buy_rate = float(cfg["SM_BUY_RATE"])

    if text.startswith("/start"):
        upsert_user_state(db, chat_id, username, first_name, "awaiting_join")
        welcome = (
            f"👋 <b>¡Bienvenido{', ' + e(first_name) if first_name else ''}!</b>\n\n"
            f"Para continuar, únete a nuestro grupo:\n👉 {REQUIRED_CHANNEL_LINK}\n\n"
            'Después de unirte, presiona el botón <b>"✅ Verificar"</b> para confirmar.'
        )
        tg.send_message(chat_id, welcome, {"reply_markup": {"inline_keyboard": [
            [{"text": "📢 Unirse al Grupo", "url": REQUIRED_CHANNEL_LINK}],
            [{"text": "✅ Verificar", "callback_data": "verify_channel"}],
        ]}})
        return

    state = db.select("telegram_user_state", "step", filters={"chat_id": eq(chat_id)}, single=True) or {}
    step = state.get("step")

    if text == "❌ Cancelar" and step in CANCELLABLE_STEPS:
        upsert_user_state(db, chat_id, username, first_name, "tienda_menu")
        send_tienda_menu(tg, chat_id, "🚫 <b>Solicitud cancelada.</b>\n\nSelecciona una opción:")
        return

    if step and (step.startswith("admin_edit_") or step.startswith("admin_add_svc_") or step == "admin_broadcast_msg"):
        if chat_id not in ADMIN_IDS:
            upsert_user_state(db, chat_id, username, first_name, "menu")
            return
        handle_admin_text_input(tg, db, chat_id, username, first_name, step, text)
        return

    if message.get("photo") and step in WAITING_SCREENSHOT_STEPS:
        purchase_row = db.select("bot_config", "value", filters={"key": eq(f"purchase_{chat_id}")}, single=True) or {}
        purchase_info = purchase_row.get("value") or {}
        purchase_desc = purchase_info.get("description") or step
        forward_photo_to_admin(
            tg, chat_id, int(message.get("message_id")),
            f"📸 <b>Nueva captura de pago</b>\n\nDe: {e(user_label(message, chat_id))}\n📦 <b>Operación:</b> {e(purchase_desc)}\nChat ID: {chat_id}"
        )
        contact_msg = purchase_info.get("contactMessage") or "Buenas, he realizado una compra"
        encoded = requests.utils.quote(contact_msg)
        upsert_user_state(db, chat_id, username, first_name, "tienda_menu")
        tg.send_message(chat_id, "✅ <b>¡Captura recibida!</b>\n\nSi ha realizado el pago, por favor contacta con el administrador:", {
            "reply_markup": {"inline_keyboard": [
                [{"text": "📩 Contactar por Telegram", "url": f"https://t.me/Vbussines26?text={encoded}"}],
                [{"text": "📱 Contactar por WhatsApp", "url": f"https://wa.me/5358613666?text={encoded}"}],
                [{"text": "🏠 Volver al menú", "callback_data": "back_to_tienda"}],
            ]}
        })
        db.delete("bot_config", {"key": eq(f"purchase_{chat_id}")})
        return

    if step == "tienda_cup":
        db.upsert("telegram_user_config", {"chat_id": chat_id, "cup_card": text.strip(), "updated_at": now_iso()}, "chat_id")
        upsert_user_state(db, chat_id, username, first_name, "tienda_confirm_number")
        tg.send_message(chat_id, "✅ Tarjeta CUP guardada.\n\n📱 Ahora envía el <b>número a confirmar</b>:", inline_cancel())
        return

    if step == "tienda_confirm_number":
        db.upsert("telegram_user_config", {"chat_id": chat_id, "confirm_number": text.strip(), "updated_at": now_iso()}, "chat_id")
        upsert_user_state(db, chat_id, username, first_name, "tienda_transfer")
        tg.send_message(chat_id, "✅ Número a confirmar guardado.\n\n💳 Ahora envía tu <b>monedero Mi Transfer</b>:", inline_cancel())
        return

    if step == "tienda_transfer":
        db.upsert("telegram_user_config", {"chat_id": chat_id, "mi_transfer": text.strip(), "updated_at": now_iso()}, "chat_id")
        upsert_user_state(db, chat_id, username, first_name, "tienda_menu")
        send_tienda_menu(tg, chat_id, "✅ Monedero Mi Transfer guardado.\n\n🎉 <b>¡Configuración completada!</b>\n\nUsa los botones del menú para navegar por la tienda. 👇")
        return

    if step == "compra_amount":
        amount = text.strip()
        cup_total = js_parse_float(amount) * buy_rate
        store_purchase(db, chat_id, f"Compra de {amount} USDT ({cup_total:g} CUP)", f"Buenas, he vendido {amount} USDT")
        upsert_user_state(db, chat_id, username, first_name, "compra_waiting_screenshot")
        tg.send_message(chat_id,
            f"🪙 <b>Compra de Moneda</b>\n\nMonto a comprar: <b>{e(amount)} USDT</b>\nDebes pagar: <b>{cup_total:g} CUP</b>\n\n"
            f"📤 Envía los USDT a la siguiente wallet:\n<code>{e(admin_usdt_wallet)}</code>\n\n📸 Después de enviar, manda una <b>captura de pantalla</b> de la transferencia.", inline_cancel())
        return

    if step == "venta_amount":
        usdt_amount = js_parse_float(text.strip())
        cup_amount = usdt_amount * sell_rate
        store_purchase(db, chat_id, f"Venta de {usdt_amount:g} USDT ({cup_amount:g} CUP)", f"Buenas, he comprado {usdt_amount:g} USDT")
        upsert_user_state(db, chat_id, username, first_name, "venta_payment_method")
        tg.send_message(chat_id,
            f"💰 <b>Venta de Moneda</b>\n\nMonto: <b>{usdt_amount:g} USDT</b> = <b>{cup_amount:g} CUP</b>\n"
            f"(Tasa: 1 USDT = {sell_rate:g} CUP)\n\nDebes pagar <b>{cup_amount:g} CUP</b> al método que elijas.\n\n¿Cómo deseas pagar?",
            {"reply_markup": {"inline_keyboard": [
                [{"text": "💳 Tarjeta CUP", "callback_data": "venta_pay_card"}],
                [{"text": "📲 Bolsa Mi Transfer", "callback_data": "venta_pay_transfer"}],
                [{"text": "❌ Cancelar", "callback_data": "cancel_to_tienda"}],
            ]}})
        return

    if step == "compra_sm_amount":
        amount = js_parse_int(text.strip())
        if amount <= 0:
            tg.send_message(chat_id, "❌ Envía una cantidad válida de saldo móvil.", inline_cancel())
            return
        cup_amount = round(amount * sm_buy_rate)
        store_purchase(db, chat_id, f"Compra de SM: {amount} SM", f"Buenas tardes, he transferido {amount} de saldo móvil")
        upsert_user_state(db, chat_id, username, first_name, "compra_sm_waiting_screenshot")
        tg.send_message(chat_id,
            f"📲 <b>Compra de Saldo Móvil</b>\n\nCantidad: <b>{amount} SM</b>\nRecibirás: <b>{cup_amount} CUP</b>\n"
            f"(Tasa: 1 SM = {sm_buy_rate:g} CUP)\n\n📱 Transfiere el saldo al número:\n<code>58613666</code>\n\n"
            f"📸 Después de transferir, envía una <b>captura de pantalla</b> de la transferencia.", inline_cancel())
        return

    if step in WAITING_SCREENSHOT_STEPS and not message.get("photo"):
        tg.send_message(chat_id, "📸 Por favor envía una <b>captura de pantalla</b> de la transferencia.", inline_cancel())
        return

    if step == "menu":
        if text == "🛍️ Tienda":
            config = db.select("telegram_user_config", "cup_card,confirm_number,mi_transfer", filters={"chat_id": eq(chat_id)}, single=True) or {}
            if config.get("cup_card") and config.get("confirm_number") and config.get("mi_transfer"):
                upsert_user_state(db, chat_id, username, first_name, "tienda_menu")
                send_tienda_menu(tg, chat_id, "🛍️ <b>Tienda</b>\n\nSelecciona una opción:")
            else:
                upsert_user_state(db, chat_id, username, first_name, "tienda_cup")
                tg.send_message(chat_id, "🛍️ <b>Configuración de Tienda</b>\n\nAntes de empezar necesitas configurar tus datos de pago.\n\n💳 Envía tu <b>número de tarjeta CUP</b>:", inline_cancel())
            return

        if text == "👤 Cuenta":
            config = db.select("telegram_user_config", "cup_card,confirm_number,mi_transfer,successful_deals", filters={"chat_id": eq(chat_id)}, single=True) or {}
            cup = config.get("cup_card") or "❌ No configurada"
            confirm = config.get("confirm_number") or "❌ No configurado"
            transfer = config.get("mi_transfer") or "❌ No configurado"
            deals = config.get("successful_deals") or 0
            me = tg.get_me()
            bot_username = (((me.get("result") or {}).get("username")) or "bot")
            invite_link = f"https://t.me/{bot_username}?start=ref_{chat_id}"
            keyboard = [[{"text": "🔗 Mi enlace de invitación", "url": invite_link}]]
            if chat_id in ADMIN_IDS:
                keyboard.append([{"text": "⚙️ Panel de Administrador", "callback_data": "admin_panel"}])
            tg.send_message(chat_id,
                f"👤 <b>Mi Cuenta</b>\n\n💳 <b>Tarjeta CUP:</b> {e(cup)}\n📱 <b>Número a confirmar:</b> {e(confirm)}\n"
                f"🪙 <b>Monedero Mi Transfer:</b> {e(transfer)}\n\n✅ <b>Negocios exitosos:</b> {deals}\n\n"
                f"🔗 <b>Tu enlace de invitación:</b>\n<code>{invite_link}</code>",
                {"reply_markup": {"inline_keyboard": keyboard}})
            return

        if text == "🎧 Soporte":
            tg.send_message(chat_id, "🎧 <b>Soporte Técnico</b>\n\nContacta con nuestro equipo de soporte:", {
                "reply_markup": {"inline_keyboard": [
                    [{"text": "📩 Contactar por Telegram", "url": "https://t.me/Vbussines26"}],
                    [{"text": "📱 Contactar por WhatsApp", "url": "https://wa.me/5358613666"}],
                ]}
            })
            return

        tg.send_message(chat_id, "Usa los botones del menú para navegar. 👇")
        return

    if step == "tienda_menu":
        if text == "📦 Servicios":
            svc_list = "\n".join([f"{e(s.get('emoji','📦'))} {e(s.get('name'))}: <b>{s.get('cup')} CUP</b>" for s in services])
            tgp_list = "\n".join([f"   • {e(t.get('name'))}: <b>{t.get('cup')} CUP</b>" for t in telegram_premium])
            buttons = [[{"text": f"{s.get('emoji','📦')} {s.get('name')} - {s.get('cup')} CUP", "callback_data": f"svc_{s.get('id')}"}] for s in services]
            buttons.append([{"text": "✨ Telegram Premium", "callback_data": "svc_tgp_menu"}])
            buttons.append([{"text": "❌ Cancelar", "callback_data": "cancel_to_tienda"}])
            tg.send_message(chat_id, f"⚠️ <b>Servicios Tecnológicos</b>\n\n{svc_list}\n\n✨ <b>Telegram Premium:</b>\n{tgp_list}\n\nElige un servicio:", {"reply_markup": {"inline_keyboard": buttons}})
            return

        if text == "💵 Venta de SM":
            packages_text = "\n".join([f"{i + 1}. <b>{p.get('sm')} SM</b> x <b>{p.get('cup')} CUP</b>" for i, p in enumerate(sm_packages)])
            buttons = [[{"text": f"📱 {p.get('sm')} SM - {p.get('cup')} CUP", "callback_data": f"sm_pkg_{p.get('sm')}"}] for p in sm_packages]
            buttons.append([{"text": "❌ Cancelar", "callback_data": "cancel_to_tienda"}])
            tg.send_message(chat_id, f"💵 <b>Venta de Saldo Móvil</b>\n\nElige un paquete:\n\n{packages_text}", {"reply_markup": {"inline_keyboard": buttons}})
            return

        if text == "💰 Venta de moneda":
            tg.send_message(chat_id, f"💰 <b>Venta de Moneda</b>\n\nEl administrador vende:\n<b>1 USDT = {sell_rate:g} CUP</b>\n\n📝 Envía la cantidad de <b>USDT</b> que deseas comprar:", inline_cancel())
            upsert_user_state(db, chat_id, username, first_name, "venta_amount")
            return

        if text == "🪙 Compra de moneda":
            tg.send_message(chat_id, f"🪙 <b>Compra de Moneda</b>\n\nCompramos:\n<b>1 USDT = {buy_rate:g} CUP</b>\n\n📝 Envía la cantidad de <b>USDT</b> que deseas vender:", inline_cancel())
            upsert_user_state(db, chat_id, username, first_name, "compra_amount")
            return

        if text == "📲 Compra de SM":
            tg.send_message(chat_id, f"📲 <b>Compra de Saldo Móvil</b>\n\nEl administrador compra saldo móvil a <b>{sm_buy_rate:g}</b> (1 SM = {sm_buy_rate:g} CUP)\n\n📝 Envía la <b>cantidad de saldo</b> que vas a vender:", inline_cancel())
            upsert_user_state(db, chat_id, username, first_name, "compra_sm_amount")
            return

        if text == "🔙 Volver":
            upsert_user_state(db, chat_id, username, first_name, "menu")
            send_main_menu(tg, chat_id, "🏠 <b>Menú Principal</b>\n\nSelecciona una opción:")
            return

        tg.send_message(chat_id, "Usa los botones del menú para navegar. 👇")
        return

    tg.send_message(chat_id, "Escribe /start para comenzar.")


def handle_callback_query(tg: Telegram, db: Database, callback_query: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    message = callback_query.get("message") or {}
    chat_id = int((message.get("chat") or {}).get("id"))
    data = callback_query.get("data") or ""
    frm = callback_query.get("from") or {}
    username = frm.get("username")
    first_name = frm.get("first_name")

    admin_cup_card = cfg["ADMIN_CUP_CARD"]
    admin_confirm_number = cfg["ADMIN_CONFIRM_NUMBER"]
    admin_mi_transfer = cfg["ADMIN_MI_TRANSFER"]
    sm_packages = cfg["SM_PACKAGES"]
    services = cfg["SERVICES"]
    telegram_premium = cfg["TELEGRAM_PREMIUM"]

    if data == "cancel_to_tienda":
        tg.answer_callback(callback_query["id"], "🚫 Cancelado")
        upsert_user_state(db, chat_id, username, first_name, "tienda_menu")
        send_tienda_menu(tg, chat_id, "🚫 <b>Solicitud cancelada.</b>\n\nSelecciona una opción:")
        return

    if data == "back_to_tienda":
        tg.answer_callback(callback_query["id"])
        upsert_user_state(db, chat_id, username, first_name, "tienda_menu")
        send_tienda_menu(tg, chat_id, "🛍️ <b>Tienda</b>\n\nSelecciona una opción:")
        return

    if data.startswith("deal_ok_") or data.startswith("deal_fail_"):
        if chat_id not in ADMIN_IDS:
            tg.answer_callback(callback_query["id"], "❌ No autorizado")
            return
        is_ok = data.startswith("deal_ok_")
        user_chat_id = js_parse_int(data.replace("deal_ok_" if is_ok else "deal_fail_", ""))
        if is_ok:
            user_cfg = db.select("telegram_user_config", "successful_deals", filters={"chat_id": eq(user_chat_id)}, single=True) or {}
            db.upsert("telegram_user_config", {"chat_id": user_chat_id, "successful_deals": (user_cfg.get("successful_deals") or 0) + 1, "updated_at": now_iso()}, "chat_id")
            admin_cfg = db.select("telegram_user_config", "successful_deals", filters={"chat_id": eq(chat_id)}, single=True) or {}
            db.upsert("telegram_user_config", {"chat_id": chat_id, "successful_deals": (admin_cfg.get("successful_deals") or 0) + 1, "updated_at": now_iso()}, "chat_id")
            tg.answer_callback(callback_query["id"], "✅ Negocio marcado como exitoso")
            tg.send_message(chat_id, f"✅ <b>Negocio exitoso registrado</b> para el usuario <code>{user_chat_id}</code>.")
            tg.send_message(user_chat_id, "🎉 <b>El negocio ha sido exitoso</b>\n\nGracias por confiar en nuestros servicios.")
        else:
            tg.answer_callback(callback_query["id"], "❌ Negocio marcado como fallido")
            tg.send_message(chat_id, f"❌ <b>Negocio fallido</b> registrado para el usuario <code>{user_chat_id}</code>.")
            tg.send_message(user_chat_id, "⚠️ <b>El negocio no pudo completarse.</b>\n\nPor favor contacta con el administrador para más información.")
        return

    if data == "verify_channel":
        tg.answer_callback(callback_query["id"], "✅ ¡Verificación exitosa!")
        upsert_user_state(db, chat_id, username, first_name, "menu")
        send_main_menu(tg, chat_id, "🎉 <b>¡Verificación exitosa!</b>\n\nBienvenido al menú principal. Usa los botones de abajo para navegar.")
        return

    if data == "admin_panel":
        if chat_id not in ADMIN_IDS:
            tg.answer_callback(callback_query["id"], "❌ No autorizado")
            return
        tg.answer_callback(callback_query["id"])
        send_admin_menu(tg, chat_id)
        return

    if data == "admin_stats":
        if chat_id not in ADMIN_IDS:
            return
        tg.answer_callback(callback_query["id"])
        user_count = db.select("telegram_user_state", "*", head=True, count=True)
        configs = db.select("telegram_user_config", "successful_deals") or []
        total_deals = sum([(c.get("successful_deals") or 0) for c in configs])
        tg.send_message(chat_id, f"📊 <b>Estadísticas</b>\n\n👥 Usuarios: <b>{user_count or 0}</b>\n✅ Negocios exitosos: <b>{total_deals}</b>", inline_cancel("admin_panel", "🔙 Volver"))
        return

    if data == "admin_rates":
        if chat_id not in ADMIN_IDS:
            return
        tg.answer_callback(callback_query["id"])
        rows = db.select("bot_config", "*") or []
        cfg2 = {r.get("key"): r.get("value") for r in rows}
        sm_rate = cfg2.get("sm_buy_rate") or 2.5
        tg.send_message(chat_id,
            f"💰 <b>Tasas de Cambio</b>\n\n🪙 Compra USDT: <b>{cfg2.get('buy_rate') or 'N/A'} CUP</b>\n"
            f"💵 Venta USDT: <b>{cfg2.get('sell_rate') or 'N/A'} CUP</b>\n📲 Compra SM: <b>{sm_rate}</b> (1 SM = {sm_rate} CUP)",
            {"reply_markup": {"inline_keyboard": [
                [{"text": "✏️ Editar Compra USDT", "callback_data": "admin_set_buy"}, {"text": "✏️ Editar Venta USDT", "callback_data": "admin_set_sell"}],
                [{"text": "✏️ Editar Compra SM", "callback_data": "admin_set_sm_buy_rate"}],
                [{"text": "🔙 Volver", "callback_data": "admin_panel"}],
            ]}})
        return

    if data in {"admin_set_buy", "admin_set_sell", "admin_set_sm_buy_rate"}:
        if chat_id not in ADMIN_IDS:
            return
        tg.answer_callback(callback_query["id"])
        if data == "admin_set_buy":
            step, msg, back = "admin_edit_buy_rate", "✏️ Envía el nuevo precio de <b>compra USDT</b> (CUP por 1 USDT):", "admin_panel"
        elif data == "admin_set_sell":
            step, msg, back = "admin_edit_sell_rate", "✏️ Envía el nuevo precio de <b>venta USDT</b> (CUP por 1 USDT):", "admin_panel"
        else:
            step, msg, back = "admin_edit_sm_buy_rate", "✏️ Envía la nueva tasa de <b>compra SM</b> (CUP por 1 SM, ej: 2.5):", "admin_rates"
        upsert_user_state(db, chat_id, username, first_name, step)
        tg.send_message(chat_id, msg, inline_cancel(back))
        return

    if data == "admin_sm":
        if chat_id not in ADMIN_IDS:
            return
        tg.answer_callback(callback_query["id"])
        row = db.select("bot_config", "value", filters={"key": eq("sm_packages")}, single=True) or {}
        pkgs = row.get("value") or []
        lines = "\n".join([f"{i + 1}. <b>{p.get('sm')} SM</b> = <b>{p.get('cup')} CUP</b>" for i, p in enumerate(pkgs)])
        buttons = [[{"text": f"✏️ {p.get('sm')} SM ({p.get('cup')} CUP)", "callback_data": f"admin_sm_edit_{i}"}] for i, p in enumerate(pkgs)]
        buttons.append([{"text": "🔙 Volver", "callback_data": "admin_panel"}])
        tg.send_message(chat_id, f"📱 <b>Paquetes de Saldo Móvil</b>\n\n{lines or 'No hay paquetes'}", {"reply_markup": {"inline_keyboard": buttons}})
        return

    if data.startswith("admin_sm_edit_"):
        if chat_id not in ADMIN_IDS:
            return
        idx = data.replace("admin_sm_edit_", "")
        tg.answer_callback(callback_query["id"])
        upsert_user_state(db, chat_id, username, first_name, f"admin_edit_sm_cup:{idx}")
        tg.send_message(chat_id, "✏️ Envía el nuevo precio en <b>CUP</b> para este paquete SM:", inline_cancel("admin_sm"))
        return

    if data == "admin_services":
        if chat_id not in ADMIN_IDS:
            return
        tg.answer_callback(callback_query["id"])
        svcs = db.select("bot_services", "*", filters={"active": "eq.true"}, order="sort_order.asc") or []
        regular = [s for s in svcs if s.get("category") == "service"]
        tgp = [s for s in svcs if s.get("category") == "telegram_premium"]
        text2 = "📦 <b>Servicios</b>\n\n"
        for s in regular:
            text2 += f"{e(s.get('emoji','📦'))} {e(s.get('name'))}: <b>{s.get('cup')} CUP</b>\n"
        if tgp:
            text2 += "\n✨ <b>Telegram Premium:</b>\n"
            for s in tgp:
                text2 += f"   • {e(s.get('name'))}: <b>{s.get('cup')} CUP</b>\n"
        buttons = [[{"text": f"✏️ {s.get('emoji','📦')} {s.get('name')}", "callback_data": f"admin_svc_edit_{s.get('id')}"}, {"text": "🗑️", "callback_data": f"admin_svc_del_{s.get('id')}"}] for s in svcs]
        buttons.append([{"text": "➕ Agregar Servicio", "callback_data": "admin_svc_add"}])
        buttons.append([{"text": "🔙 Volver", "callback_data": "admin_panel"}])
        tg.send_message(chat_id, text2, {"reply_markup": {"inline_keyboard": buttons}})
        return

    if data.startswith("admin_svc_edit_"):
        if chat_id not in ADMIN_IDS:
            return
        svc_id = data.replace("admin_svc_edit_", "")
        tg.answer_callback(callback_query["id"])
        upsert_user_state(db, chat_id, username, first_name, f"admin_edit_svc_cup:{svc_id}")
        tg.send_message(chat_id, "✏️ Envía el nuevo precio en <b>CUP</b> para este servicio:", inline_cancel("admin_services"))
        return

    if data.startswith("admin_svc_del_"):
        if chat_id not in ADMIN_IDS:
            return
        svc_id = data.replace("admin_svc_del_", "")
        tg.answer_callback(callback_query["id"], "🗑️ Eliminado")
        db.update("bot_services", {"active": False, "updated_at": now_iso()}, {"id": eq(svc_id)})
        tg.send_message(chat_id, "✅ Servicio eliminado.", inline_cancel("admin_services", "🔙 Volver a Servicios"))
        return

    if data == "admin_svc_add":
        if chat_id not in ADMIN_IDS:
            return
        tg.answer_callback(callback_query["id"])
        upsert_user_state(db, chat_id, username, first_name, "admin_add_svc_id")
        db.upsert("bot_config", {"key": f"admin_temp_{chat_id}", "value": {}, "updated_at": now_iso()}, "key")
        tg.send_message(chat_id, "➕ <b>Agregar Servicio</b>\n\nEnvía el <b>ID</b> del servicio (ej: vpn, iptv):", inline_cancel("admin_services"))
        return

    if data in {"admin_svc_add_cat_service", "admin_svc_add_cat_tgp"}:
        if chat_id not in ADMIN_IDS:
            return
        tg.answer_callback(callback_query["id"])
        category = "telegram_premium" if data == "admin_svc_add_cat_tgp" else "service"
        row = db.select("bot_config", "value", filters={"key": eq(f"admin_temp_{chat_id}")}, single=True) or {}
        temp = row.get("value") or {}
        db.insert("bot_services", {
            "id": temp.get("id"), "name": temp.get("name"), "cup": js_parse_int(temp.get("cup")),
            "emoji": temp.get("emoji") or "📦", "category": category,
            "duration_months": js_parse_int(temp.get("duration"), 0) or None if category == "telegram_premium" else None,
            "sort_order": 0,
        })
        db.delete("bot_config", {"key": eq(f"admin_temp_{chat_id}")})
        upsert_user_state(db, chat_id, username, first_name, "menu")
        tg.send_message(chat_id, f"✅ Servicio <b>{e(temp.get('name'))}</b> agregado correctamente.", inline_cancel("admin_services", "🔙 Volver a Servicios"))
        return

    if data == "admin_svc_add_default_emoji":
        if chat_id not in ADMIN_IDS:
            return
        tg.answer_callback(callback_query["id"])
        row = db.select("bot_config", "value", filters={"key": eq(f"admin_temp_{chat_id}")}, single=True) or {}
        temp = row.get("value") or {}
        temp["emoji"] = "📦"
        db.upsert("bot_config", {"key": f"admin_temp_{chat_id}", "value": temp, "updated_at": now_iso()}, "key")
        upsert_user_state(db, chat_id, username, first_name, "menu")
        tg.send_message(chat_id, f"📦 <b>Nuevo servicio:</b>\n\n{e(temp.get('emoji'))} {e(temp.get('name'))}: <b>{temp.get('cup')} CUP</b>\n\nSelecciona la categoría:", {
            "reply_markup": {"inline_keyboard": [
                [{"text": "⚡ Servicio", "callback_data": "admin_svc_add_cat_service"}],
                [{"text": "✨ Telegram Premium", "callback_data": "admin_svc_add_cat_tgp"}],
                [{"text": "❌ Cancelar", "callback_data": "admin_services"}],
            ]}
        })
        return

    if data.startswith("sm_pkg_"):
        sm_amount = js_parse_int(data.replace("sm_pkg_", ""))
        pkg = next((p for p in sm_packages if js_parse_int(p.get("sm")) == sm_amount), None)
        if not pkg:
            tg.answer_callback(callback_query["id"], "❌ Paquete no encontrado")
            return
        tg.answer_callback(callback_query["id"], f"📱 {pkg.get('sm')} SM seleccionado")
        tg.send_message(chat_id, f"📱 <b>Paquete seleccionado:</b> {pkg.get('sm')} SM x {pkg.get('cup')} CUP\n\n¿Cómo deseas pagar?", {
            "reply_markup": {"inline_keyboard": [
                [{"text": "💳 Tarjeta CUP", "callback_data": f"sm_pay_card_{pkg.get('sm')}"}],
                [{"text": "📲 Bolsa Mi Transfer", "callback_data": f"sm_pay_transfer_{pkg.get('sm')}"}],
                [{"text": "❌ Cancelar", "callback_data": "cancel_to_tienda"}],
            ]}
        })
        return

    if data.startswith("sm_pay_card_") or data.startswith("sm_pay_transfer_"):
        is_card = data.startswith("sm_pay_card_")
        sm_amount = js_parse_int(data.replace("sm_pay_card_" if is_card else "sm_pay_transfer_", ""))
        pkg = next((p for p in sm_packages if js_parse_int(p.get("sm")) == sm_amount), {})
        tg.answer_callback(callback_query["id"])
        store_purchase(db, chat_id, f"Venta de SM: {pkg.get('sm')} SM ({pkg.get('cup')} CUP)", f"Buenas, he comprado {pkg.get('sm')} SM")
        upsert_user_state(db, chat_id, username, first_name, "sm_waiting_screenshot")
        if is_card:
            msg = (f"💳 <b>Pago por Tarjeta CUP</b>\n\nPaquete: <b>{pkg.get('sm')} SM - {pkg.get('cup')} CUP</b>\n\n"
                   f"Envía <b>{pkg.get('cup')} CUP</b> a la tarjeta:\n<code>{e(admin_cup_card)}</code>\n\n"
                   f"⚠️ <b>Por favor confirma al número: {e(admin_confirm_number)}</b>\n\n📸 Después de pagar, envía una <b>captura de pantalla</b> de la transferencia.")
        else:
            msg = (f"📲 <b>Pago por Bolsa Mi Transfer</b>\n\nPaquete: <b>{pkg.get('sm')} SM - {pkg.get('cup')} CUP</b>\n\n"
                   f"Envía <b>{pkg.get('cup')} CUP</b> a Mi Transfer:\n<code>{e(admin_mi_transfer)}</code>\n\n📸 Después de pagar, envía una <b>captura de pantalla</b> de la transferencia.")
        tg.send_message(chat_id, msg, inline_cancel())
        return

    if data in {"venta_pay_card", "venta_pay_transfer"}:
        is_card = data == "venta_pay_card"
        tg.answer_callback(callback_query["id"])
        upsert_user_state(db, chat_id, username, first_name, "venta_waiting_screenshot")
        if is_card:
            msg = f"💳 <b>Pago por Tarjeta CUP</b>\n\nEnvía los CUP a la tarjeta:\n<code>{e(admin_cup_card)}</code>\n\n⚠️ <b>Por favor confirma al número: {e(admin_confirm_number)}</b>\n\n📸 Después de pagar, envía una <b>captura de pantalla</b> de la transferencia."
        else:
            msg = f"📲 <b>Pago por Bolsa Mi Transfer</b>\n\nEnvía los CUP a Mi Transfer:\n<code>{e(admin_mi_transfer)}</code>\n\n📸 Después de pagar, envía una <b>captura de pantalla</b> de la transferencia."
        tg.send_message(chat_id, msg, inline_cancel())
        return

    if data.startswith("svc_") and not data.startswith("svc_tgp") and not data.startswith("svc_pay_"):
        svc_id = data.replace("svc_", "")
        svc = next((s for s in services if str(s.get("id")) == svc_id), None)
        if not svc:
            tg.answer_callback(callback_query["id"], "❌ Servicio no encontrado")
            return
        tg.answer_callback(callback_query["id"], f"{svc.get('emoji','📦')} {svc.get('name')}")
        tg.send_message(chat_id, f"{svc.get('emoji','📦')} <b>{e(svc.get('name'))}</b>\n\nPrecio: <b>{svc.get('cup')} CUP</b>\n\n¿Cómo deseas pagar?", {
            "reply_markup": {"inline_keyboard": [
                [{"text": "💳 Tarjeta CUP", "callback_data": f"svc_pay_card_{svc.get('id')}"}],
                [{"text": "📲 Bolsa Mi Transfer", "callback_data": f"svc_pay_transfer_{svc.get('id')}"}],
                [{"text": "❌ Cancelar", "callback_data": "cancel_to_tienda"}],
            ]}
        })
        return

    if data == "svc_tgp_menu":
        tg.answer_callback(callback_query["id"])
        buttons = [[{"text": f"{t.get('name')} - {t.get('cup')} CUP", "callback_data": f"svc_tgp_{t.get('id')}"}] for t in telegram_premium]
        buttons.append([{"text": "❌ Cancelar", "callback_data": "cancel_to_tienda"}])
        tg.send_message(chat_id, "✨ <b>Telegram Premium</b>\n\nElige la duración:", {"reply_markup": {"inline_keyboard": buttons}})
        return

    if data.startswith("svc_tgp_") and not data.startswith("svc_tgp_menu") and not data.startswith("svc_tgp_pay_"):
        tgp_id = data.replace("svc_tgp_", "")
        pkg = next((t for t in telegram_premium if str(t.get("id")) == tgp_id), None)
        if not pkg:
            tg.answer_callback(callback_query["id"], "❌ No encontrado")
            return
        tg.answer_callback(callback_query["id"], f"✨ {pkg.get('name')}")
        tg.send_message(chat_id, f"✨ <b>Telegram Premium - {e(pkg.get('name'))}</b>\n\nPrecio: <b>{pkg.get('cup')} CUP</b>\n\n¿Cómo deseas pagar?", {
            "reply_markup": {"inline_keyboard": [
                [{"text": "💳 Tarjeta CUP", "callback_data": f"svc_tgp_pay_card_{pkg.get('id')}"}],
                [{"text": "📲 Bolsa Mi Transfer", "callback_data": f"svc_tgp_pay_transfer_{pkg.get('id')}"}],
                [{"text": "❌ Cancelar", "callback_data": "cancel_to_tienda"}],
            ]}
        })
        return

    if data.startswith("svc_pay_card_") or data.startswith("svc_pay_transfer_"):
        is_card = data.startswith("svc_pay_card_")
        svc_id = data.replace("svc_pay_card_" if is_card else "svc_pay_transfer_", "")
        svc = next((s for s in services if str(s.get("id")) == svc_id), {})
        tg.answer_callback(callback_query["id"])
        store_purchase(db, chat_id, f"Servicio: {svc.get('name')} ({svc.get('cup')} CUP)", f"Buenas, he comprado el servicio {svc.get('name')}")
        upsert_user_state(db, chat_id, username, first_name, "svc_waiting_screenshot")
        if is_card:
            msg = f"💳 <b>Pago por Tarjeta CUP</b>\n\nServicio: <b>{e(svc.get('name'))} - {svc.get('cup')} CUP</b>\n\nEnvía <b>{svc.get('cup')} CUP</b> a la tarjeta:\n<code>{e(admin_cup_card)}</code>\n\n⚠️ <b>Por favor confirma al número: {e(admin_confirm_number)}</b>\n\n📸 Después de pagar, envía una <b>captura de pantalla</b> de la transferencia."
        else:
            msg = f"📲 <b>Pago por Bolsa Mi Transfer</b>\n\nServicio: <b>{e(svc.get('name'))} - {svc.get('cup')} CUP</b>\n\nEnvía <b>{svc.get('cup')} CUP</b> a Mi Transfer:\n<code>{e(admin_mi_transfer)}</code>\n\n📸 Después de pagar, envía una <b>captura de pantalla</b> de la transferencia."
        tg.send_message(chat_id, msg, inline_cancel())
        return

    if data.startswith("svc_tgp_pay_card_") or data.startswith("svc_tgp_pay_transfer_"):
        is_card = data.startswith("svc_tgp_pay_card_")
        tgp_id = data.replace("svc_tgp_pay_card_" if is_card else "svc_tgp_pay_transfer_", "")
        pkg = next((t for t in telegram_premium if str(t.get("id")) == tgp_id), {})
        tg.answer_callback(callback_query["id"])
        store_purchase(db, chat_id, f"Telegram Premium: {pkg.get('name')} ({pkg.get('cup')} CUP)", f"Buenas, he comprado Telegram Premium {pkg.get('name')}")
        upsert_user_state(db, chat_id, username, first_name, "svc_waiting_screenshot")
        if is_card:
            msg = f"💳 <b>Pago por Tarjeta CUP</b>\n\nServicio: <b>Telegram Premium {e(pkg.get('name'))} - {pkg.get('cup')} CUP</b>\n\nEnvía <b>{pkg.get('cup')} CUP</b> a la tarjeta:\n<code>{e(admin_cup_card)}</code>\n\n⚠️ <b>Por favor confirma al número: {e(admin_confirm_number)}</b>\n\n📸 Después de pagar, envía una <b>captura de pantalla</b> de la transferencia."
        else:
            msg = f"📲 <b>Pago por Bolsa Mi Transfer</b>\n\nServicio: <b>Telegram Premium {e(pkg.get('name'))} - {pkg.get('cup')} CUP</b>\n\nEnvía <b>{pkg.get('cup')} CUP</b> a Mi Transfer:\n<code>{e(admin_mi_transfer)}</code>\n\n📸 Después de pagar, envía una <b>captura de pantalla</b> de la transferencia."
        tg.send_message(chat_id, msg, inline_cancel())
        return

    if data == "admin_broadcast":
        if chat_id not in ADMIN_IDS:
            return
        tg.answer_callback(callback_query["id"])
        upsert_user_state(db, chat_id, username, first_name, "admin_broadcast_msg")
        tg.send_message(chat_id, "📢 <b>Enviar Mensaje a Todos</b>\n\nEscribe el mensaje que quieres enviar a todos los usuarios:", inline_cancel("admin_panel"))
        return

    if data == "payment_done":
        tg.answer_callback(callback_query["id"], "📸 Envía la captura")
        tg.send_message(chat_id, "📸 Por favor envía una <b>captura de pantalla</b> de la transferencia.")
        return

    tg.answer_callback(callback_query["id"])


def send_admin_menu(tg: Telegram, chat_id: int) -> None:
    tg.send_message(chat_id, "⚙️ <b>Panel de Administrador</b>\n\nSelecciona una opción:", {"reply_markup": {"inline_keyboard": [
        [{"text": "📊 Estadísticas", "callback_data": "admin_stats"}],
        [{"text": "💰 Tasas de Cambio", "callback_data": "admin_rates"}],
        [{"text": "📱 Paquetes SM", "callback_data": "admin_sm"}],
        [{"text": "📦 Servicios", "callback_data": "admin_services"}],
        [{"text": "📢 Enviar Mensaje a Todos", "callback_data": "admin_broadcast"}],
    ]}})


def handle_admin_text_input(tg: Telegram, db: Database, chat_id: int, username: Optional[str], first_name: Optional[str], step: str, text: str) -> None:
    if step == "admin_edit_buy_rate":
        val = js_parse_int(text.strip(), None)
        if val is None:
            tg.send_message(chat_id, "❌ Envía un número válido.")
            return
        db.upsert("bot_config", {"key": "buy_rate", "value": val, "updated_at": now_iso()}, "key")
        upsert_user_state(db, chat_id, username, first_name, "menu")
        tg.send_message(chat_id, f"✅ Tasa de compra actualizada a <b>{val} CUP</b>.", inline_cancel("admin_rates", "🔙 Volver"))
        return

    if step == "admin_edit_sell_rate":
        val = js_parse_int(text.strip(), None)
        if val is None:
            tg.send_message(chat_id, "❌ Envía un número válido.")
            return
        db.upsert("bot_config", {"key": "sell_rate", "value": val, "updated_at": now_iso()}, "key")
        upsert_user_state(db, chat_id, username, first_name, "menu")
        tg.send_message(chat_id, f"✅ Tasa de venta actualizada a <b>{val} CUP</b>.", inline_cancel("admin_rates", "🔙 Volver"))
        return

    if step == "admin_edit_sm_buy_rate":
        val = js_parse_float(text.strip(), None)
        if val is None or val <= 0:
            tg.send_message(chat_id, "❌ Envía un número válido (ej: 2.5).")
            return
        db.upsert("bot_config", {"key": "sm_buy_rate", "value": val, "updated_at": now_iso()}, "key")
        upsert_user_state(db, chat_id, username, first_name, "menu")
        tg.send_message(chat_id, f"✅ Tasa de compra SM actualizada a <b>{val:g}</b> (1 SM = {val:g} CUP).", inline_cancel("admin_rates", "🔙 Volver"))
        return

    if step.startswith("admin_edit_sm_cup:"):
        idx = js_parse_int(step.split(":", 1)[1], -1)
        val = js_parse_int(text.strip(), None)
        if val is None:
            tg.send_message(chat_id, "❌ Envía un número válido.")
            return
        row = db.select("bot_config", "value", filters={"key": eq("sm_packages")}, single=True) or {}
        pkgs = row.get("value") or []
        if 0 <= idx < len(pkgs):
            pkgs[idx]["cup"] = val
            db.upsert("bot_config", {"key": "sm_packages", "value": pkgs, "updated_at": now_iso()}, "key")
        upsert_user_state(db, chat_id, username, first_name, "menu")
        tg.send_message(chat_id, f"✅ Precio SM actualizado a <b>{val} CUP</b>.", inline_cancel("admin_sm", "🔙 Volver"))
        return

    if step.startswith("admin_edit_svc_cup:"):
        svc_id = step.split(":", 1)[1]
        val = js_parse_int(text.strip(), None)
        if val is None:
            tg.send_message(chat_id, "❌ Envía un número válido.")
            return
        db.update("bot_services", {"cup": val, "updated_at": now_iso()}, {"id": eq(svc_id)})
        upsert_user_state(db, chat_id, username, first_name, "menu")
        tg.send_message(chat_id, f"✅ Precio del servicio actualizado a <b>{val} CUP</b>.", inline_cancel("admin_services", "🔙 Volver"))
        return

    if step == "admin_add_svc_id":
        svc_id = clean_service_id(text)
        db.upsert("bot_config", {"key": f"admin_temp_{chat_id}", "value": {"id": svc_id}, "updated_at": now_iso()}, "key")
        upsert_user_state(db, chat_id, username, first_name, "admin_add_svc_name")
        tg.send_message(chat_id, "📝 Ahora envía el <b>nombre</b> del servicio:", inline_cancel("admin_services"))
        return

    if step == "admin_add_svc_name":
        row = db.select("bot_config", "value", filters={"key": eq(f"admin_temp_{chat_id}")}, single=True) or {}
        temp = row.get("value") or {}
        temp["name"] = text.strip()
        db.upsert("bot_config", {"key": f"admin_temp_{chat_id}", "value": temp, "updated_at": now_iso()}, "key")
        upsert_user_state(db, chat_id, username, first_name, "admin_add_svc_cup")
        tg.send_message(chat_id, "💰 Envía el <b>precio en CUP</b>:", inline_cancel("admin_services"))
        return

    if step == "admin_add_svc_cup":
        val = js_parse_int(text.strip(), None)
        if val is None:
            tg.send_message(chat_id, "❌ Envía un número válido.")
            return
        row = db.select("bot_config", "value", filters={"key": eq(f"admin_temp_{chat_id}")}, single=True) or {}
        temp = row.get("value") or {}
        temp["cup"] = val
        db.upsert("bot_config", {"key": f"admin_temp_{chat_id}", "value": temp, "updated_at": now_iso()}, "key")
        upsert_user_state(db, chat_id, username, first_name, "admin_add_svc_emoji")
        tg.send_message(chat_id, "🎨 Envía un <b>emoji</b> para el servicio (o envía 📦 para usar el predeterminado):", {"reply_markup": {"inline_keyboard": [
            [{"text": "📦 Usar predeterminado", "callback_data": "admin_svc_add_default_emoji"}],
            [{"text": "❌ Cancelar", "callback_data": "admin_services"}],
        ]}})
        return

    if step == "admin_add_svc_emoji":
        row = db.select("bot_config", "value", filters={"key": eq(f"admin_temp_{chat_id}")}, single=True) or {}
        temp = row.get("value") or {}
        temp["emoji"] = text.strip() or "📦"
        db.upsert("bot_config", {"key": f"admin_temp_{chat_id}", "value": temp, "updated_at": now_iso()}, "key")
        upsert_user_state(db, chat_id, username, first_name, "menu")
        tg.send_message(chat_id, f"📦 <b>Nuevo servicio:</b>\n\n{e(temp.get('emoji'))} {e(temp.get('name'))}: <b>{temp.get('cup')} CUP</b>\n\nSelecciona la categoría:", {"reply_markup": {"inline_keyboard": [
            [{"text": "⚡ Servicio", "callback_data": "admin_svc_add_cat_service"}],
            [{"text": "✨ Telegram Premium", "callback_data": "admin_svc_add_cat_tgp"}],
            [{"text": "❌ Cancelar", "callback_data": "admin_services"}],
        ]}})
        return

    if step == "admin_broadcast_msg":
        msg = text.strip()
        if not msg:
            tg.send_message(chat_id, "❌ El mensaje no puede estar vacío.")
            return
        users = db.select("telegram_user_state", "chat_id") or []
        sent = failed = 0
        for user in users:
            try:
                tg.send_message(int(user.get("chat_id")), f"📢 <b>Mensaje del Administrador:</b>\n\n{e(msg)}")
                sent += 1
                time.sleep(0.04)
            except Exception:
                failed += 1
        upsert_user_state(db, chat_id, username, first_name, "menu")
        tg.send_message(chat_id, f"✅ <b>Mensaje enviado</b>\n\n📤 Enviados: <b>{sent}</b>\n❌ Fallidos: <b>{failed}</b>", inline_cancel("admin_panel", "🔙 Volver"))


def process_update(update: Dict[str, Any]) -> None:
    tg = get_tg()
    db = get_db()
    cfg = load_config(db)
    if update.get("message"):
        handle_message(tg, db, update["message"], cfg)
    elif update.get("callback_query"):
        handle_callback_query(tg, db, update["callback_query"], cfg)


@app.route("/", methods=["GET", "POST"])
def root():
    if request.method == "GET":
        missing = []
        if not BOT_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not SUPABASE_URL:
            missing.append("SUPABASE_URL")
        if not SUPABASE_SERVICE_ROLE_KEY:
            missing.append("SUPABASE_SERVICE_ROLE_KEY")
        status = 500 if missing else 200
        return jsonify({"ok": not missing, "mode": "webhook" if WEBHOOK_ONLY else "polling", "missing": missing}), status

    try:
        update = request.get_json(force=True, silent=False)
        if isinstance(update, dict) and update.get("update_id") is not None:
            process_update(update)
        return jsonify({"ok": True})
    except Exception as exc:
        log.exception("Error processing webhook update")
        return jsonify({"ok": False, "error": str(exc)}), 200


@app.route("/health", methods=["GET"])
def health():
    return root()


def register_webhook_once() -> None:
    if not BOT_TOKEN or not WEBHOOK_URL:
        return
    try:
        url = WEBHOOK_URL.rstrip("/")
        data = get_tg().set_webhook(url)
        log.info("Set webhook result: %s", data)
    except Exception:
        log.exception("Failed to set webhook")


def polling_loop() -> None:
    tg = get_tg()
    db = get_db()
    state = db.select("telegram_bot_state", "update_offset", filters={"id": eq(1)}, single=True) or {"update_offset": 0}
    offset = int(state.get("update_offset") or 0)
    log.info("Starting polling at offset %s", offset)
    while True:
        try:
            data = tg.get_updates(offset, timeout=50)
            updates = data.get("result") or []
            if not updates:
                continue
            for update in updates:
                try:
                    process_update(update)
                except Exception:
                    log.exception("Error processing update")
            offset = max(int(u.get("update_id", 0)) for u in updates) + 1
            db.update("telegram_bot_state", {"update_offset": offset, "updated_at": now_iso()}, {"id": eq(1)})
        except Exception:
            log.exception("Polling error")
            time.sleep(5)


if WEBHOOK_URL and BOT_TOKEN:
    threading.Thread(target=register_webhook_once, daemon=True).start()

if __name__ == "__main__":
    if not WEBHOOK_ONLY:
        polling_loop()
    else:
        app.run(host="0.0.0.0", port=PORT)
