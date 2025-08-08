# main.py
import os
import json
import logging
import re
from datetime import datetime

from dotenv import load_dotenv
from langdetect import detect
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters, CallbackQueryHandler
)

# ====== OpenAI (минимально, только фолбэк) ======
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False

# ====== Google Sheets ======
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ------------------------------------------------
# ENV
# ------------------------------------------------
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# OpenAI client (опционально)
client_oa = None
if OPENAI_API_KEY and OPENAI_AVAILABLE:
    try:
        client_oa = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        client_oa = None

# Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client_sheet = gspread.authorize(credentials)
sheet_feedback = client_sheet.open("TendAI Feedback").worksheet("Feedback")

def add_feedback_row(row):
    try:
        sheet_feedback.append_row(row)
    except Exception as e:
        logging.error(f"Sheets append error: {e}")

# ------------------------------------------------
# LOGGING
# ------------------------------------------------
logging.basicConfig(level=logging.INFO)

# ------------------------------------------------
# STATE
# ------------------------------------------------
# user_state[user_id] = {
#   "lang": "ru/en/uk/es",
#   "slots": {"loc": None, "kind": None, "duration": None},
#   "awaiting": None  # "loc" | "kind" | "duration" | None
# }
user_state = {}

SUPPORTED = {"ru", "en", "uk", "es"}

def norm_lang(code: str) -> str:
    if not code:
        return "en"
    code = code.lower()
    if code.startswith("ru"): return "ru"
    if code.startswith("uk") or code.startswith("ua"): return "uk"
    if code.startswith("en"): return "en"
    if code.startswith("es"): return "es"
    return "en"

# ------------------------------------------------
# L10N
# ------------------------------------------------
T = {
    "ru": {
        "hello": "Привет! Как я могу помочь тебе сегодня? Есть какие-то вопросы о здоровье?",
        "where": "Мне нужно немного больше информации, чтобы помочь. Где именно у вас болит?",
        "kind": "Какой характер боли: тупая, острая, пульсирующая или жгучая?",
        "duration": "Как давно это продолжается?",
        "thanks": "Спасибо за вопрос. Можете уточнить, где именно у вас болит? И как долго продолжается эта боль?",
        "bad_llm": "Прошу прощения, сейчас не могу использовать ИИ. Давайте разберёмся вручную.",
        "quick_menu": ["Голова", "Горло", "Спина", "Живот", "Другое"],
        "kinds_menu": ["Тупая", "Острая", "Пульсирующая", "Жгучая", "Пропустить"],
        "ask_more_kind": "Понял. Давайте уточним характер боли.",
        "ask_more_duration": "Спасибо. Сколько это длится (минуты/часы/дни)?",
        "tips_head": "💡 Возможные причины: стресс, обезвоживание, недосып\n🪪 Что делать: вода, отдых, свежий воздух\n🚨 Врач: если боль внезапная/очень сильная, с тошнотой/нарушением зрения",
        "tips_throat": "💡 Возможные причины: вирус, раздражение, сухость\n🪪 Что делать: тёплое питьё, покой, увлажнение\n🚨 Врач: высокая температура, сильная боль, затруднение дыхания",
        "tips_back": "💡 Возможные причины: мышечное перенапряжение, поза, нагрузка\n🪪 Что делать: мягкое тепло, лёгкая растяжка, отдых\n🚨 Врач: если боль отдает в ноги, онемение, слабость",
        "tips_stomach": "💡 Возможные причины: гастрит, питание, стресс\n🪪 Что делать: тёплая вода, покой, без еды 2 часа\n🚨 Врач: резкая боль, температура, рвота",
        "feedback_q": "Спасибо за оценку 🙏\nХочешь добавить комментарий? Просто напиши сообщение в ответ.\nИли отправь /skip, чтобы пропустить.",
        "fb_saved": "Комментарий сохранён, спасибо! 🙌",
        "lang_set": "Язык сохранён: Русский",
        "help_lang": "Используйте /lang ru|en|uk|es, чтобы сменить язык.",
        "buttons_fb": ["👍 Да", "👎 Нет"],
    },
    "en": {
        "hello": "Hi! How can I help you today? Any health questions?",
        "where": "I need a bit more information to help. Where exactly does it hurt?",
        "kind": "What kind of pain is it: dull, sharp, throbbing, or burning?",
        "duration": "How long has it lasted?",
        "thanks": "Thanks for your message. Where exactly does it hurt? And how long has it lasted?",
        "bad_llm": "Sorry, AI is unavailable right now. Let’s handle it manually.",
        "quick_menu": ["Head", "Throat", "Back", "Stomach", "Other"],
        "kinds_menu": ["Dull", "Sharp", "Throbbing", "Burning", "Skip"],
        "ask_more_kind": "Got it. Let’s clarify the pain type.",
        "ask_more_duration": "Thanks. How long (minutes/hours/days)?",
        "tips_head": "💡 Possible causes: stress, dehydration, poor sleep\n🪪 Try: water, rest, fresh air\n🚨 Doctor: sudden/severe pain, with nausea or vision issues",
        "tips_throat": "💡 Possible causes: virus, irritation, dryness\n🪪 Try: warm fluids, rest, humidify\n🚨 Doctor: high fever, severe pain, trouble breathing",
        "tips_back": "💡 Possible causes: muscle strain, posture, load\n🪪 Try: gentle heat, light stretching, rest\n🚨 Doctor: radiating pain, numbness, weakness",
        "tips_stomach": "💡 Possible causes: gastritis, diet, stress\n🪪 Try: warm water, rest, skip food for 2 hours\n🚨 Doctor: sharp pain, fever, vomiting",
        "feedback_q": "Thanks for the rating 🙏\nWant to add a comment? Just type it now.\nOr send /skip to pass.",
        "fb_saved": "Comment saved, thank you! 🙌",
        "lang_set": "Language set: English",
        "help_lang": "Use /lang ru|en|uk|es to change language.",
        "buttons_fb": ["👍 Yes", "👎 No"],
    },
    "uk": {
        "hello": "Привіт! Чим можу допомогти сьогодні? Є питання про здоров’я?",
        "where": "Потрібно трохи більше інформації. Де саме болить?",
        "kind": "Який характер болю: тупий, гострий, пульсівний чи пекучий?",
        "duration": "Як довго це триває?",
        "thanks": "Дякую за повідомлення. Де саме болить і як довго це триває?",
        "bad_llm": "Вибач, зараз ІІ недоступний. Розберімося вручну.",
        "quick_menu": ["Голова", "Горло", "Спина", "Живіт", "Інше"],
        "kinds_menu": ["Тупий", "Гострий", "Пульсівний", "Пекучий", "Пропустити"],
        "ask_more_kind": "Зрозуміло. Уточнимо характер болю.",
        "ask_more_duration": "Дякую. Скільки це триває (хвилини/години/дні)?",
        "tips_head": "💡 Можливі причини: стрес, зневоднення, недосип\n🪪 Варто: вода, відпочинок, свіже повітря\n🚨 Лікар: раптовий/сильний біль, нудота, проблеми із зором",
        "tips_throat": "💡 Можливі причини: вірус, подразнення, сухість\n🪪 Варто: теплі напої, спокій, зволоження\n🚨 Лікар: висока температура, сильний біль, утруднене дихання",
        "tips_back": "💡 Можливі причини: м’язове перенапруження, постава, навантаження\n🪪 Варто: легке тепло, розтяжка, відпочинок\n🚨 Лікар: іррадіація болю, оніміння, слабкість",
        "tips_stomach": "💡 Можливі причини: гастрит, харчування, стрес\n🪪 Варто: тепла вода, спокій, без їжі 2 години\n🚨 Лікар: різкий біль, температура, блювання",
        "feedback_q": "Дякую за оцінку 🙏\nХочеш додати коментар? Просто напиши його зараз.\nАбо /skip, щоб пропустити.",
        "fb_saved": "Коментар збережено, дякуємо! 🙌",
        "lang_set": "Мову змінено: Українська",
        "help_lang": "Використовуйте /lang ru|en|uk|es щоб змінити мову.",
        "buttons_fb": ["👍 Так", "👎 Ні"],
    },
    "es": {
        "hello": "¡Hola! ¿Cómo puedo ayudarte hoy? ¿Alguna duda sobre tu salud?",
        "where": "Necesito un poco más de información. ¿Dónde exactamente te duele?",
        "kind": "¿Qué tipo de dolor es: sordo, agudo, palpitante o ardor?",
        "duration": "¿Desde cuándo lo tienes?",
        "thanks": "Gracias por tu mensaje. ¿Dónde exactamente te duele y desde cuándo?",
        "bad_llm": "Perdón, la IA no está disponible ahora. Vamos a resolverlo manualmente.",
        "quick_menu": ["Cabeza", "Garganta", "Espalda", "Estómago", "Otro"],
        "kinds_menu": ["Sordo", "Agudo", "Palpitante", "Ardor", "Omitir"],
        "ask_more_kind": "Entendido. Aclaremos el tipo de dolor.",
        "ask_more_duration": "Gracias. ¿Cuánto tiempo (minutos/horas/días)?",
        "tips_head": "💡 Posibles causas: estrés, deshidratación, poco sueño\n🪪 Prueba: agua, descanso, aire fresco\n🚨 Médico: dolor súbito/intenso, con náuseas o problemas de visión",
        "tips_throat": "💡 Posibles causas: virus, irritación, sequedad\n🪪 Prueba: bebidas tibias, descanso, humidificar\n🚨 Médico: fiebre alta, dolor intenso, dificultad para respirar",
        "tips_back": "💡 Posibles causas: tensión muscular, postura, carga\n🪪 Prueba: calor suave, estiramientos ligeros, descanso\n🚨 Médico: dolor irradiado, entumecimiento, debilidad",
        "tips_stomach": "💡 Posibles causas: gastritis, alimentación, estrés\n🪪 Prueba: agua tibia, descanso, evitar comida 2 horas\n🚨 Médico: dolor agudo, fiebre, vómitos",
        "feedback_q": "Gracias por la valoración 🙏\n¿Quieres añadir un comentario? Escríbelo ahora.\nO envía /skip para omitir.",
        "fb_saved": "¡Comentario guardado, gracias! 🙌",
        "lang_set": "Idioma guardado: Español",
        "help_lang": "Usa /lang ru|en|uk|es para cambiar el idioma.",
        "buttons_fb": ["👍 Sí", "👎 No"],
    }
}

# Синонимы (локализация парсинга)
LOC_SYNS = {
    "ru": {
        "head": ["голова", "голове", "голов", "висок", "лоб", "затылок"],
        "throat": ["горло", "горле"],
        "back": ["спина", "поясница", "позвоночник"],
        "stomach": ["живот", "желудок", "кишки", "теперев живот"],
        "other": ["другое", "прочее"]
    },
    "en": {
        "head": ["head", "temple", "forehead", "occiput"],
        "throat": ["throat"],
        "back": ["back", "lower back", "spine"],
        "stomach": ["stomach", "belly", "abdomen", "tummy"],
        "other": ["other"]
    },
    "uk": {
        "head": ["голова", "висок", "лоб", "потилиця"],
        "throat": ["горло"],
        "back": ["спина", "поперек", "хребет"],
        "stomach": ["живіт", "шлунок", "кишки"],
        "other": ["інше"]
    },
    "es": {
        "head": ["cabeza", "sien", "frente", "nuca"],
        "throat": ["garganta"],
        "back": ["espalda", "lumbago", "lumbar", "columna"],
        "stomach": ["estómago", "barriga", "abdomen", "panza"],
        "other": ["otro", "otra"]
    }
}

KIND_SYNS = {
    "ru": {
        "dull": ["тупая", "тупой"],
        "sharp": ["острая", "острый", "режущая", "режущий", "колющая", "колющий"],
        "throb": ["пульсирующая", "пульсирующий"],
        "burn": ["жгучая", "жгучий", "жжение", "жжет", "жжёт"]
    },
    "en": {
        "dull": ["dull"],
        "sharp": ["sharp", "stabbing", "cutting"],
        "throb": ["throbbing", "pulsating"],
        "burn": ["burning", "burn"]
    },
    "uk": {
        "dull": ["тупий", "тупа"],
        "sharp": ["гострий", "гостра", "колючий", "ріжучий"],
        "throb": ["пульсівний", "пульсівна"],
        "burn": ["пекучий", "пекуча", "печіння"]
    },
    "es": {
        "dull": ["sordo", "sorda"],
        "sharp": ["agudo", "aguda", "punzante"],
        "throb": ["palpitante", "pulsátil"],
        "burn": ["ardor", "ardiente", "quemazón"]
    }
}

DUR_PATTERNS = {
    "ru": r"(\d+)\s*(мин|минут|час|часа|часов|дн|дней)",
    "en": r"(\d+)\s*(min|mins|minute|minutes|hour|hours|day|days)",
    "uk": r"(\d+)\s*(хв|хвилин|год|годин|дн|днів)",
    "es": r"(\d+)\s*(min|mins|minutos|minuto|hora|horas|día|días)"
}

def get_tips(lang, loc_key):
    if loc_key == "head": return T[lang]["tips_head"]
    if loc_key == "throat": return T[lang]["tips_throat"]
    if loc_key == "back": return T[lang]["tips_back"]
    if loc_key == "stomach": return T[lang]["tips_stomach"]
    return ""

# ------------------------------------------------
# HELPERS
# ------------------------------------------------
def get_lang_for_user(update: Update) -> str:
    user_id = update.effective_user.id
    txt = (update.message.text or "").strip() if update.message else ""
    detected = None
    if txt:
        try:
            detected = detect(txt)
        except Exception:
            detected = None
    tg_code = update.effective_user.language_code
    # приоритет: уже сохранённый -> detect -> telegram -> en
    lang = user_state.get(user_id, {}).get("lang")
    if lang in SUPPORTED:
        return lang
    for candidate in [detected, tg_code]:
        if candidate:
            n = norm_lang(candidate)
            if n in SUPPORTED:
                return n
    return "en"

def set_lang_for_user(user_id: int, lang: str):
    st = user_state.setdefault(user_id, {})
    st["lang"] = lang
    st.setdefault("slots", {"loc": None, "kind": None, "duration": None})
    st.setdefault("awaiting", None)

def parse_loc(text: str, lang: str):
    txt = text.lower()
    syns = LOC_SYNS[lang]
    for key, words in syns.items():
        for w in words:
            if w in txt:
                return key
    return None

def parse_kind(text: str, lang: str):
    txt = text.lower()
    syns = KIND_SYNS[lang]
    for key, words in syns.items():
        for w in words:
            if w in txt:
                return key
    return None

def parse_duration(text: str, lang: str):
    txt = text.lower()
    p = DUR_PATTERNS[lang]
    m = re.search(p, txt)
    if not m:
        return None
    num = m.group(1)
    unit = m.group(2)
    return f"{num} {unit}"

def quick_menu(lang):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(x) for x in T[lang]["quick_menu"]]],
        resize_keyboard=True
    )

def kinds_menu(lang):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(x) for x in T[lang]["kinds_menu"]]],
        resize_keyboard=True
    )

def feedback_buttons(lang):
    b = T[lang]["buttons_fb"]
    return InlineKeyboardMarkup([[InlineKeyboardButton(b[0], callback_data="feedback_yes"),
                                  InlineKeyboardButton(b[1], callback_data="feedback_no")]])

# ------------------------------------------------
# COMMANDS
# ------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_lang_for_user(update)
    set_lang_for_user(user_id, lang)
    await update.message.reply_text(T[lang]["hello"], reply_markup=quick_menu(lang))

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(T[get_lang_for_user(update)]["help_lang"])
        return
    candidate = norm_lang(context.args[0])
    if candidate not in SUPPORTED:
        await update.message.reply_text(T[get_lang_for_user(update)]["help_lang"])
        return
    set_lang_for_user(user_id, candidate)
    await update.message.reply_text(T[candidate]["lang_set"], reply_markup=quick_menu(candidate))

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_lang_for_user(update)
    st = user_state.setdefault(user_id, {"lang": lang, "slots": {"loc": None, "kind": None, "duration": None}})
    # пропускаем текущий слот
    if st.get("awaiting") == "kind":
        st["slots"]["kind"] = "skip"
    elif st.get("awaiting") == "duration":
        st["slots"]["duration"] = "skip"
    st["awaiting"] = None
    await ask_next_or_reply(update, lang, st)

# ------------------------------------------------
# FEEDBACK (inline)
# ------------------------------------------------
async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = user_state.get(user_id, {}).get("lang", "en")
    name = query.data  # feedback_yes / feedback_no
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    username = query.from_user.username or ""

    add_feedback_row([timestamp, str(user_id), name, username, "", ""])
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(T[lang]["feedback_q"])

# ------------------------------------------------
# CORE: slot filling + reply
# ------------------------------------------------
async def ask_next_or_reply(update: Update, lang: str, st: dict):
    slots = st["slots"]
    msg = update.message

    # Если нет локализации — спросить где
    if not slots.get("loc"):
        st["awaiting"] = "loc"
        await msg.reply_text(T[lang]["where"], reply_markup=quick_menu(lang))
        return

    # Если нет характера — спросить характер
    if not slots.get("kind"):
        st["awaiting"] = "kind"
        await msg.reply_text(T[lang]["ask_more_kind"], reply_markup=kinds_menu(lang))
        return

    # Если нет длительности — спросить длительность
    if not slots.get("duration"):
        st["awaiting"] = "duration"
        await msg.reply_text(T[lang]["ask_more_duration"])
        return

    # Всё есть или частично заполнено → дать советы по локации
    loc_key = slots["loc"]
    tips = get_tips(lang, loc_key)
    st["awaiting"] = None
    await msg.reply_text(tips, reply_markup=feedback_buttons(lang))

    # сбросим слот-диалог, но язык оставим
    st["slots"] = {"loc": None, "kind": None, "duration": None}

# ------------------------------------------------
# MESSAGE HANDLER
# ------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    lang = get_lang_for_user(update)
    set_lang_for_user(user_id, lang)
    st = user_state[user_id]

    # Если пользователь отвечает комментарием после фидбэка
    if text.lower() != "/skip" and text not in sum((T[l]["quick_menu"] + T[l]["kinds_menu"] for l in SUPPORTED), []):
        # возможно это свободный текст — попробуем вытащить слоты
        loc = parse_loc(text, lang)
        if loc:
            st["slots"]["loc"] = loc
        kind = parse_kind(text, lang)
        if kind:
            st["slots"]["kind"] = kind
        dur = parse_duration(text, lang)
        if dur:
            st["slots"]["duration"] = dur

    # Обрабатываем выбор из меню локаций
    menu_map = {
        "ru": {"Голова": "head", "Горло": "throat", "Спина": "back", "Живот": "stomach"},
        "en": {"Head": "head", "Throat": "throat", "Back": "back", "Stomach": "stomach"},
        "uk": {"Голова": "head", "Горло": "throat", "Спина": "back", "Живіт": "stomach"},
        "es": {"Cabeza": "head", "Garganta": "throat", "Espalda": "back", "Estómago": "stomach"},
    }
    if text in menu_map[lang]:
        st["slots"]["loc"] = menu_map[lang][text]

    # Обрабатываем выбор характера боли
    kind_map = {
        "ru": {"Тупая": "dull", "Острая": "sharp", "Пульсирующая": "throb", "Жгучая": "burn", "Пропустить": "skip"},
        "en": {"Dull": "dull", "Sharp": "sharp", "Throbbing": "throb", "Burning": "burn", "Skip": "skip"},
        "uk": {"Тупий": "dull", "Гострий": "sharp", "Пульсівний": "throb", "Пекучий": "burn", "Пропустити": "skip"},
        "es": {"Sordo": "dull", "Agudo": "sharp", "Palpitante": "throb", "Ardor": "burn", "Omitir": "skip"},
    }
    if text in kind_map[lang]:
        st["slots"]["kind"] = kind_map[lang][text]

    # Если пользователь прислал просто “спасибо”/“окей” и т.п. — коротко ответим
    if re.fullmatch(r"(спасибо|дякую|thanks|thank you|ok|okay|gracias|vale)", text.lower()):
        await update.message.reply_text("🫶")
        return

    # Перейдём к следующему вопросу/ответу
    await ask_next_or_reply(update, lang, st)

# ------------------------------------------------
# STARTUP
# ------------------------------------------------
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("skip", cmd_skip))

    app.add_handler(CallbackQueryHandler(feedback_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
