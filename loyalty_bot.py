import os
import json
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import gspread
from gspread.auth import service_account_from_dict


# === ENV НАСТРОЙКИ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

GSSERVICEJSON = os.getenv("GSSERVICEJSON")  # JSON ключ сервис-аккаунта
GSSHEETID = os.getenv("GSSHEETID")          # ID таблицы в Google Sheets

PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("BASE_URL")

# Ожидаемые листы:
# Sheet "clients": phone | name | created_at | turnover | bonus_balance | level
# Sheet "transactions": phone | type | amount | bonus_delta | ts | comment

GSCLIENT = None
GS_SHEET = None
CLIENTS_WS = None
TX_WS = None
TG_LINKS_WS = None  # лист для связок user_id <-> phone


# === GOOGLE SHEETS ===

def init_gs():
    """Инициализация Google Sheets (вызывать перед операциями)."""
    global GSCLIENT, GS_SHEET, CLIENTS_WS, TX_WS, TG_LINKS_WS
    if GSCLIENT is not None:
        return

    if not GSSERVICEJSON or not GSSHEETID:
        print("No GS creds in env (GSSERVICEJSON/GSSHEETID)")
        return

    info = json.loads(GSSERVICEJSON)
    client = service_account_from_dict(info)
    sheet = client.open_by_key(GSSHEETID)

    try:
        tg_links_ws = sheet.worksheet("tg_links")
    except Exception:
        tg_links_ws = None
        
    try:
        clients_ws = sheet.worksheet("clients")
    except gspread.exceptions.WorksheetNotFound:
        clients_ws = sheet.add_worksheet("clients", rows=1000, cols=10)
        clients_ws.append_row(
            ["phone", "name", "created_at", "turnover", "bonus_balance", "level"],
            value_input_option="RAW",
        )

    try:
        tx_ws = sheet.worksheet("transactions")
    except gspread.exceptions.WorksheetNotFound:
        tx_ws = sheet.add_worksheet("transactions", rows=2000, cols=10)
        tx_ws.append_row(
            ["phone", "type", "amount", "bonus_delta", "ts", "comment"],
            value_input_option="RAW",
        )

    GSCLIENT = client
    GS_SHEET = sheet
    CLIENTS_WS = clients_ws
    TX_WS = tx_ws
    TG_LINKS_WS = tg_links_ws

    print("Google Sheets initialized")


def find_client_by_phone(phone: str):
    """Поиск клиента в листе clients по телефону."""
    if CLIENTS_WS is None:
        return None
    records = CLIENTS_WS.get_all_records()
    for r in records:
        if str(r.get("phone", "")).strip() == phone.strip():
            return r
    return None

def get_phone_by_user_id(user_id: int) -> str | None:
    """Возвращает телефон, привязанный к Telegram user_id, из листа tg_links."""
    if TG_LINKS_WS is None:
        return None
    try:
        records = TG_LINKS_WS.get_all_records()
        for r in records:
            uid = str(r.get("user_id", "")).strip()
            if uid == str(user_id):
                phone = str(r.get("phone", "")).strip()
                return phone or None
    except Exception as e:
        print(f"get_phone_by_user_id error: {e}")
    return None


def link_user_to_phone(user, phone: str):
    """Создаёт или обновляет связь user_id <-> phone в листе tg_links."""
    if TG_LINKS_WS is None:
        return
    try:
        records = TG_LINKS_WS.get_all_records()
        user_id_str = str(user.id)
        row_index = None
        for idx, r in enumerate(records, start=2):
            if str(r.get("user_id", "")).strip() == user_id_str:
                row_index = idx
                break

        now = datetime.utcnow().isoformat(timespec="seconds")
        row_values = [
            user_id_str,
            user.username or "",
            user.first_name or "",
            phone,
            now,
        ]

        if row_index is None:
            TG_LINKS_WS.append_row(row_values, value_input_option="RAW")
        else:
            TG_LINKS_WS.update(f"A{row_index}:E{row_index}", [row_values])
    except Exception as e:
        print(f"link_user_to_phone error: {e}")


def upsert_client(phone: str, name: str | None = None):
    """Создать или обновить клиента (имя можно обновлять)."""
    if CLIENTS_WS is None:
        return None

    records = CLIENTS_WS.get_all_records()
    row_idx = None
    for idx, r in enumerate(records, start=2):  # 1 строка — заголовок
        if str(r.get("phone", "")).strip() == phone.strip():
            row_idx = idx
            break

    now = datetime.utcnow().isoformat(timespec="seconds")

    if row_idx is None:
        # новый клиент
        row = [
            phone,
            name or "",
            now,
            0,          # turnover
            0,          # bonus_balance
            "base",     # level
        ]
        CLIENTS_WS.append_row(row, value_input_option="RAW")
        return {
            "phone": phone,
            "name": name or "",
            "created_at": now,
            "turnover": 0,
            "bonus_balance": 0,
            "level": "base",
        }
    else:
        # обновляем имя, если есть
        existing = records[row_idx - 2]
        new_name = name or existing.get("name", "")
        # обновление только имени (чтобы не трогать оборот/бонусы)
        CLIENTS_WS.update_cell(row_idx, 2, new_name)
        existing["name"] = new_name
        return existing

def update_client_row(client_dict):
    """Полностью обновить строку клиента по phone."""
    if CLIENTS_WS is None:
        return
    phone = str(client_dict.get("phone", "")).strip()
    if not phone:
        return
    records = CLIENTS_WS.get_all_records()
    for idx, r in enumerate(records, start=2):  # строка в Sheets = idx
        if str(r.get("phone", "")).strip() == phone:
            # обновляем диапазон A:F в найденной строке
            CLIENTS_WS.update(
                f"A{idx}:F{idx}",
                [[
                    phone,
                    client_dict.get("name", ""),
                    client_dict.get("created_at", ""),
                    client_dict.get("turnover", 0),
                    client_dict.get("bonus_balance", 0),
                    client_dict.get("level", "base"),
                ]],
            )
            return

def log_transaction(phone: str, tx_type: str, amount: float, bonus_delta: float, comment: str = ""):
    """Запись транзакции в лист transactions."""
    if TX_WS is None:
        return
    ts = datetime.utcnow().isoformat(timespec="seconds")
    TX_WS.append_row(
        [phone, tx_type, amount, bonus_delta, ts, comment],
        value_input_option="RAW",
    )


# === ЛОГИКА УРОВНЕЙ И БОНУСОВ ===

def calc_level_and_rate(turnover: float) -> tuple[str, float]:
    """Возвращает (уровень, процент_начисления_бонусов)."""
    if turnover >= 30000:
        return "gold", 0.10
    elif turnover >= 10000:
        return "silver", 0.07
    else:
        return "base", 0.05


def describe_level(level: str) -> str:
    """Описание уровня для клиента (мотивационный текст)."""
    if level == "gold":
        return (
            "Ваш уровень: ЗОЛОТО ✨\n"
            "Вы — VIP гость нашего фото-ателье: 10% от каждой покупки возвращаются к вам в виде бонусов.\n"
            "Бонусами можно оплатить до 30% суммы следующей покупки.\n"           
            "Чем чаще вы к нам заходите, тем выгоднее каждая новая услуга."
        )
    elif level == "silver":
        return (
            "Ваш уровень: СЕРЕБРО ⭐️\n"
            "Вы уже в числе наших любимых клиентов: 7% от каждой покупки возвращаются на бонусный счёт.\n"
            "Бонусами можно оплатить до 20% суммы следующей покупки.\n"   
            "Делайте ещё заказы — и уровень поднимется до Золота."
        )
    else:
        return (
            "Ваш уровень: БАЗОВЫЙ 💎\n"
            "С каждого заказа вы получаете 5% в виде бонусов.\n"
            "Бонусами можно оплатить до 10% суммы следующей покупки.\n"   
            "Накопленные бонусы можно тратить на следующие услуги — приятно возвращаться, когда каждый визит окупается."
        )

def format_client_cabinet(client, phone: str) -> str:
    """Текст личного кабинета для клиента."""
    name = client.get("name") or "Клиент"
    level = client.get("level", "base")
    turnover = float(client.get("turnover", 0) or 0)
    bonus = float(client.get("bonus_balance", 0) or 0)

    lvl_text = describe_level(level)

    text = (
        f"{name}, добро пожаловать в ваш личный кабинет программы лояльности 📸\n\n"
        f"Телефон: {phone}\n"
        f"Накопленный оборот: {turnover:.0f}₽\n"
        f"Бонусный счёт: {bonus:.0f} бонусов\n\n"
        f"{lvl_text}\n\n"
        "Каждая печать фото, ксерокс, скан или услуга в ателье — это ещё один шаг к новым бонусам.\n"
        "Вы можете копить их и списывать частично за услуги 😉"
    )
    return text


# === HANDLERS ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветствие клиента."""
    user = update.effective_user

    keyboard = [
        [InlineKeyboardButton("🔐 Открыть личный кабинет", callback_data="cabinet_open")]
    ]

    text = (
        "Привет! Я бот программы лояльности Фото Химки.\n\n"
        "Каждый ваш визит — это не только красивые снимки и распечатки, "
        "но и бонусы, которые возвращаются к Вам.\n\n"
        "Нажмите кнопку ниже, чтобы открыть свой личный кабинет и посмотреть, "
        "какой уровень и сколько бонусов вы уже накопили,"
        "а так же сколько осталось потратить до следующего уровня"
    )

    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Доступ запрещён.")
        return
    await update.message.reply_text(
        "🔑 Админ-режим.\n"
        "Отправьте номер телефона клиента (в любом удобном формате)."
    )
    context.user_data["admin_mode"] = True
    context.user_data["admin_step"] = "await_phone"

TG_LINKS_WS = None  # уже есть глобально

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на Inline-кнопки."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user

    # Личный кабинет клиента
    if data == "cabinet_open":
        init_gs()

        # 1) Пробуем найти телефон по user_id
        linked_phone = get_phone_by_user_id(user.id)

        if linked_phone:
            client = find_client_by_phone(linked_phone)
            if not client:
                client = upsert_client(linked_phone, user.full_name or "")

            turnover = float(client.get("turnover", 0) or 0)
            level, _ = calc_level_and_rate(turnover)
            if client.get("level") != level:
                client["level"] = level
                update_client_row(client)

            context.user_data["client_phone"] = linked_phone
            cabinet_text = format_client_cabinet(client, linked_phone)
            await query.edit_message_text(cabinet_text)
            return

        # 2) Если привязки нет — просим телефон
        context.user_data["awaiting_phone_for_cabinet"] = True
        await query.edit_message_text(
            "Введите ваш номер телефона в формате 89XXXXXXXXX\n\n"
            "Мы найдём ваш профиль в системе лояльности или создадим новый, "
            "чтобы вы могли копить бонусы и получать выгоду из каждого посещения."
        )
        return

    # Админские кнопки
    if data == "admin_purchase":
        context.user_data["admin_step"] = "await_purchase_sum"
        await query.edit_message_text(
            "💰 Введите сумму покупки (в рублях):\n"
            "Например: 450 или 450.50"
        )
        return
        
    if data == "admin_redeem":
        context.user_data["admin_step"] = "await_redeem_sum"
        await query.edit_message_text(
            "🎁 Введите сумму бонусов для списания:\n"
            "Например: 100 или 250.50"
        )
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений (телефон, суммы и т.д.)."""
    text = (update.message.text or "").strip()
    user = update.effective_user

    # 1) Клиент вводит телефон для личного кабинета
    if context.user_data.get("awaiting_phone_for_cabinet"):
        context.user_data["awaiting_phone_for_cabinet"] = False
        phone = text  # сюда можно потом добавить нормализацию

        init_gs()
        client = find_client_by_phone(phone)
        if not client:
            client = upsert_client(phone, user.full_name or "")

        # актуализируем уровень/процент, если что-то поменялось
        turnover = float(client.get("turnover", 0) or 0)
        level, _ = calc_level_and_rate(turnover)
        if client.get("level") != level:
            client["level"] = level
            update_client_row(client)

        # ПРИВЯЗЫВАЕМ user_id ↔ phone
        link_user_to_phone(user, phone)
        context.user_data["client_phone"] = phone

        cabinet_text = format_client_cabinet(client, phone)
        await update.message.reply_text(cabinet_text)
        return

        # 2) Админский сценарий
    if context.user_data.get("admin_mode"):
        step = context.user_data.get("admin_step")

        # 2.1. Получаем телефон клиента
        if step == "await_phone":
            phone = text.strip()
            context.user_data["admin_client_phone"] = phone
            init_gs()
            client = find_client_by_phone(phone)
            if not client:
                client = upsert_client(phone, "")

            turnover = float(client.get("turnover", 0) or 0)
            level, _ = calc_level_and_rate(turnover)
            if client.get("level") != level:
                client["level"] = level
                update_client_row(client)

            bonus = float(client.get("bonus_balance", 0) or 0)
            name = client.get("name", "") or "Клиент"

            keyboard = [
                [InlineKeyboardButton("➕ Покупка", callback_data="admin_purchase")],
                [InlineKeyboardButton("➖ Списать бонусы", callback_data="admin_redeem")],
            ]

            await update.message.reply_text(
                f"Профиль клиента:\n\n"
                f"Имя: {name}\n"
                f"Телефон: {phone}\n"
                f"Уровень: {level}\n"
                f"Оборот: {turnover:.0f}₽\n"
                f"Бонусы: {bonus:.0f}\n\n"
                "Выберите действие:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            context.user_data["admin_step"] = "menu"
            return

        # 2.2. Ввод суммы покупки
        if step == "await_purchase_sum":
            phone = context.user_data.get("admin_client_phone")
            if not phone:
                await update.message.reply_text(
                    "❗ Телефон клиента не найден в сессии. Отправь /admin и введи телефон заново."
                )
                context.user_data["admin_step"] = "await_phone"
                return

            try:
                amount = float(text.replace(",", "."))
            except ValueError:
                await update.message.reply_text("⚠️ Неверный формат суммы. Попробуйте ещё раз.")
                return

            init_gs()
            client = find_client_by_phone(phone)
            if not client:
                await update.message.reply_text("Клиент не найден (возможно, ошибка номера).")
                context.user_data["admin_step"] = "await_phone"
                return

            turnover = float(client.get("turnover", 0) or 0)
            bonus_balance = float(client.get("bonus_balance", 0) or 0)

            new_turnover = turnover + amount
            level, rate = calc_level_and_rate(new_turnover)
            bonus_delta = round(amount * rate)
            new_bonus_balance = bonus_balance + bonus_delta

            client["turnover"] = new_turnover
            client["bonus_balance"] = new_bonus_balance
            client["level"] = level
            update_client_row(client)

            log_transaction(phone, "purchase", amount, bonus_delta, "Покупка в ателье")

            await update.message.reply_text(
                f"✅ Покупка на {amount:.0f}₽ успешно добавлена.\n"
                f"Начислено бонусов: {bonus_delta:.0f}.\n"
                f"Новый баланс бонусов: {new_bonus_balance:.0f}.\n"
                f"Текущий уровень клиента: {level}."
            )

            context.user_data["admin_step"] = "menu"
            return

        # 2.3. Ввод суммы списания бонусов
        if step == "await_redeem_sum":
            phone = context.user_data.get("admin_client_phone")
            if not phone:
                await update.message.reply_text(
                    "❗ Телефон клиента не найден в сессии. Отправь /admin и введи телефон заново."
                )
                context.user_data["admin_step"] = "await_phone"
                return

            try:
                redeem = float(text.replace(",", "."))
            except ValueError:
                await update.message.reply_text("⚠️ Неверное число. Попробуй ещё раз.")
                return

            init_gs()
            client = find_client_by_phone(phone)
            if not client:
                await update.message.reply_text("Клиент не найден (возможно, ошибка номера).")
                context.user_data["admin_step"] = "await_phone"
                return

            bonus_balance = float(client.get("bonus_balance", 0) or 0)
            if redeem > bonus_balance:
                await update.message.reply_text(
                    f"Недостаточно бонусов для списания.\n"
                    f"Текущий баланс: {bonus_balance:.0f}."
                )
                return

            new_balance = bonus_balance - redeem
            client["bonus_balance"] = new_balance
            update_client_row(client)

            log_transaction(phone, "redeem", 0, -redeem, "Списание бонусов")

            await update.message.reply_text(
                f"🎁 Списано бонусов: {redeem:.0f}.\n"
                f"Новый баланс бонусов: {new_balance:.0f}."
            )

            context.user_data["admin_step"] = "menu"
            return

    # Если текст не попал ни в один сценарий
    await update.message.reply_text(
        "Сообщение не распознано.\n\n"
        "Клиент: используйте /start, чтобы открыть личный кабинет.\n"
        "Админ: используйте /admin для работы с клиентами."
    )


# === MAIN ===

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in environment")

    if not BASE_URL:
        raise RuntimeError("BASE_URL is not set in environment")

    init_gs()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # URL, по которому Telegram будет стучаться
    webhook_path = BOT_TOKEN  # можно любое, но токен — удобно
    webhook_url = f"{BASE_URL}/{webhook_path}"

    print("Starting loyalty bot with webhook...")
    print(f"Listening on 0.0.0.0:{PORT}, webhook URL = {webhook_url}")

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
