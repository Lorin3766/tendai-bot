import os
import time
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from openai import OpenAI

# Загрузка переменных среды
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# Логгирование
logging.basicConfig(level=logging.INFO)

# Память и счётчик сообщений
user_memory = {}
message_counter = {}

# Быстрые ответы по ключевым симптомам
quick_mode_symptoms = {
    "голова": """🕐 Здоровье за 60 секунд:
💡 Возможные причины: стресс, обезвоживание, недосып  
🩺 Что делать: выпей воды, отдохни, проветри комнату  
🚨 Когда к врачу: если боль внезапная, сильная, с тошнотой или нарушением зрения""",

    "живот": """🕐 Здоровье за 60 секунд:
💡 Возможные причины: гастрит, питание, стресс  
🩺 Что делать: тёплая вода, покой, исключи еду на 2 часа  
🚨 Когда к врачу: если боль резкая, с температурой, рвотой или длится >1 дня""",

    "слабость": """🕐 Здоровье за 60 секунд:
💡 Возможные причины: усталость, вирус, анемия  
🩺 Что делать: отдых, поешь, выпей воды  
🚨 Когда к врачу: если слабость длится >2 дней или нарастает"""
}

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет, я TendAI 🤗 Что тебя беспокоит и волнует? Я подскажу, что делать.")

# Кнопки фидбека
def feedback_buttons():
    buttons = [
        [
            InlineKeyboardButton("👍 Да", callback_data="feedback_yes"),
            InlineKeyboardButton("👎 Нет", callback_data="feedback_no")
        ]
    ]
    return InlineKeyboardMarkup(buttons)

# Обработка отзывов
async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    feedback = query.data
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open("feedback_log.txt", "a", encoding="utf-8") as f:
        f.write(f"{timestamp} | user_id={user_id} | feedback={feedback}\n")

    logging.info(f"[ОТЗЫВ] {timestamp} | user_id={user_id} | feedback={feedback}")
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Спасибо за отзыв 🙏")

# Основная логика
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text.strip().lower()
    message_counter[user_id] = message_counter.get(user_id, 0) + 1

    # Здоровье за 60 секунд
    if "#60сек" in user_message or "/fast" in user_message:
        for keyword, answer in quick_mode_symptoms.items():
            if keyword in user_message:
                await update.message.reply_text(answer, reply_markup=feedback_buttons())
                return
        await update.message.reply_text(
            "Укажи симптом, например: «#60сек голова» или «/fast живот».", 
            reply_markup=feedback_buttons()
        )
        return

    # Стандартные уточнения
    if "голова" in user_message:
        await update.message.reply_text(
            "Где именно болит голова? Лоб, затылок, виски?\n"
            "Какой характер боли: тупая, острая, пульсирующая?\n"
            "Есть ли ещё симптомы — тошнота, светобоязнь?"
        )
        user_memory[user_id] = "головная боль"
        return
    elif "горло" in user_message:
        await update.message.reply_text(
            "Горло болит при глотании или постоянно?\n"
            "Есть ли температура или кашель?\n"
            "Когда началось?"
        )
        user_memory[user_id] = "боль в горле"
        return
    elif "кашель" in user_message:
        await update.message.reply_text(
            "Кашель сухой или с мокротой?\n"
            "Давно ли он у вас?\n"
            "Есть ли температура, боль в груди или одышка?"
        )
        user_memory[user_id] = "кашель"
        return

    # Память
    memory_text = ""
    if user_id in user_memory:
        memory_text = f"(Ты ранее упоминал: {user_memory[user_id]})\n"

    # Системный промпт
    system_prompt = (
        "Ты — заботливый и умный помощник по здоровью. "
        "Отвечай коротко, понятно и по-человечески. "
        "Если человек жалуется на симптом, задай 1-2 уточняющих вопроса, укажи возможные причины (3–5 слов), "
        "что можно сделать дома и когда стоит обратиться к врачу. "
        "Избегай длинных вводных. Пиши как добрый, заботливый человек, но по делу. "
        "Ты — бот TendAI, не врач, но хорошо разбираешься в здоровье и долголетии."
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

    await update.message.reply_text(bot_reply, reply_markup=feedback_buttons())

# Запуск
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(feedback_callback))

    print("TendAI запущен!")
    while True:
        try:
            app.run_polling()
        except Exception as e:
            logging.error(f"Произошла ошибка в боте: {e}")
            time.sleep(5)
