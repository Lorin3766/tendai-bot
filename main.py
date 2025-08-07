import json
import logging
from dotenv import load_dotenv
from langdetect import detect
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from openai import OpenAI
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Загрузка переменных среды
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# Подключение к Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client_sheet = gspread.authorize(credentials)
sheet = client_sheet.open("TendAI Feedback").worksheet("Feedback")

def add_feedback(user_id, feedback_text):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([timestamp, str(user_id), feedback_text])

# Логгирование
logging.basicConfig(level=logging.INFO)

# Память и счётчики
user_memory = {}
message_counter = {}

# Быстрые шаблоны
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

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет, я TendAI 🤗 Что тебя беспокоит и волнует? Я подскажу, что делать.")

# Кнопки фидбека
def feedback_buttons():
    return InlineKeyboardMarkup([[InlineKeyboardButton("👍 Да", callback_data="feedback_yes"),
                                  InlineKeyboardButton("👎 Нет", callback_data="feedback_no")]])

# Обработка фидбека
async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    feedback = query.data

    try:
        add_feedback(user_id, feedback)
    except Exception as e:
        logging.error(f"Ошибка при сохранении отзыва: {e}")

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Спасибо за отзыв 🙏")

# Обработка сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text.strip()
    user_lower = user_message.lower()
    message_counter[user_id] = message_counter.get(user_id, 0) + 1
    lang = detect(user_message)

    if "#60сек" in user_lower or "/fast" in user_lower:
        for keyword, reply in quick_mode_symptoms.items():
            if keyword in user_lower:
                await update.message.reply_text(reply, reply_markup=feedback_buttons())
                return
        await update.message.reply_text("❗ Укажи симптом, например: «#60сек голова» или «/fast stomach».", reply_markup=feedback_buttons())
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

    await update.message.reply_text(bot_reply, reply_markup=feedback_buttons())

# Запуск
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(feedback_callback))
    app.run_polling()
