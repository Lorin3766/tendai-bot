# -*- coding: utf-8 -*-
from typing import Optional, Set, Dict
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

# -------- Публичный API модуля --------
def register_intake_pro(application, gspread_client=None, spreadsheet_id: Optional[str] = None, on_finish=None) -> None:
    """
    Регистрирует хендлеры ПРО-опросника в приложении PTB.
    - gspread_client / spreadsheet_id — просто сохраняются в bot_data (на будущее / для совместимости).
    - on_finish: async-колбэк вида (update, context, profile_dict) — вызывается по завершении шага 6/6.
    """
    application.bot_data["ipro_on_finish"] = on_finish
    application.bot_data["ipro_gs"] = {"gclient": gspread_client, "spreadsheet_id": spreadsheet_id}

    # Один компактный CallbackQueryHandler на весь опросник:
    application.add_handler(CallbackQueryHandler(_ipro_cb, pattern=r"^intake:"))

def intake_entry_button(text: str = "🙏 Опросник (6 пунктов)") -> InlineKeyboardButton:
    """Кнопка для запуска опросника из любого меню/интерфейса."""
    return InlineKeyboardButton(text, callback_data="intake:start")


# -------- Локализация (минимально необходимая) --------
_I18N = {
    "en": {
        "intro": "Quick 6-step intake to tailor your advice.",
        "step1": "Step 1/6. Sex:",
        "step2": "Step 2/6. Age:",
        "step3": "Step 3/6. Main goal:",
        "step4": "Step 4/6. Chronic conditions (toggle, then Next):",
        "step5": "Step 5/6. Sleep pattern:",
        "step6": "Step 6/6. Activity level:",
        "next": "Next",
        "skip": "Skip",
        "back": "◀ Back",
        "done": "Finish",
        "saved": "Saved profile.",
        "sex_opts": [("Male", "male"), ("Female", "female"), ("Other", "other")],
        "age_opts": [("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "goal_opts": [("Weight","weight"),("Energy","energy"),("Sleep","sleep"),("Longevity","longevity"),("Strength","strength")],
        "chronic_opts": [("None","none"),("Hypertension","hypertension"),("Diabetes","diabetes"),("Thyroid","thyroid"),("Other","other")],
        "sleep_opts": [("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Irregular","irregular")],
        "activity_opts": [("<5k steps","<5k"),("5–8k","5-8k"),("8–12k","8-12k"),("Regular sport","sport")],
    },
    "ru": {
        "intro": "Короткий опрос из 6 шагов, чтобы советы были точнее.",
        "step1": "Шаг 1/6. Пол:",
        "step2": "Шаг 2/6. Возраст:",
        "step3": "Шаг 3/6. Главная цель:",
        "step4": "Шаг 4/6. Хронические болезни (выбирайте/отменяйте, затем «Далее»):",
        "step5": "Шаг 5/6. Режим сна:",
        "step6": "Шаг 6/6. Уровень активности:",
        "next": "Далее",
        "skip": "Пропустить",
        "back": "◀ Назад",
        "done": "Завершить",
        "saved": "Профиль сохранён.",
        "sex_opts": [("Мужской","male"),("Женский","female"),("Другое","other")],
        "age_opts": [("18–25","22"),("26–35","30"),("36–45","40"),("46–60","50"),("60+","65")],
        "goal_opts": [("Похудение","weight"),("Энергия","energy"),("Сон","sleep"),("Долголетие","longevity"),("Сила","strength")],
        "chronic_opts": [("Нет","none"),("Гипертония","hypertension"),("Диабет","diabetes"),("Щитовидка","thyroid"),("Другое","other")],
        "sleep_opts": [("23:00/07:00","23:00/07:00"),("00:00/08:00","00:00/08:00"),("Нерегулярно","irregular")],
        "activity_opts": [("<5к шагов","<5k"),("5–8к","5-8k"),("8–12к","8-12k"),("Спорт регулярно","sport")],
    }
}

def _lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    # Пытаемся взять язык из user_data, иначе — из Telegram, дефолт 'en'
    l = (context.user_data.get("lang")
         or (getattr(update.effective_user, "language_code", None) or "en").split("-")[0].lower())
    return "ru" if l in ("ru","uk","be","kk") else "en"


# --------- Рендеры клавиатур ---------
def _kb_from_pairs(prefix: str, pairs, per_row: int = 3) -> InlineKeyboardMarkup:
    rows, row = [], []
    for text, val in pairs:
        row.append(InlineKeyboardButton(text, callback_data=f"intake:{prefix}:{val}"))
        if len(row) == per_row:
            rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def _kb_chronic(lang: str, selected: Set[str]) -> InlineKeyboardMarkup:
    labels = _I18N[lang]["chronic_opts"]
    rows, row = [], []
    for text, val in labels:
        mark = "✅ " if val in selected else ""
        row.append(InlineKeyboardButton(mark + text, callback_data=f"intake:chronic:toggle:{val}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton(_I18N[lang]["next"], callback_data="intake:chronic:next"),
                 InlineKeyboardButton(_I18N[lang]["skip"], callback_data="intake:chronic:skip")])
    return InlineKeyboardMarkup(rows)


# --------- Основной Callback опросника ---------
async def _ipro_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data  # "intake:...."
    lang = _lang(update, context)
    t = _I18N[lang]

    # Состояние опросника у пользователя
    st: Dict = context.user_data.setdefault("ipro", {
        "step": 0,
        "sex": "",
        "age": "",
        "goal": "",
        "chronic": set(),   # type: ignore
        "hab_sleep": "",
        "hab_activity": "",
        "meds": "",         # отдельным шагом не собираем, пусть будет пустым
        "complaints": set() # зарезервировано
    })
    # Исправляем тип (после рестарта может восстановиться как list)
    if not isinstance(st.get("chronic"), set):
        st["chronic"] = set(st.get("chronic") or [])

    # --- Старт ---
    if data == "intake:start":
        st.update({"step": 1, "sex":"", "age":"", "goal":"", "chronic": set(),
                   "hab_sleep":"", "hab_activity":"", "meds":"", "complaints": set()})
        await q.message.reply_text(t["intro"])
        await q.message.reply_text(t["step1"], reply_markup=_kb_from_pairs("sex", t["sex_opts"], per_row=3))
        return

    # --- Шаг 1: Пол ---
    if data.startswith("intake:sex:"):
        st["sex"] = data.split(":")[-1]
        st["step"] = 2
        await q.message.reply_text(t["step2"], reply_markup=_kb_from_pairs("age", t["age_opts"], per_row=3))
        return

    # --- Шаг 2: Возраст ---
    if data.startswith("intake:age:"):
        st["age"] = data.split(":")[-1]
        st["step"] = 3
        await q.message.reply_text(t["step3"], reply_markup=_kb_from_pairs("goal", t["goal_opts"], per_row=3))
        return

    # --- Шаг 3: Цель ---
    if data.startswith("intake:goal:"):
        st["goal"] = data.split(":")[-1]
        st["step"] = 4
        await q.message.reply_text(t["step4"], reply_markup=_kb_chronic(lang, st["chronic"]))
        return

    # --- Шаг 4: Хронические (мультивыбор) ---
    if data.startswith("intake:chronic:toggle:"):
        key = data.split(":")[-1]
        sel: Set[str] = st["chronic"]
        if key in sel:
            sel.remove(key)
        else:
            # Если выбрали "none", снимаем всё остальное
            if key in ("none",):
                sel.clear()
            sel.add(key)
            # Если выбрали что-то кроме "none" — убрать "none"
            if key != "none" and "none" in sel:
                sel.remove("none")
        await q.edit_message_reply_markup(reply_markup=_kb_chronic(lang, sel))
        return

    if data == "intake:chronic:skip" or data == "intake:chronic:next":
        st["step"] = 5
        await q.message.reply_text(t["step5"], reply_markup=_kb_from_pairs("sleep", t["sleep_opts"], per_row=2))
        return

    # --- Шаг 5: Сон ---
    if data.startswith("intake:sleep:"):
        st["hab_sleep"] = data.split(":")[-1]
        st["step"] = 6
        await q.message.reply_text(t["step6"], reply_markup=_kb_from_pairs("activity", t["activity_opts"], per_row=2))
        return

    # --- Шаг 6: Активность -> финал ---
    if data.startswith("intake:activity:"):
        st["hab_activity"] = data.split(":")[-1]
        st["step"] = 7  # финальное внутреннее состояние

        # Подготовим профиль в удобном виде
        profile = {
            "sex": st.get("sex", ""),
            "age": st.get("age", ""),
            "goal": st.get("goal", ""),
            "chronic": set(st.get("chronic") or []),
            "meds": st.get("meds", ""),
            "hab_sleep": st.get("hab_sleep", ""),
            "hab_activity": st.get("hab_activity", ""),
            "complaints": set(st.get("complaints") or []),
        }

        # Колбэк, переданный хозяином приложения (main.py)
        on_finish = update.get_bot().bot_data.get("ipro_on_finish")
        if callable(on_finish):
            try:
                await on_finish(update, context, profile)
            except Exception:
                # Мягко деградируем: просто покажем “сохранено”
                await q.message.reply_text(t["saved"])
        else:
            await q.message.reply_text(t["saved"])

        # Очистим state
        try:
            context.user_data.pop("ipro", None)
        except Exception:
            pass
        return
