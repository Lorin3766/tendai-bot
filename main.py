import os
import json
import time
import logging
from typing import Optional

from dotenv import load_dotenv
from langdetect import detect
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from openai import OpenAI
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# =========================
# Настройки / Константы
# =========================
MAX_COMMENT_LEN = 600           # максимум символов в текстовом отзыве
COMMENT_COOLDOWN_SEC = 20       # не чаще одного текстового отзыва от пользователя
SHEET_NAME = "TendAI Feedback"
WORKSHEET_NAME = "Feedback"

# =========================
# Загрузка переменных среды
# =========================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан в .env")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не задан в .env")
if not GOOGLE_CREDENTIALS_JSON:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON не задан в .env")

client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# Подключение к Google Sheets
# Ожидаемые колонки в листе Feedback (строка 1):
# timestamp | user_id | name | username | rating | comment
# =========================
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client_sheet = gspread.authorize(credentials)
sheet = client_sheet.open(SHEET_NAME).worksheet(WORKSHEET_NAME)

def append_row_safe(values: list, retries: int = 2) -> None:
    """Безопасная запись строки в Google Sheets с короткими повторами при временных сбоях."""
    for i in range(retries + 1):
        try:
            sheet.append_row(values)
            return
        except Exception as e:
            logging.error(f"append_row попытка {i+1} ошибка: {e}")
            if i < retries:
                time.sleep(0.8)
            else:
                logging.error("Не удалось записать строку в Google Sheets после повторов.")

def save_feedback_to_sheet(user, rating: Optional[str|int], comment: Optional[str]) -> None:
    """Сохраняет строку отзыва в Google Sheets."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    uid = user.id
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    uname = f"@{user.username}" if user.username else ""
    append_row_safe([ts, str(uid), name, uname, str(rating or ""), comment or ""])

# ==============
# Логирование
# ==============
logging.basicConfig(level=logging.INFO)

# ==================
# Память и счётчики
# ==================
user_memory: dict[int, str] = {}
message_counter: dict[int, int] = {}
last_comment_at: dict[int, float] = {}  # анти-спам по текстовым отзывам

# =======================
# Быстрые шаблоны (60 сек)
# =======================
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

# =======================
# Кнопки фидбека (звёзды)
# =======================
def rating_keyboard() -> InlineKeyboardMarkup:
    stars = [InlineKeyboardButton(f"{i}⭐", callback_data=f"rate_{i}") for i in range(1, 6)]
    row1 = stars
    row2 = [InlineKeyboardButton("📝 Оставить комментарий", callback_data="comment")]
    return InlineKeyboardMarkup([row1, row2])

# ============
# Команда /start
# ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет, я TendAI 🤗 Что тебя беспокоит и волнует? Я подскажу, что делать."
    )

# ===========================
# Обработка callback: ЗВЁЗДЫ
# ===========================
async def handle_rate_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        rating = int(q.data.split("_")[1])
    except Exception:
        rating = None

    context.user_data["last_rating"] = rating
    try:
        save_feedback_to_sheet(user=update.effective_user, rating=rating, comment="")
    except Exception as e:
        logging.error(f"Ошибка сохранения рейтинга: {e}")

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await q.message.reply_text(f"Спасибо! Оценка сохранена: {rating}⭐")

# ======================================
# Обработка callback: "Оставить комментарий"
# ======================================
async def handle_comment_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id

    now = time.time()
    if uid in last_comment_at and now - last_comment_at[uid] < COMMENT_COOLDOWN_SEC:
        wait = int(COMMENT_COOLDOWN_SEC - (now - last_comment_at[uid]))
        await q.message.reply_text(f"Подождите {wait} сек. перед новым комментарием 🙏")
        return

    context.user_data["awaiting_comment"] = True
    await q.message.reply_text(
        "Напишите короткий отзыв (1–2 предложения).",
        reply_markup=ForceReply(selective=True)
    )

# =========================================
# Приём ТЕКСТОВОГО комментария от пользователя
# =========================================
async def receive_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_comment"):
        return  # это обычное сообщение, не отзыв

    uid = update.effective_user.id
    now = time.time()
    if uid in last_comment_at and now - last_comment_at[uid] < COMMENT_COOLDOWN_SEC:
        wait = int(COMMENT_COOLDOWN_SEC - (now - last_comment_at[uid]))
        await update.message.reply_text(f"Подождите {wait} сек. перед новым комментарием 🙏")
        context.user_data["awaiting_comment"] = False
        return

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Отзыв пустой. Напишите пару фраз, пожалуйста.")
        return
    if len(text) > MAX_COMMENT_LEN:
        await update.message.reply_text(f"Слишком длинно. Укоротите до ~{MAX_COMMENT_LEN} символов 🙏")
        return

    rating = context.user_data.get("last_rating", "")
    try:
        save_feedback_to_sheet(user=update.effective_user, rating=rating, comment=text)
    except Exception as e:
        logging.error(f"Ошибка сохранения комментария: {e}")

    last_comment_at[uid] = now
    context.user_data["awaiting_comment"] = False
    await update.message.reply_text("Спасибо за отзыв! Он сохранён 🙏")

# =====================================================
# (Необязательно) Обработка старых кнопок 👍/👎 для легаси
# =====================================================
async def legacy_thumb_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    value = "yes" if q.data.endswith("yes") else "no"
    try:
        save_feedback_to_sheet(user=update.effective_user, rating=value, comment="")
    except Exception as e:
        logging.error(f"Ошибка сохранения legacy-отзыва: {e}")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await q.message.reply_text("Спасибо за отзыв 🙏")

# ===================
# Основная логика ответа
# ===================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text.strip()
    user_lower = user_message.lower()
    message_counter[user_id] = message_counter.get(user_id, 0) + 1

    try:
        lang = detect(user_message)
    except Exception:
        lang = "unknown"

    # Быстрый режим
    if "#60сек" in user_lower or "/fast" in user_lower:
        for keyword, reply in quick_mode_symptoms.items():
            if keyword in user_lower:
                await update.message.reply_text(reply, reply_markup=rating_keyboard())
                return
        await update.message.reply_text(
            "❗ Укажи симптом, например: «#60сек голова» или «/fast stomach».",
            reply_markup=rating_keyboard()
        )
        return

    # Уточняющие вопросы
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

    await update.message.reply_text(bot_reply, reply_markup=rating_keyboard())

# =====
# Запуск
# =====
async def _post_init(app):
    # Сброс вебхука перед polling и очистка старой очереди
    await app.bot.delete_webhook(drop_pending_updates=True)

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(_post_init).build()

    # Команды
    app.add_handler(CommandHandler("start", start))

    # Callback-и
    app.add_handler(CallbackQueryHandler(handle_rate_cb, pattern=r"^rate_[1-5]$"))
    app.add_handler(CallbackQueryHandler(handle_comment_cb, pattern=r"^comment$"))
    app.add_handler(CallbackQueryHandler(legacy_thumb_cb, pattern=r"^feedback_(yes|no)$"))

    # Текст: делаем обработчик комментариев НЕблокирующим
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_comment, block=False))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
