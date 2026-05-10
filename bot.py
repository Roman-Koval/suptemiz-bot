"""
SupTemiz Telegram Bot — Railway deployment
"""

import os
import logging
import asyncio
import threading
import json

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
import firebase_admin
from firebase_admin import credentials, firestore

# =========================
# Конфиг из переменных окружения
# =========================
BOT_TOKEN     = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])

_sa_dict = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
cred = credentials.Certificate(_sa_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# =========================
# Метки
# =========================
STATUS_LABELS = {
    "pending":     "⏳ В ожидании",
    "confirmed":   "✅ Подтверждён",
    "in_progress": "🔄 В процессе",
    "done":        "✔️ Завершён",
    "cancelled":   "❌ Отменён",
}
TYPE_LABELS = {
    "standard": "Стандартная",
    "deep":     "Генеральная",
    "office":   "Офис",
}

def status_label(s):
    return STATUS_LABELS.get(s, s or "—")

def format_order(order):
    return (
        f"🧽 <b>Заказ {order.get('id','—')}</b>\n\n"
        f"👤 <b>Имя:</b> {order.get('name','—')}\n"
        f"📱 <b>Телефон:</b> {order.get('phone','—')}\n"
        f"📍 <b>Район:</b> {order.get('area','—')}\n"
        f"🏠 <b>Тип:</b> {TYPE_LABELS.get(order.get('type',''),'—')}\n"
        f"📐 <b>Площадь:</b> {order.get('areaSize','—')} м²\n"
        f"💰 <b>Цена:</b> {order.get('price','—')} ₺\n"
        f"📅 <b>Дата:</b> {order.get('date','—')} {order.get('time','')}\n"
        f"💬 <b>Коммент:</b> {order.get('comment') or '—'}\n"
        f"🔄 <b>Статус:</b> {status_label(order.get('status'))}"
    )

def status_keyboard(doc_id, current_status):
    buttons = [
        ("✅ Подтвердить",  "confirmed"),
        ("▶️ В процессе",   "in_progress"),
        ("✔️ Завершить",    "done"),
        ("❌ Отменить",     "cancelled"),
    ]
    kb = [
        [InlineKeyboardButton(label, callback_data=f"status:{doc_id}:{st}")]
        for label, st in buttons if st != current_status
    ]
    kb.append([InlineKeyboardButton("📋 Список заказов", callback_data="cmd:list")])
    return InlineKeyboardMarkup(kb)

def is_admin(update: Update) -> bool:
    return (update.effective_chat.id if update.effective_chat else None) == ADMIN_CHAT_ID

# =========================
# Команды
# =========================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"👋 <b>SupTemiz Admin Bot</b>\n"
        f"Ваш chat_id: <code>{chat_id}</code>\n\n"
        f"/list — последние 20 заказов\n"
        f"/pending — заказы в ожидании\n"
        f"/stats — статистика",
        parse_mode="HTML"
    )

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️ Нет доступа")
        return
    docs = list(
        db.collection("orders")
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(20)
        .stream()
    )
    if not docs:
        await update.message.reply_text("Заказов нет")
        return
    for d in docs:
        order = d.to_dict()
        await update.message.reply_text(
            format_order(order),
            parse_mode="HTML",
            reply_markup=status_keyboard(d.id, order.get("status", ""))
        )

async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️ Нет доступа")
        return
    docs = list(
        db.collection("orders")
        .where("status", "==", "pending")
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .stream()
    )
    if not docs:
        await update.message.reply_text("✅ Нет заказов в ожидании")
        return
    for d in docs:
        order = d.to_dict()
        await update.message.reply_text(
            format_order(order),
            parse_mode="HTML",
            reply_markup=status_keyboard(d.id, "pending")
        )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️ Нет доступа")
        return
    docs = list(db.collection("orders").stream())
    counts = {k: 0 for k in STATUS_LABELS}
    revenue = 0
    for d in docs:
        o = d.to_dict()
        s = o.get("status", "")
        if s in counts:
            counts[s] += 1
        if s == "done":
            try:
                revenue += float(o.get("price", 0) or 0)
            except (ValueError, TypeError):
                pass
    await update.message.reply_text(
        f"📊 <b>Статистика SupTemiz</b>\n\n"
        f"Всего заказов: <b>{len(docs)}</b>\n"
        f"⏳ В ожидании: <b>{counts['pending']}</b>\n"
        f"✅ Подтверждено: <b>{counts['confirmed']}</b>\n"
        f"🔄 В процессе: <b>{counts['in_progress']}</b>\n"
        f"✔️ Завершено: <b>{counts['done']}</b>\n"
        f"❌ Отменено: <b>{counts['cancelled']}</b>\n\n"
        f"💰 Выручка (завершённые): <b>{int(revenue):,} ₺</b>",
        parse_mode="HTML"
    )

# =========================
# Inline-кнопки — смена статуса
# =========================
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(update):
        await query.answer("⛔️ Нет доступа", show_alert=True)
        return

    data = query.data

    if data.startswith("status:"):
        _, doc_id, new_status = data.split(":", 2)
        try:
            ref = db.collection("orders").document(doc_id)
            ref.update({"status": new_status})
            order = ref.get().to_dict()
            await query.edit_message_text(
                format_order(order),
                parse_mode="HTML",
                reply_markup=status_keyboard(doc_id, new_status)
            )
        except Exception as e:
            log.error(f"Status update error: {e}")
            await query.answer(f"Ошибка: {e}", show_alert=True)

    elif data == "cmd:list":
        await cmd_list(update, ctx)

# =========================
# Firestore listener — уведомления о новых заказах
# Запускается в отдельном потоке, отправляет через event loop бота
# =========================
def start_firestore_listener(loop, bot):
    """
    Слушает новые документы в коллекции orders.
    loop — event loop бота (из asyncio)
    bot — объект Bot для отправки сообщений
    """
    seen_ids = set()

    # Загружаем существующие ID чтобы не уведомлять при старте
    for d in db.collection("orders").stream():
        seen_ids.add(d.id)
    log.info(f"Listener инициализирован, существующих заказов: {len(seen_ids)}")

    def on_snapshot(col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name == "ADDED" and change.document.id not in seen_ids:
                seen_ids.add(change.document.id)
                order = change.document.to_dict()
                doc_id = change.document.id
                log.info(f"Новый заказ: {doc_id}")

                text = "🔔 <b>НОВЫЙ ЗАКАЗ!</b>\n\n" + format_order(order)
                kb   = status_keyboard(doc_id, "pending")

                async def send():
                    try:
                        await bot.send_message(
                            chat_id=ADMIN_CHAT_ID,
                            text=text,
                            parse_mode="HTML",
                            reply_markup=kb
                        )
                        log.info(f"Уведомление о заказе {doc_id} отправлено")
                    except Exception as e:
                        log.error(f"Ошибка отправки уведомления: {e}")

                # Безопасно запускаем корутину в event loop бота
                asyncio.run_coroutine_threadsafe(send(), loop)

    # Подписываемся на коллекцию
    db.collection("orders").on_snapshot(on_snapshot)
    log.info("Firestore listener запущен и слушает новые заказы")

    # Держим поток живым
    threading.Event().wait()

# =========================
# Запуск
# =========================
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start",   cmd_start))
    application.add_handler(CommandHandler("list",    cmd_list))
    application.add_handler(CommandHandler("pending", cmd_pending))
    application.add_handler(CommandHandler("stats",   cmd_stats))
    application.add_handler(CallbackQueryHandler(on_callback))

    # Получаем event loop и запускаем listener ДО polling
    loop = asyncio.get_event_loop()

    listener_thread = threading.Thread(
        target=start_firestore_listener,
        args=(loop, application.bot),
        daemon=True
    )
    listener_thread.start()

    log.info("Бот запущен (polling)")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
