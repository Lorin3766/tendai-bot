# intake_pro.py — PRO-опросник 6 пунктов (python-telegram-bot v20+)
import re
from typing import Dict, Set
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters

def register_intake_pro(app, save_profile_cb=None):
    """Подключение хендлеров к Application.
    save_profile_cb(update, context, profile_dict) — опционально, если нужно писать в Sheets.
    """
    app.bot_data["ipro_save_cb"] = save_profile_cb
    app.add_handler(CommandHandler("intake", _ipro_start_cmd))
    app.add_handler(CallbackQueryHandler(_ipro_cb, pattern=r"^ipro:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _ipro_text_router))

# -------- I18N ----------
def _T(lang: str):
    RU = {
        "title": "🧩 Профиль (PRO) — 6 пунктов за 40–60 сек. Поехали?",
        "btn_start": "Начать",
        "age_sex": "1/6 — Возраст и пол.\nНапишите одной строкой, напр.: «42, мужчина».",
        "age_sex_ok": "Принято: {age} лет, {sex}.",
        "age_sex_err": "Не понял. Напишите так: «42, мужчина» или «42 M».",
        "chronic": "2/6 — Хронические болезни. Отметьте и нажмите «Готово».",
        "none": "Нет",
        "cvd": "Сердечно-сосудистые",
        "dm": "Сахарный диабет",
        "gi": "ЖКТ/печень/почки",
        "other": "Другое",
        "done": "Готово",
        "goal": "3/6 — Главная цель:",
        "g_longevity": "Долголетие",
        "g_weight": "Снижение веса",
        "g_energy": "Энергия/работоспособность",
        "g_heart": "Поддержка сердца/сосудов",
        "g_other": "Другое",
        "hab": "4/6 — Привычки. Отметьте варианты и «Готово».",
        "smoke": "Курение",
        "s_none": "Нет",
        "s_rare": "Иногда",
        "s_daily": "Ежедневно",
        "alcohol": "Алкоголь",
        "a_none": "Нет",
        "a_rare": "Редко",
        "a_weekly": "1–3/нед",
        "a_daily": "Ежедневно",
        "activity": "Активность",
        "act_low": "Сидячий",
        "act_5_8": "5–8k шагов",
        "act_8_12": "8–12k шагов",
        "act_sport": "Спорт 2+/нед",
        "sleep": "Сон",
        "sl_5_6": "5–6 ч",
        "sl_6_7": "6–7 ч",
        "sl_7_8": "7–8 ч",
        "sl_8_9": "8–9 ч",
        "compl": "5/6 — Что беспокоит чаще? (можно несколько) Затем «Готово».",
        "c_head": "Голова",
        "c_heart": "Сердце/давление",
        "c_gi": "ЖКТ",
        "c_joints": "Суставы/спина",
        "c_fatigue": "Усталость/стресс",
        "meds": "6/6 — Лекарства/добавки.\nНапишите короткий список или «нет».",
        "saved": "✅ Профиль сохранён.",
        "sex_m": "мужчина",
        "sex_f": "женщина",
        "sex_u": "не указан",
    }
    EN = {
        "title": "🧩 Profile (PRO) — 6 items in 40–60s. Ready?",
        "btn_start": "Start",
        "age_sex": "1/6 — Age & sex.\nType one line, e.g., “42, male”.",
        "age_sex_ok": "Got it: {age} y/o, {sex}.",
        "age_sex_err": "Please type like “42, male”.",
        "chronic": "2/6 — Chronic conditions. Toggle and press “Done”.",
        "none": "None",
        "cvd": "Cardio-vascular",
        "dm": "Diabetes",
        "gi": "GI/Liver/Kidney",
        "other": "Other",
        "done": "Done",
        "goal": "3/6 — Main goal:",
        "g_longevity": "Longevity",
        "g_weight": "Weight loss",
        "g_energy": "Energy/productivity",
        "g_heart": "Heart & vessels support",
        "g_other": "Other",
        "hab": "4/6 — Habits. Toggle options, then “Done”.",
        "smoke": "Smoking",
        "s_none": "No",
        "s_rare": "Occasional",
        "s_daily": "Daily",
        "alcohol": "Alcohol",
        "a_none": "No",
        "a_rare": "Rare",
        "a_weekly": "1–3/wk",
        "a_daily": "Daily",
        "activity": "Activity",
        "act_low": "Sedentary",
        "act_5_8": "5–8k steps",
        "act_8_12": "8–12k steps",
        "act_sport": "Sport 2+/wk",
        "sleep": "Sleep",
        "sl_5_6": "5–6 h",
        "sl_6_7": "6–7 h",
        "sl_7_8": "7–8 h",
        "sl_8_9": "8–9 h",
        "compl": "5/6 — What bothers you most? (multi-select) Then “Done”.",
        "c_head": "Head",
        "c_heart": "Heart/BP",
        "c_gi": "GI",
        "c_joints": "Joints/back",
        "c_fatigue": "Fatigue/stress",
        "meds": "6/6 — Meds/supps. Type a short list or “none”.",
        "saved": "✅ Profile saved.",
        "sex_m": "male",
        "sex_f": "female",
        "sex_u": "unspecified",
    }
    return EN if lang == "en" else RU

def _lang(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return ctx.user_data.get("lang", "en")

# -------- Flow ----------
async def _ipro_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _lang(context)
    kb = [[InlineKeyboardButton(_T(lang)["btn_start"], callback_data="ipro:start")]]
    await update.effective_chat.send_message(_T(lang)["title"],
                                             reply_markup=InlineKeyboardMarkup(kb))

async def _ipro_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = _lang(context)
    st: Dict = context.user_data.setdefault("ipro", {
        "age": None, "sex": None,
        "chronic": set(), "goal": None,
        "hab_smoke": None, "hab_alcohol": None, "hab_activity": None, "hab_sleep": None,
        "complaints": set(), "meds": None,
    })
    data = q.data.split(":")[1:]

    if data[0] == "start":
        context.user_data["ipro_expect"] = "age_sex"
        await q.edit_message_text(_T(lang)["age_sex"])
        return

    if data[0] == "chr":
        if data[1] == "toggle":
            key = data[2]
            if key in st["chronic"]:
                st["chronic"].remove(key)
            else:
                if key == "none":
                    st["chronic"] = {"none"}
                else:
                    st["chronic"].discard("none")
                    st["chronic"].add(key)
        elif data[1] == "done":
            await _show_goal(q, lang); return
        await _show_chronic(q, lang, st["chronic"]); return

    if data[0] == "goal":
        st["goal"] = data[1]
        await _show_habits(q, lang, st); return

    if data[0] == "hab":
        kind, val = data[1], data[2]
        if kind == "smoke": st["hab_smoke"] = val
        elif kind == "alcohol": st["hab_alcohol"] = val
        elif kind == "act": st["hab_activity"] = val
        elif kind == "sleep": st["hab_sleep"] = val
        elif kind == "done":
            await _show_complaints(q, lang, st["complaints"]); return
        await _show_habits(q, lang, st); return

    if data[0] == "compl":
        if data[1] == "toggle":
            key = data[2]
            if key in st["complaints"]: st["complaints"].remove(key)
            else: st["complaints"].add(key)
        elif data[1] == "done":
            context.user_data["ipro_expect"] = "meds"
            await q.edit_message_text(_T(lang)["meds"]); return
        await _show_complaints(q, lang, st["complaints"]); return

async def _ipro_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expect = context.user_data.get("ipro_expect")
    if not expect: return
    lang = _lang(context)
    st: Dict = context.user_data.setdefault("ipro", {})
    txt = (update.message.text or "").strip()

    if expect == "age_sex":
        age, sex = _parse_age_sex(txt)
        if age is None:
            await update.message.reply_text(_T(lang)["age_sex_err"]); return
        st["age"] = age; st["sex"] = sex or "u"
        context.user_data["ipro_expect"] = None
        sex_h = {"m": _T(lang)["sex_m"], "f": _T(lang)["sex_f"], "u": _T(lang)["sex_u"]}[st["sex"]]
        await update.message.reply_text(_T(lang)["age_sex_ok"].format(age=age, sex=sex_h))
        msg = await update.effective_chat.send_message("…")
        await _show_chronic(msg, lang, set()); return

    if expect == "meds":
        st["meds"] = txt
        context.user_data["ipro_expect"] = None

        # сохраним профиль локально и через опциональный callback
        context.user_data["ipro_profile"] = st.copy()
        save_cb = context.application.bot_data.get("ipro_save_cb")
        if callable(save_cb):
            await save_cb(update, context, st.copy())

        await update.message.reply_text(_T(lang)["saved"])
        context.user_data.pop("ipro", None)
        return

# -------- UI builders ----------
async def _show_chronic(target, lang: str, selected: Set[str]):
    def flag(k): return "✅ " if k in selected else ""
    kb = [
        [InlineKeyboardButton(flag("none")+_T(lang)["none"], callback_data="ipro:chr:toggle:none")],
        [InlineKeyboardButton(flag("cvd")+_T(lang)["cvd"], callback_data="ipro:chr:toggle:cvd")],
        [InlineKeyboardButton(flag("dm")+_T(lang)["dm"], callback_data="ipro:chr:toggle:dm")],
        [InlineKeyboardButton(flag("gi")+_T(lang)["gi"], callback_data="ipro:chr:toggle:gi")],
        [InlineKeyboardButton(flag("other")+_T(lang)["other"], callback_data="ipro:chr:toggle:other")],
        [InlineKeyboardButton(_T(lang)["done"], callback_data="ipro:chr:done")],
    ]
    await _edit_any(target, _T(lang)["chronic"], kb)

async def _show_goal(target, lang: str):
    kb = [
        [InlineKeyboardButton(_T(lang)["g_longevity"], callback_data="ipro:goal:longevity")],
        [InlineKeyboardButton(_T(lang)["g_weight"], callback_data="ipro:goal:weight")],
        [InlineKeyboardButton(_T(lang)["g_energy"], callback_data="ipro:goal:energy")],
        [InlineKeyboardButton(_T(lang)["g_heart"], callback_data="ipro:goal:heart")],
        [InlineKeyboardButton(_T(lang)["g_other"], callback_data="ipro:goal:other")],
    ]
    await _edit_any(target, _T(lang)["goal"], kb)

async def _show_habits(target, lang: str, st: Dict):
    def chk(k, v): return "✅ " if st.get(k) == v else ""
    kb = [
        [InlineKeyboardButton("— "+_T(lang)["smoke"]+" —", callback_data="ipro:hab:noop:x")],
        [
            InlineKeyboardButton(chk("hab_smoke","none")+_T(lang)["s_none"], callback_data="ipro:hab:smoke:none"),
            InlineKeyboardButton(chk("hab_smoke","rare")+_T(lang)["s_rare"], callback_data="ipro:hab:smoke:rare"),
            InlineKeyboardButton(chk("hab_smoke","daily")+_T(lang)["s_daily"], callback_data="ipro:hab:smoke:daily"),
        ],
        [InlineKeyboardButton("— "+_T(lang)["alcohol"]+" —", callback_data="ipro:hab:noop:x")],
        [
            InlineKeyboardButton(chk("hab_alcohol","none")+_T(lang)["a_none"], callback_data="ipro:hab:alcohol:none"),
            InlineKeyboardButton(chk("hab_alcohol","rare")+_T(lang)["a_rare"], callback_data="ipro:hab:alcohol:rare"),
            InlineKeyboardButton(chk("hab_alcohol","weekly")+_T(lang)["a_weekly"], callback_data="ipro:hab:alcohol:weekly"),
            InlineKeyboardButton(chk("hab_alcohol","daily")+_T(lang)["a_daily"], callback_data="ipro:hab:alcohol:daily"),
        ],
        [InlineKeyboardButton("— "+_T(lang)["activity"]+" —", callback_data="ipro:hab:noop:x")],
        [
            InlineKeyboardButton(chk("hab_activity","low")+_T(lang)["act_low"], callback_data="ipro:hab:act:low"),
            InlineKeyboardButton(chk("hab_activity","5-8k")+_T(lang)["act_5_8"], callback_data="ipro:hab:act:5-8k"),
            InlineKeyboardButton(chk("hab_activity","8-12k")+_T(lang)["act_8_12"], callback_data="ipro:hab:act:8-12k"),
            InlineKeyboardButton(chk("hab_activity","sport")+_T(lang)["act_sport"], callback_data="ipro:hab:act:sport"),
        ],
        [InlineKeyboardButton("— "+_T(lang)["sleep"]+" —", callback_data="ipro:hab:noop:x")],
        [
            InlineKeyboardButton(chk("hab_sleep","5-6")+_T(lang)["sl_5_6"], callback_data="ipro:hab:sleep:5-6"),
            InlineKeyboardButton(chk("hab_sleep","6-7")+_T(lang)["sl_6_7"], callback_data="ipro:hab:sleep:6-7"),
            InlineKeyboardButton(chk("hab_sleep","7-8")+_T(lang)["sl_7_8"], callback_data="ipro:hab:sleep:7-8"),
            InlineKeyboardButton(chk("hab_sleep","8-9")+_T(lang)["sl_8_9"], callback_data="ipro:hab:sleep:8-9"),
        ],
        [InlineKeyboardButton(_T(lang)["done"], callback_data="ipro:hab:done:x")],
    ]
    await _edit_any(target, _T(lang)["hab"], kb)

async def _show_complaints(target, lang: str, selected: Set[str]):
    def flag(k): return "✅ " if k in selected else ""
    kb = [
        [
            InlineKeyboardButton(flag("head")+_T(lang)["c_head"], callback_data="ipro:compl:toggle:head"),
            InlineKeyboardButton(flag("heart")+_T(lang)["c_heart"], callback_data="ipro:compl:toggle:heart"),
        ],
        [
            InlineKeyboardButton(flag("gi")+_T(lang)["c_gi"], callback_data="ipro:compl:toggle:gi"),
            InlineKeyboardButton(flag("joints")+_T(lang)["c_joints"], callback_data="ipro:compl:toggle:joints"),
        ],
        [InlineKeyboardButton(flag("fatigue")+_T(lang)["c_fatigue"], callback_data="ipro:compl:toggle:fatigue")],
        [InlineKeyboardButton(_T(lang)["done"], callback_data="ipro:compl:done")],
    ]
    await _edit_any(target, _T(lang)["compl"], kb)

async def _edit_any(target, text: str, kb_rows):
    markup = InlineKeyboardMarkup(kb_rows)
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=markup)
    else:
        await target.edit_text(text, reply_markup=markup)

def _parse_age_sex(s: str):
    m = re.search(r"(\d{1,3})", s)
    age = int(m.group(1)) if m else None
    s_low = s.lower()
    sex = None
    if re.search(r"\b(m|male|м|муж|мужчина)\b", s_low): sex = "m"
    elif re.search(r"\b(f|female|ж|жен|женщина)\b", s_low): sex = "f"
    return age, sex
