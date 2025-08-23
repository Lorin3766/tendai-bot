# -*- coding: utf-8 -*-
# Minimal PRO intake (6 steps): sex, age, goal, chronic, meds, sleep
# Exports:
#   - intake_entry_button(label: str|None) -> InlineKeyboardButton
#   - register_intake_pro(app, gclient=None, ws_profiles=None, on_complete_cb=None)

from typing import List, Tuple, Optional, Dict, Any, Callable
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters

# ---- i18n helpers ----
def _lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    # пробуем из user_data, иначе код Telegram
    return (context.user_data.get("lang")
            or getattr(update.effective_user, "language_code", "en").split("-")[0]
            or "en")

def _t(lang: str, key: str) -> str:
    RU = {
        "title": "Быстрый опрос (6 шагов). Можно выбрать кнопкой или написать свой ответ.",
        "write": "✍️ Написать",
        "skip": "⏭️ Пропустить",
        "done": "Готово! Сохраняю профиль…",
        "steps": [
            "Шаг 1/6. Пол:",
            "Шаг 2/6. Возраст:",
            "Шаг 3/6. Главная цель:",
            "Шаг 4/6. Хронические болезни:",
            "Шаг 5/6. Лекарства/добавки/аллергии:",
            "Шаг 6/6. Сон (отбой/подъём, напр. 23:30/07:00):",
        ]
    }
    EN = {
        "title": "Quick intake (6 steps). Use buttons or type your answer.",
        "write": "✍️ Write",
        "skip": "⏭️ Skip",
        "done": "Done! Saving your profile…",
        "steps": [
            "Step 1/6. Sex:",
            "Step 2/6. Age:",
            "Step 3/6. Main goal:",
            "Step 4/6. Chronic conditions:",
            "Step 5/6. Meds/supplements/allergies:",
            "Step 6/6. Sleep (bed/wake, e.g., 23:30/07:00):",
        ]
    }
    data = RU if lang != "en" else EN
    if key == "steps":
        return data["steps"]
    return data.get(key, key)

# ---- steps & options ----
Step = Dict[str, Any]
_STEPS: List[Step] = [
    {"key":"sex","opts":{
        "ru":[("Мужской","male"),("Женский","female"),("Другое","other")],
        "en":[("Male","male"),("Female","female"),("Other","other")],
    }},
    {"key":"age","opts":{
        "ru":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "en":[("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
    }},
    {"key":"goal","opts":{
        "ru":[("Похудение","weight"),("Энергия","energy"),("Сон","sleep"),("Долголетие","longevity"),("Сила","strength")],
        "en":[("Weight","weight"),("Energy","energy"),("Sleep","sleep"),("Longevity","longevity"),("Strength","strength")],
    }},
    {"key":"chronic","opts":{
        "ru":[("Нет","none"),("Гипертония","hypertension"),("Диабет","diabetes"),("Щитовидка","thyroid"),("Другое","other")],
        "en":[("None","none"),("Hypertension","hypertension"),("Diabetes","diabetes"),("Thyroid","thyroid"),("Other","other")],
    }},
    {"key":"meds","opts":{
        "ru":[("Нет","none"),("Магний","magnesium"),("Витамин D","vitd"),("Аллергии есть","allergies"),("Другое","other")],
        "en":[("None","none"),("Magnesium","magnesium"),("Vitamin D","vitd"),("Allergies","allergies"),("Other","other")],
    }},
    {"key":"sleep","opts":{
        "ru":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
        "en":[("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Irregular","irregular")],
    }},
]

# ---- public helpers ----
def intake_entry_button(label: Optional[str] = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(label or "🧩 Intake (6-step)", callback_data="ipro:start")

def register_intake_pro(app, gclient=None, ws_profiles=None, on_complete_cb: Optional[Callable]=None):
    # сохраним зависимости в bot_data
    app.bot_data.setdefault("ipro_cfg", {})
    app.bot_data["ipro_cfg"].update({
        "gclient": gclient,
        "ws_profiles": ws_profiles,
        "on_complete_cb": on_complete_cb,
    })

    app.add_handler(CallbackQueryHandler(_ipro_cb, pattern=r"^ipro:"))
    # текстовый ввод используем ТОЛЬКО когда ждём свободный ответ
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _ipro_text), group=1)

# ---- internals ----
def _kb_for_step(lang: str, key: str, opts: List[Tuple[str,str]]) -> InlineKeyboardMarkup:
    rows, row = [], []
    for label, val in opts:
        row.append(InlineKeyboardButton(label, callback_data=f"ipro:choose|{key}|{val}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton(_t(lang,"write"), callback_data=f"ipro:write|{key}"),
        InlineKeyboardButton(_t(lang,"skip"),  callback_data=f"ipro:skip|{key}"),
    ])
    return InlineKeyboardMarkup(rows)

async def _send_step(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    lang = _lang(update, context)
    steps_titles = _t(lang, "steps")
    step = _STEPS[idx]
    kb = _kb_for_step(lang, step["key"], step["opts"]["ru" if lang!="en" else "en"])
    msg = steps_titles[idx]
    await _reply(update, context, msg, kb)

async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, kb: Optional[InlineKeyboardMarkup]=None):
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=kb or ReplyKeyboardRemove())
    else:
        await update.message.reply_text(text, reply_markup=kb or ReplyKeyboardRemove())

async def _ipro_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "ipro:start":
        context.user_data["ipro"] = {"idx": 0, "answers": {}, "wait_key": None}
        lang = _lang(update, context)
        await q.message.reply_text(_t(lang, "title"))
        await _send_step(update, context, 0)
        return

    ud = context.user_data.get("ipro") or {}
    idx = ud.get("idx", 0)
    if not ud:
        return

    if data.startswith("ipro:choose|"):
        _, _, key, val = data.split("|", 3)
        ud["answers"][key] = val
        context.user_data["ipro"] = ud
        await _advance(update, context)
        return

    if data.startswith("ipro:write|"):
        _, _, key = data.split("|", 2)
        ud["wait_key"] = key
        context.user_data["ipro"] = ud
        lang = _lang(update, context)
        await q.message.reply_text("Напишите короткий ответ:" if lang!="en" else "Type your answer:")
        return

    if data.startswith("ipro:skip|"):
        _, _, key = data.split("|", 2)
        ud["answers"].setdefault(key, "")
        context.user_data["ipro"] = ud
        await _advance(update, context)
        return

async def _ipro_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data.get("ipro")
    if not ud or not ud.get("wait_key"):
        # не наша ситуация — пропускаем для других хендлеров
        return
    key = ud["wait_key"]; ud["wait_key"] = None
    val = (update.message.text or "").strip()
    if key == "age":
        # вытащим 2 цифры, если есть
        import re
        m = re.search(r"\d{2}", val)
        if m:
            val = m.group(0)
    ud["answers"][key] = val
    context.user_data["ipro"] = ud
    await _advance(update, context)

async def _advance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data.get("ipro") or {}
    idx = ud.get("idx", 0) + 1
    ud["idx"] = idx
    context.user_data["ipro"] = ud

    if idx < len(_STEPS):
        await _send_step(update, context, idx)
        return

    # Готово — соберём профиль и передадим наверх
    lang = _lang(update, context)
    await _reply(update, context, _t(lang, "done"))

    answers = ud.get("answers", {})
    # приводим к ключам, которые ждёт main.py
    profile = {
        "sex": answers.get("sex",""),
        "age": answers.get("age",""),
        "goal": answers.get("goal",""),
        "chronic": answers.get("chronic",""),
        "meds": answers.get("meds",""),
        "hab_sleep": answers.get("sleep",""),
        "hab_activity": "",   # в этой миниверсии нет отдельного шага
        "complaints": set(),  # пусто
    }

    cfg = context.application.bot_data.get("ipro_cfg") or {}
    on_done = cfg.get("on_complete_cb")
    if callable(on_done):
        try:
            await on_done(update, context, profile)
        except Exception as e:
            # не падаем из-за внешнего кода
            import logging
            logging.error(f"ipro on_complete_cb error: {e}")

    # очистим состояние
    context.user_data["ipro"] = {}
