import os, json, logging
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# Опционально: OpenAI и Google Sheets подключаем мягко
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
except Exception:
    gspread = None
    ServiceAccountCredentials = None

# ---------- Настройка ----------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN отсутствует")

client = OpenAI(api_key=OPENAI_API_KEY) if (OPENAI_API_KEY and OpenAI) else None

# ---------- Google Sheets ----------
sheet = None
if gspread and ServiceAccountCredentials:
    creds_env = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_env:
        try:
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds_dict = json.loads(creds_env)
            credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            gclient = gspread.authorize(credentials)
            sheet = gclient.open("TendAI Feedback").worksheet("Feedback")
            logging.info("Google Sheets подключены")
        except Exception as e:
            logging.exception(f"Sheets error: {e}")
    else:
        logging.info("GOOGLE_CREDENTIALS_JSON не задан — отзывы писать не будем")

def add_feedback_row(user, name, rating, comment):
    """Пишем полную строку отзыва. Ожидаемые заголовки: timestamp, user_id, name, username, rating, comment"""
    if not sheet:
        return
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username = (user.username or "")
        sheet.append_row([ts, str(user.id), name, username, rating, comment or ""])
    except Exception:
        logging.exception("Не удалось записать отзыв в Sheets")

# ---------- Память/шаблоны ----------
user_memory = {}
pending_feedback = {}  # user_id -> {"name": "feedback_yes|feedback_no", "rating": 1|0}

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

def feedback_buttons():
    return InlineKeyboardMarkup([[InlineKeyboardButton("👍 Да", callback_data="feedback_yes"),
                                  InlineKeyboardButton("👎 Нет", callback_data="feedback_no")]])

# ---------- Команды ----------
async def on_startup(app):
    me = await app.bot.get_me()
    logging.info(f"Running as @{me.username}")
    await app.bot.delete_webhook(drop_pending_updates=True)
    logging.info("Webhook cleared")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Как я могу помочь тебе сегодня? Есть какие-то вопросы о здоровье?",
        reply_markup=feedback_buttons()
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def skip_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in pending_feedback:
        data = pending_feedback.pop(uid)
        add_feedback_row(update.effective_user, data["name"], data["rating"], "")
        await update.message.reply_text("Окей, без комментария. Спасибо! 🙏")
    else:
        await update.message.reply_text("Сейчас нечего пропускать 🙂")

# ---------- Обработка фидбека ----------
async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        await q.answer()
        user = q.from_user
        choice = q.data  # "feedback_yes" | "feedback_no"
        rating = 1 if choice == "feedback_yes" else 0

        # Запросим необязательный комментарий и запомним выбор
        pending_feedback[user.id] = {"name": choice, "rating": rating}

        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        await q.message.reply_text(
            "Спасибо за оценку 🙏\n"
            "Хочешь добавить комментарий? Просто напиши сообщение в ответ.\n"
            "Или отправь /skip, чтобы пропустить."
        )
    except Exception:
        logging.exception("feedback_callback error")

# ---------- Основной обработчик ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        text = (update.message.text or "").strip()
        low = text.lower()
        logging.info(f"Message from {user_id}: {text!r}")

        # Если ждём комментарий к отзыву и пришёл не-команда текст — сохраняем
        if user_id in pending_feedback and not text.startswith("/"):
            data = pending_feedback.pop(user_id)
            add_feedback_row(update.effective_user, data["name"], data["rating"], text)
            await update.message.reply_text("Комментарий сохранён, спасибо! 🙌")
            return

        # Быстрый режим
        if "#60сек" in low or "/fast" in low:
            for k, reply in quick_mode_symptoms.items():
                if k in low:
                    await update.message.reply_text(reply, reply_markup=feedback_buttons())
                    return
            await update.message.reply_text("❗ Укажи симптом: «#60сек голова» или «/fast stomach».", reply_markup=feedback_buttons())
            return

        # Простые мини-диалоги
        if "голова" in low or "headache" in low:
            await update.message.reply_text(
                "Где именно болит: лоб, затылок, виски?\n"
                "Какой характер: тупая, острая, пульсирующая?\n"
                "Есть ли тошнота/светобоязнь?"
            )
            user_memory[user_id] = "головная боль"
            return

        if "горло" in low or "throat" in low:
            await update.message.reply_text(
                "Болит при глотании или постоянно?\n"
                "Есть температура/кашель?\n"
                "Когда началось?"
            )
            user_memory[user_id] = "боль в горле"
            return

        if "кашель" in low or "cough" in low:
            await update.message.reply_text(
                "Кашель сухой или с мокротой?\n"
                "Давно?\n"
                "Есть температура/боль в груди/одышка?"
            )
            user_memory[user_id] = "кашель"
            return

        memory_text = f"(Ранее упоминал: {user_memory[user_id]})\n" if user_id in user_memory else ""

        # Ответ ИИ (если ключ задан), иначе дефолт
        reply_text = "Мне нужно чуть больше деталей: что именно беспокоит?"
        if client:
            try:
                system_prompt = (
                    "Ты — заботливый помощник по здоровью по имени TendAI. "
                    "Отвечай на языке пользователя. Если есть симптом — 1–2 уточняющих вопроса, "
                    "2–3 возможные причины, что сделать дома, и когда к врачу."
                )
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": text}
                    ],
                    temperature=0.6,
                    max_tokens=400
                )
                reply_text = memory_text + (resp.choices[0].message.content or "").strip()
            except Exception as e:
                logging.exception("OpenAI error")
                reply_text = f"Сервис ИИ временно недоступен: {e}"

        await update.message.reply_text(reply_text, reply_markup=feedback_buttons())

    except Exception:
        logging.exception("handle_message fatal error")

# ---------- Запуск ----------
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("skip", skip_comment))
    app.add_handler(CallbackQueryHandler(feedback_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)
