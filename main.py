import os
import json
import logging
import time
from typing import Optional

from dotenv import load_dotenv
from langdetect import detect, LangDetectException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from openai import OpenAI
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ── ENV ───────────────────────────────────────────────────────────────────
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# ── Google Sheets ─────────────────────────────────────────────────────────
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client_sheet = gspread.authorize(credentials)
sheet = client_sheet.open("TendAI Feedback").worksheet("Feedback")

# СТАРОЕ (для 👍/👎) — как было
def add_feedback(user_id, feedback_text):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([timestamp, str(user_id), feedback_text])

# НОВОЕ (звёзды/комментарий): timestamp | user_id | name | username | rating | comment
def add_detailed_feedback(user, rating: Optional[int|str], comment: Optional[str]):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    username = f"@{user.username}" if user.username else ""
    sheet.append_row([timestamp, str(user.id), name, username, str(rating or ""), comment or ""])

# ── Логи/память ───────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
user_memory = {}
message_counter = {}
last_comment_at = {}  # анти-спам на текстовые отзывы

# ── Быстрые шаблоны ───────────────────────────────────────────────────────
quick_mode_symptoms = {
    "голова": """[Здоровье за 60 секунд]
💡 Возможные причины: стресс, обезвоживание, недосып  
🪪 Что делать: выпей воды, отдохни, проветри комнату  
🚨 Когда к врачу: если боль внезапная, сильная, с тошнотой или нарушением зрения""",
    "head": """[Quick Health Check]
💡 Possible causes: stress, dehydration, fatigue  
🪪 Try: rest, hydration, fresh air  
🚨 See a doctor if pain is sudden, severe, or with nausea/vision issues""",
    "живот": """[Здоровье за 60 секунд]
💡 Возможные причины: гастрит, питание, стресс  
🪪 Что делать: тёплая вода, покой, исключи еду на 2 часа  
🚨 Когда к врачу: если боль резкая, с температурой, рвотой или длится >1 дня""",
    "stomach": """[Quick Health Check]
💡 Possible causes: gastritis, poor diet, stress  
🪪 Try: warm water, rest, skip food for 2 hours  
🚨 See a doctor if pain is sharp, with fever or vomiting""",
    "слабость": """[Здоровье за 60 секунд]
💡 Возможные причины: усталость, вирус, анемия  
🪪 Что делать: отдых, поешь, выпей воды  
🚨 Когда к врачу: если слабость длится >2 дней или нарастает""",
    "weakness": """[Quick Health Check]
💡 Possible causes: fatigue, virus, low iron  
🪪 Try: rest, eat, hydrate  
🚨 Doctor: if weakness lasts >2 days or gets worse"""
}

# ── Команда /start ────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет, я TendAI 🤗 Что тебя беспокоит и волнует? Я подскажу, что делать.")

# ── Кнопки (звёзды + комментарий + твои 👍/👎) ────────────────────────────
def combined_feedback_buttons():
    stars = [InlineKeyboardButton(f"{i}⭐", callback_data=f"rate_{i}") for i in range(1, 6)]
    row1 = stars
    row2 = [InlineKeyboardButton("📝 Оставить комментарий", callback_data="comment")]
    row3 = [InlineKeyboardButton("👍 Да", callback_data="feedback_yes"),
            InlineKeyboardButton("👎 Нет", callback_data="feedback_no")]
    return InlineKeyboardMarkup([row1, row2, row3])

# ── СТАРЫЕ 👍/👎 — как было ───────────────────────────────────────────────
async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        add_feedback(q.from_user.id, q.data)
    except Exception as e:
        logging.error(f"Ошибка при сохранении отзыва: {e}")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await q.message.reply_text("Спасибо за отзыв 🙏")

# ── Звёзды 1–5 ────────────────────────────────────────────────────────────
async def handle_rate_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        rating = int(q.data.split("_")[1])
    except Exception:
        rating = None
    context.user_data["last_rating"] = rating
    try:
        add_detailed_feedback(update.effective_user, rating=rating, comment="")
    except Exception as e:
        logging.error(f"Ошибка сохранения рейтинга: {e}")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await q.message.reply_text(f"Спасибо! Оценка сохранена: {rating}⭐")

# ── Запрос текстового комментария ─────────────────────────────────────────
async def handle_comment_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    now = time.time()
    if uid in last_comment_at and now - last_comment_at[uid] < 20:
        wait = int(20 - (now - last_comment_at[uid]))
        await q.message.reply_text(f"Подождите {wait} сек. перед новым комментарием 🙏")
        return
    context.user_data["awaiting_comment"] = True
    await q.message.reply_text(
        "Напишите короткий отзыв (1–2 предложения).",
        reply_markup=ForceReply(selective=True)
    )

# ── Приём комментария (только в ответ на ForceReply) ──────────────────────
async def receive_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_comment"):
        return
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Отзыв пустой. Напишите пару фраз, пожалуйста.")
        return
    if len(text) > 600:
        await update.message.reply_text("Слишком длинно. Укоротите до ~600 символов 🙏")
        return
    rating = context.user_data.get("last_rating", "")
    try:
        add_detailed_feedback(update.effective_user, rating=rating, comment=text)
    except Exception as e:
        logging.error(f"Ошибка сохранения комментария: {e}")
    last_comment_at[uid] = time.time()
    context.user_data["awaiting_comment"] = False
    await update.message.reply_text("Спасибо за отзыв! Он сохранён 🙏")

# ── Основная логика сообщений (твоя) ──────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text.strip()
    user_lower = user_message.lower()
    message_counter[user_id] = message_counter.get(user_id, 0) + 1

    try:
        lang = detect(user_message)
    except LangDetectException:
        lang = "unknown"

    if "#60сек" in user_lower or "/fast" in user_lower:
        for keyword, reply in quick_mode_symptoms.items():
            if keyword in user_lower:
                await update.message.reply_text(reply, reply_markup=combined_feedback_buttons())
                return
        await update.message.reply_text("❗ Укажи симптом, например: «#60сек голова» или «/fast stomach».",
                                        reply_markup=combined_feedback_buttons())
        return

    if "голова" in user_lower or "headache" in user_lower:
        await update.message.reply_text(
            "Где именно болит голова? Лоб, затылок, виски?\n"
            "Какой характер боли: тупая, острая, пульсирующая?\n"
            "Есть ли ещё симптомы — тошнота, светобоязнь?"
        )
        user_memory[user_id] = "головная боль"
        return

    if "горло" in user_lower or "throat" in user_lower:
        await update.message.reply_text(
            "Горло болит при глотании или постоянно?\n"
            "Есть ли температура или кашель?\n"
            "Когда началось?"
        )
        user_memory[user_id] = "боль в горле"
        return

    if "кашель" in user_lower or "cough" in user_lower:
        await update.message.reply_text(
            "Кашель сухой или с мокротой?\n"
            "Давно ли он у вас?\n"
            "Есть ли температура, боль в груди или одышка?"
        )
        user_memory[user_id] = "кашель"
        return

    memory_text = ""
    if user_id in user_memory:
        memory_text = f"(Ты ранее упоминал: {user_memory[user_id]})\n"

    system_prompt = (
        "Ты — заботливый и умный помощник по здоровью и долголетию по имени TendAI.\n"
        "Всегда отвечай на том языке, на котором говорит пользователь.\n"
        "Будь тёплым, но отвечай по сути, без повторов.\n"
        "Если упоминается симптом — задай 1–2 уточняющих вопроса, назови 2–3 возможные причины,\n"
        "предложи, что можно сделать дома, и в каких случаях идти к врачу.\n"
        "Если боли нет — не нагнетай, просто объясни спокойно.\n"
        "Если пользователь благодарит — ответь коротко и по-человечески.\n"
        "Говори ясно, коротко и с заботой."
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.7,
            max_tokens=400
        )
        bot_reply = memory_text + response.choices[0].message.content.strip()
    except Exception as e:
        bot_reply = f"Произошла ошибка при обращении к ИИ: {e}"
        logging.error(bot_reply)

    await update.message.reply_text(bot_reply, reply_markup=combined_feedback_buttons())

# ── Сброс вебхука перед polling (чтобы не было конфликтов) ────────────────
async def _post_init(app):
    await app.bot.delete_webhook(drop_pending_updates=True)

# ── Запуск ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", start))

    # callbacks: новые + старые
    app.add_handler(CallbackQueryHandler(handle_rate_cb, pattern=r"^rate_[1-5]$"))
    app.add_handler(CallbackQueryHandler(handle_comment_cb, pattern=r"^comment$"))
    app.add_handler(CallbackQueryHandler(feedback_callback, pattern=r"^feedback_(yes|no)$"))  # 👍/👎

    # Приём комментария — только на ответ (ForceReply), не мешает обычным сообщениям
    app.add_handler(MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND, receive_comment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling(drop_pending_updates=True)
