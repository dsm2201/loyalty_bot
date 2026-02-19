import os
from datetime import datetime
import json

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters,
)
import gspread
from gspread.auth import service_account_from_dict

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

GSSERVICEJSON = os.getenv("GSSERVICEJSON")  # JSON ключ сервис-аккаунта
GSSHEETID = os.getenv("GSSHEETID")          # ID таблицы

GSCLIENT = None
GS_SHEET = None
CLIENTS_WS = None
TX_WS = None

def init_gs():
    global GSCLIENT, GS_SHEET, CLIENTS_WS, TX_WS
    if not GSSERVICEJSON or not GSSHEETID:
        print("No GS creds in env")
        return
    info = json.loads(GSSERVICEJSON)
    client = service_account_from_dict(info)
    sheet = client.open_by_key(GSSHEETID)

    CLIENTS_WS = sheet.worksheet("clients")
    TX_WS = sheet.worksheet("transactions")

    GSCLIENT = client
    GS_SHEET = sheet
    print("Google Sheets inited")
def find_client_by_phone(phone: str):
    if CLIENTS_WS is None:
        return None
    records = CLIENTS_WS.get_all_records()
    for r in records:
        if str(r.get("phone", "")).strip() == phone.strip():
            return r
    return None

def create_or_update_client(phone: str, name: str):
    if CLIENTS_WS is None:
        return
    records = CLIENTS_WS.get_all_records()
    row_idx = None
    for idx, r in enumerate(records, start=2):  # row 1 = header
        if str(r.get("phone", "")).strip() == phone.strip():
            row_idx = idx
            break
    now = datetime.utcnow().isoformat(timespec="seconds")
    if row_idx is None:
        CLIENTS_WS.append_row([phone, name, now, 0, 0, "base"], value_input_option="RAW")
    else:
        # Обновим имя, если поменялось
        CLIENTS_WS.update_cell(row_idx, 2, name)

def log_transaction(phone: str, tx_type: str, amount: float, bonus_delta: float, comment: str = ""):
    if TX_WS is None:
        return
    ts = datetime.utcnow().isoformat(timespec="seconds")
    TX_WS.append_row(
        [phone, tx_type, amount, bonus_delta, ts, comment],
        value_input_option="RAW"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keyboard = [
        [InlineKeyboardButton("🔐 Личный кабинет", callback_data="cabinet_open")]
    ]
    await update.message.reply_text(
        "Привет! Это бот системы лояльности фото-ателье.\n"
        "Нажми кнопку, чтобы открыть личный кабинет.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cabinet_open":
        await query.edit_message_text("Введите ваш номер телефона в формате +79XXXXXXXXX")
        context.user_data["awaiting_phone_for_cabinet"] = True

    if data == "admin_purchase":
        context.user_data["admin_step"] = "await_purchase_sum"
        await query.edit_message_text("Введи сумму покупки (в рублях):")
        return

    if data == "admin_redeem":
        context.user_data["admin_step"] = "await_redeem_sum"
        await query.edit_message_text("Введи, сколько бонусов списать:")
        return

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user = update.effective_user

    # Клиент вводит телефон
    if context.user_data.get("awaiting_phone_for_cabinet"):
        context.user_data["awaiting_phone_for_cabinet"] = False
        phone = text
        init_gs()
        client = find_client_by_phone(phone)
        if not client:
            create_or_update_client(phone, user.full_name or "")
            client = find_client_by_phone(phone)

        level = client.get("level", "base")
        bonus = client.get("bonus_balance", 0)
        await update.message.reply_text(
            f"Ваш телефон: {phone}\n"
            f"Уровень: {level}\n"
            f"Бонусы: {bonus}"
        )
        return
    # Админский сценарий
    if context.user_data.get("admin_mode"):
        step = context.user_data.get("admin_step")

        if step == "await_phone":
            phone = text
            init_gs()
            client = find_client_by_phone(phone)
            if not client:
                create_or_update_client(phone, "")
                client = find_client_by_phone(phone)

            context.user_data["admin_client_phone"] = phone
            bonus = client.get("bonus_balance", 0)
            level = client.get("level", "base")
            turnover = client.get("turnover", 0)

            keyboard = [
                [InlineKeyboardButton("➕ Покупка", callback_data="admin_purchase")],
                [InlineKeyboardButton("➖ Списать бонусы", callback_data="admin_redeem")]
            ]
            await update.message.reply_text(
                f"Клиент: {phone}\n"
                f"Уровень: {level}\n"
                f"Оборот: {turnover}\n"
                f"Бонусы: {bonus}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data["admin_step"] = "menu"
            return
        if step == "await_purchase_sum":
            phone = context.user_data.get("admin_client_phone")
            try:
                amount = float(text.replace(",", "."))
            except ValueError:
                await update.message.reply_text("Неверная сумма, попробуй ещё раз.")
                return
            init_gs()
            client = find_client_by_phone(phone)
            if not client:
                await update.message.reply_text("Клиент не найден.")
                return

            # Простая логика начисления: 5% от суммы
            bonus_delta = round(amount * 0.05)
            # обновляем оборот и бонусы
            turnover = float(client.get("turnover", 0) or 0) + amount
            bonus_balance = float(client.get("bonus_balance", 0) or 0) + bonus_delta

            # найдём строку и обновим
            records = CLIENTS_WS.get_all_records()
            for idx, r in enumerate(records, start=2):
                if str(r.get("phone", "")).strip() == phone:
                    CLIENTS_WS.update_row(idx, [
                        phone,
                        r.get("name", ""),
                        r.get("created_at", ""),
                        turnover,
                        bonus_balance,
                        r.get("level", "base"),
                    ])
                    break

            log_transaction(phone, "purchase", amount, bonus_delta, "Покупка в ателье")
            await update.message.reply_text(
                f"Покупка на {amount}₽.\n"
                f"Начислено бонусов: {bonus_delta}.\n"
                f"Новый баланс: {bonus_balance}."
            )
            context.user_data["admin_step"] = "menu"
            return

        if step == "await_redeem_sum":
            phone = context.user_data.get("admin_client_phone")
            try:
                redeem = float(text.replace(",", "."))
            except ValueError:
                await update.message.reply_text("Неверное число, попробуй ещё раз.")
                return
            init_gs()
            client = find_client_by_phone(phone)
            if not client:
                await update.message.reply_text("Клиент не найден.")
                return

            bonus_balance = float(client.get("bonus_balance", 0) or 0)
            if redeem > bonus_balance:
                await update.message.reply_text(
                    f"Недостаточно бонусов. Текущий баланс: {bonus_balance}."
                )
                return

            new_balance = bonus_balance - redeem

            records = CLIENTS_WS.get_all_records()
            for idx, r in enumerate(records, start=2):
                if str(r.get("phone", "")).strip() == phone:
                    CLIENTS_WS.update_row(idx, [
                        phone,
                        r.get("name", ""),
                        r.get("created_at", ""),
                        r.get("turnover", 0),
                        new_balance,
                        r.get("level", "base"),
                    ])
                    break

            log_transaction(phone, "redeem", 0, -redeem, "Списание бонусов")
            await update.message.reply_text(
                f"Списано бонусов: {redeem}.\n"
                f"Новый баланс: {new_balance}."
            )
            context.user_data["admin_step"] = "menu"
            return


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    await update.message.reply_text(
        "Админ-режим.\n"
        "Отправь номер телефона клиента, которого хочешь найти/создать."
    )
    context.user_data["admin_mode"] = True
    context.user_data["admin_step"] = "await_phone"

def main():
    if not BOT_TOKEN:
        raise RuntimeError("No BOT_TOKEN in env")

    init_gs()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Starting loyalty bot...")
    app.run_polling()

if __name__ == "__main__":
    main()

