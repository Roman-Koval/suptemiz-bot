"""
SupTemiz Telegram Bot — Railway deployment
- Уведомления администратору о новых заказах
- Смена статусов с кнопками
- Deep link: клиент нажимает Start → бот сохраняет chat_id → уведомления автоматически
- WhatsApp: кнопка-ссылка с готовым текстом
"""

import os
import logging
import asyncio
import threading
import json
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.error import TelegramError
import firebase_admin
from firebase_admin import credentials, firestore

# =========================
# Конфиг
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

STATUS_CLIENT_MSG = {
    "confirmed": (
        "✅ Ваш заказ подтверждён!\n\n"
        "Мы приедем в назначенное время.\n"
        "Если возникнут вопросы — напишите нам.\n\n"
        "С уважением, SupTemiz"
    ),
    "in_progress": (
        "🔄 Уборка началась!\n\n"
        "Наши специалисты уже работают.\n"
        "Мы сообщим, когда всё будет готово.\n\n"
        "С уважением, SupTemiz"
    ),
    "done": (
        "✔️ Уборка завершена!\n\n"
        "Надеемся, вам всё понравилось!\n"
        "Будем рады видеть вас снова. 😊\n\n"
        "С уважением, SupTemiz"
    ),
    "cancelled": (
        "❌ Ваш заказ отменён.\n\n"
        "Если это ошибка или хотите перенести — напишите нам.\n\n"
        "С уважением, SupTemiz"
    ),
}

TYPE_LABELS = {
    "standard": "Стандартная",
    "deep":     "Генеральная",
    "office":   "Офис",
}

def status_label(s):
    return STATUS_LABELS.get(s, s or "—")

def format_order(order):
    has_tg = bool(order.get("clientChatId"))
    tg_icon = "✈️" if has_tg else "📵"
    tg_line = f"\n{tg_icon} <b>Telegram:</b> {'подписан' if has_tg else 'не подписан'}"
    return (
        f"🧽 <b>Заказ {order.get('id','—')}</b>\n\n"
        f"👤 <b>Имя:</b> {order.get('name','—')}\n"
        f"📱 <b>Телефон:</b> {order.get('phone','—')}"
        f"{tg_line}\n"
        f"📍 <b>Район:</b> {order.get('area','—')}\n"
        f"🏠 <b>Тип:</b> {TYPE_LABELS.get(order.get('type',''),'—')}\n"
        f"📐 <b>Площадь:</b> {order.get('areaSize','—')} м²\n"
        f"💰 <b>Цена:</b> {order.get('price','—')} ₺\n"
        f"📅 <b>Дата:</b> {order.get('date','—')} {order.get('time','')}\n"
        f"💬 <b>Коммент:</b> {order.get('comment') or '—'}\n"
        f"🔄 <b>Статус:</b> {status_label(order.get('status'))}"
    )

def clean_phone(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit() or c == "+")

def wa_link(phone: str, text: str) -> str:
    p = clean_phone(phone).lstrip("+")
    return f"https://wa.me/{p}?text={quote(text)}"

def make_keyboard(doc_id: str, current_status: str, order: dict) -> InlineKeyboardMarkup:
    status_buttons = [
        ("✅ Подтвердить",  "confirmed"),
        ("▶️ В процессе",   "in_progress"),
        ("✔️ Завершить",    "done"),
        ("❌ Отменить",     "cancelled"),
    ]

    kb = [
        [InlineKeyboardButton(label, callback_data=f"status:{doc_id}:{st}")]
        for label, st in status_buttons if st != current_status
    ]

    client_msg = STATUS_CLIENT_MSG.get(current_status, "")
    order_info = f"Заказ {order.get('id','')}: {order.get('date','')} {order.get('time','')}\n\n"
    full_msg   = order_info + client_msg

    # WhatsApp кнопка — всегда если есть телефон и есть текст для статуса
    phone = order.get("phone", "")
    if phone and client_msg:
        kb.append([InlineKeyboardButton(
            "📲 Уведомить в WhatsApp",
            url=wa_link(phone, full_msg)
        )])

    # Telegram кнопка — только если клиент подписался (есть clientChatId)
    client_chat_id = order.get("clientChatId")
    if client_chat_id and client_msg:
        kb.append([InlineKeyboardButton(
            "✈️ Уведомить в Telegram",
            callback_data=f"notify_tg:{doc_id}:{current_status}"
        )])

    kb.append([InlineKeyboardButton("📋 Список заказов", callback_data="cmd:list")])
    return InlineKeyboardMarkup(kb)

def is_admin(update: Update) -> bool:
    return (update.effective_chat.id if update.effective_chat else None) == ADMIN_CHAT_ID

# =========================
# /start — обработка deep link
# =========================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args    = ctx.args  # параметры после /start

    # Deep link: /start order_ST-XXXXXX
    if args and args[0].startswith("order_"):
        order_id = args[0][6:]  # убираем "order_"
        log.info(f"Deep link: chat_id={chat_id}, order_id={order_id}")

        # Ищем заказ по полю id (не по doc id)
        docs = list(
            db.collection("orders")
            .where("id", "==", order_id)
            .limit(1)
            .stream()
        )

        if docs:
            doc_ref = docs[0].reference
            doc_ref.update({"clientChatId": chat_id})
            log.info(f"Сохранён clientChatId={chat_id} для заказа {order_id}")
            await update.message.reply_text(
                f"✅ Отлично! Теперь вы будете получать уведомления о статусе заказа "
                f"<b>{order_id}</b> прямо сюда.\n\n"
                f"SupTemiz",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"⚠️ Заказ <b>{order_id}</b> не найден.\n"
                f"Попробуйте оформить заказ снова на сайте.",
                parse_mode="HTML"
            )
        return

    # Обычный /start — показываем chat_id если это admin
    await update.message.reply_text(
        f"👋 <b>SupTemiz Bot</b>\n\n"
        f"Здесь вы будете получать уведомления о статусе вашего заказа.\n\n"
        f"{'🔑 Ваш chat_id: <code>' + str(chat_id) + '</code>' if chat_id == ADMIN_CHAT_ID else ''}",
        parse_mode="HTML"
    )

# =========================
# Команды администратора
# =========================
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
            reply_markup=make_keyboard(d.id, order.get("status", "pending"), order)
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
            reply_markup=make_keyboard(d.id, "pending", order)
        )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔️ Нет доступа")
        return
    docs  = list(db.collection("orders").stream())
    counts  = {k: 0 for k in STATUS_LABELS}
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
        f"Всего: <b>{len(docs)}</b>\n"
        f"⏳ В ожидании: <b>{counts['pending']}</b>\n"
        f"✅ Подтверждено: <b>{counts['confirmed']}</b>\n"
        f"🔄 В процессе: <b>{counts['in_progress']}</b>\n"
        f"✔️ Завершено: <b>{counts['done']}</b>\n"
        f"❌ Отменено: <b>{counts['cancelled']}</b>\n\n"
        f"💰 Выручка: <b>{int(revenue):,} ₺</b>",
        parse_mode="HTML"
    )

# =========================
# Inline-кнопки
# =========================
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(update):
        await query.answer("⛔️ Нет доступа", show_alert=True)
        return

    data = query.data

    # Смена статуса
    if data.startswith("status:"):
        _, doc_id, new_status = data.split(":", 2)
        try:
            ref   = db.collection("orders").document(doc_id)
            ref.update({"status": new_status})
            order = ref.get().to_dict()

            # Автоматически уведомляем клиента в Telegram если подписан
            client_chat_id = order.get("clientChatId")
            client_msg     = STATUS_CLIENT_MSG.get(new_status, "")
            if client_chat_id and client_msg:
                order_info = f"Заказ {order.get('id','')}: {order.get('date','')} {order.get('time','')}\n\n"
                try:
                    await ctx.bot.send_message(
                        chat_id=client_chat_id,
                        text=order_info + client_msg
                    )
                    log.info(f"Клиент {client_chat_id} автоматически уведомлён: {new_status}")
                except TelegramError as e:
                    log.error(f"Ошибка авто-уведомления клиента: {e}")

            await query.edit_message_text(
                format_order(order),
                parse_mode="HTML",
                reply_markup=make_keyboard(doc_id, new_status, order)
            )
        except Exception as e:
            log.error(f"Status update error: {e}")
            await query.answer(f"Ошибка: {e}", show_alert=True)

    # Ручное уведомление клиента в Telegram
    elif data.startswith("notify_tg:"):
        _, doc_id, status = data.split(":", 2)
        try:
            order          = db.collection("orders").document(doc_id).get().to_dict()
            client_chat_id = order.get("clientChatId")
            client_msg     = STATUS_CLIENT_MSG.get(status, "")
            if not client_chat_id or not client_msg:
                await query.answer("Нет данных для отправки", show_alert=True)
                return
            order_info = f"Заказ {order.get('id','')}: {order.get('date','')} {order.get('time','')}\n\n"
            await ctx.bot.send_message(
                chat_id=client_chat_id,
                text=order_info + client_msg
            )
            await query.answer("✅ Уведомление отправлено!", show_alert=True)
        except TelegramError as e:
            await query.answer(f"❌ Ошибка: {e}", show_alert=True)

    elif data == "cmd:list":
        await cmd_list(update, ctx)

# =========================
# Firestore listener
# =========================
def start_firestore_listener(loop, bot):
    seen_ids = set()
    for d in db.collection("orders").stream():
        seen_ids.add(d.id)
    log.info(f"Listener: {len(seen_ids)} заказов загружено")

    def on_snapshot(col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name == "ADDED" and change.document.id not in seen_ids:
                seen_ids.add(change.document.id)
                order  = change.document.to_dict()
                doc_id = change.document.id
                log.info(f"Новый заказ: {doc_id}")

                text = "🔔 <b>НОВЫЙ ЗАКАЗ!</b>\n\n" + format_order(order)
                kb   = make_keyboard(doc_id, "pending", order)

                async def send(t=text, k=kb):
                    try:
                        await bot.send_message(
                            chat_id=ADMIN_CHAT_ID,
                            text=t,
                            parse_mode="HTML",
                            reply_markup=k
                        )
                    except Exception as e:
                        log.error(f"Ошибка отправки: {e}")

                asyncio.run_coroutine_threadsafe(send(), loop)

    db.collection("orders").on_snapshot(on_snapshot)
    log.info("Firestore listener запущен")
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

    loop = asyncio.get_event_loop()
    threading.Thread(
        target=start_firestore_listener,
        args=(loop, application.bot),
        daemon=True
    ).start()

    log.info("Бот запущен (polling)")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
