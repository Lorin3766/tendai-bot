# -*- coding: utf-8 -*-
"""
Minimal PRO intake (6 шагов): sex, age, goal, chronic, meds, sleep.

Экспорт:
- intake_entry_button(label: Optional[str]) -> InlineKeyboardButton
- register_intake_pro(app, on_complete_cb: Optional[Callable])

Как это работает:
- Кнопка/коллбек "ipro:start" запускает опрос.
- На каждом шаге можно выбрать кнопку, написать ответ или пропустить.
- После 6-го шага вызывается on_complete_cb(update, context, profile_dict).
- Модуль не зависит от Google Sheets и ничего сам не сохраняет.
"""

from __future__ import annotations
from typing import List, Tuple, Optional, Dict, Any, Callable

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

__all__ = ["intake_entry_button", "register_intake_pro"]

# ───────────────────────── i18n ─────────────────────────


def _lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Язык: сначала user_data['lang'], потом код из Telegram."""
    code = (
        context.user_data.get("lang")
        or getattr(update.effective_user, "language_code", "en")
    )
    return (code or "en").split("-")[0].lower() or "en"


def _t(lang: str, key: str):
    RU = {
        "title": "Быстрый опрос (6 шагов). Можно выбрать кнопкой или написать свой ответ.",
        "write": "✍️ Написать",
        "skip": "⏭️ Пропустить",
        "done": "Готово! Сохраняю профиль…",
        "ask_free": "Напишите короткий ответ:",
        "steps": [
            "Шаг 1/6. Пол:",
            "Шаг 2/6. Возраст:",
            "Шаг 3/6. Главная цель:",
            "Шаг 4/6. Хронические болезни:",
            "Шаг 5/6. Лекарства/добавки/аллергии:",
            "Шаг 6/6. Сон (отбой/подъём, напр. 23:30/07:00):",
        ],
    }
    EN = {
        "title": "Quick intake (6 steps). Use buttons or type your answer.",
        "write": "✍️ Write",
        "skip": "⏭️ Skip",
        "done": "Done! Saving your profile…",
        "ask_free": "Type your answer:",
        "steps": [
            "Step 1/6. Sex:",
            "Step 2/6. Age:",
            "Step 3/6. Main goal:",
            "Step 4/6. Chronic conditions:",
            "Step 5/6. Meds/supplements/allergies:",
            "Step 6/6. Sleep (bed/wake, e.g., 23:30/07:00):",
        ],
    }
    data = EN if lang == "en" else RU
    return data["steps"] if key == "steps" else data.get(key, key)


# ──────────────────────── Шаги опроса ────────────────────────

Step = Dict[str, Any]
_STEPS: List[Step] = [
    {
        "key": "sex",
        "opts": {
            "ru": [("Мужской", "male"), ("Женский", "female"), ("Другое", "other")],
            "en": [("Male", "male"), ("Female", "female"), ("Other", "other")],
        },
    },
    {
        "key": "age",
        "opts": {
            "ru": [("18–25", "22"), ("26–35", "30"), ("36–45", "40"), ("46–60", "50"), ("60+", "65")],
            "en": [("18–25", "22"), ("26–35", "30"), ("36–45", "40"), ("46–60", "50"), ("60+", "65")],
        },
    },
    {
        "key": "goal",
        "opts": {
            "ru": [("Похудение", "weight"), ("Энергия", "energy"), ("Сон", "sleep"), ("Долголетие", "longevity"), ("Сила", "strength")],
            "en": [("Weight", "weight"), ("Energy", "energy"), ("Sleep", "sleep"), ("Longevity", "longevity"), ("Strength", "strength")],
        },
    },
    {
        "key": "chronic",
        "opts": {
            "ru": [("Нет", "none"), ("Гипертония", "hypertension"), ("Диабет", "diabetes"), ("Щитовидка", "thyroid"), ("Другое", "other")],
            "en": [("None", "none"), ("Hypertension", "hypertension"), ("Diabetes", "diabetes"), ("Thyroid", "thyroid"), ("Other", "other")],
        },
    },
    {
        "key": "meds",
        "opts": {
            "ru": [("Нет", "none"), ("Магний", "magnesium"), ("Витамин D", "vitd"), ("Аллергии есть", "allergies"), ("Другое", "other")],
            "en": [("None", "none"), ("Magnesium", "magnesium"), ("Vitamin D", "vitd"), ("Allergies", "allergies"), ("Other", "other")],
        },
    },
    {
        "key": "sleep",
        "opts": {
            "ru": [("23:00/07:00", "23:00/07:00"), ("00:00/08:00", "00:00/08:00"), ("Нерегулярно", "irregular")],
            "en": [("23:00/07:00", "23:00/07:00"), ("00:00/08:00", "00:00/08:00"), ("Irregular", "irregular")],
        },
    },
]

# ──────────────────────── Публичный API ────────────────────────


def intake_entry_button(label: Optional[str] = None) -> InlineKeyboardButton:
    """Кнопка запуска опроса (callback_data='ipro:start')."""
    return InlineKeyboardButton(label or "🧩 Intake (6-step)", callback_data="ipro:start")


def register_intake_pro(app, **kwargs):
    """
    Регистрирует обработчики опроса.
    Параметры:
      - on_complete_cb: Optional[Callable[[Update, Context, dict], Awaitable[None]]]
    Остальные kwargs игнорируются для совместимости.
    """
    app.bot_data.setdefault("ipro_cfg", {})
    if "on_complete_cb" in kwargs:
        app.bot_data["ipro_cfg"]["on_complete_cb"] = kwargs["on_complete_cb"]

    # ВАЖНО: текстовый хендлер ставим в group=0, чтобы перехватывать ответы раньше общего msg_text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _ipro_text), group=0)
    app.add_handler(CallbackQueryHandler(_ipro_cb, pattern=r"^ipro:"))


# ──────────────────────── Внутреннее ────────────────────────


def _kb_for_step(lang: str, key: str, opts: List[Tuple[str, str]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for label, val in opts:
        row.append(InlineKeyboardButton(label, callback_data=f"ipro:choose|{key}|{val}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(_t(lang, "write"), callback_data=f"ipro:write|{key}"),
            InlineKeyboardButton(_t(lang, "skip"), callback_data=f"ipro:skip|{key}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, kb: Optional[InlineKeyboardMarkup] = None):
    """Ответ в текущий чат (и при callback, и при обычном сообщении)."""
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=kb or ReplyKeyboardRemove())
    else:
        await update.message.reply_text(text, reply_markup=kb or ReplyKeyboardRemove())


async def _send_step(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    lang = _lang(update, context)
    step = _STEPS[idx]
    steps_titles = _t(lang, "steps")
    kb = _kb_for_step(lang, step["key"], step["opts"]["en" if lang == "en" else "ru"])
    await _reply(update, context, steps_titles[idx], kb)


async def _ipro_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    # Старт
    if data == "ipro:start":
        context.user_data["ipro"] = {"idx": 0, "answers": {}, "wait_key": None}
        lang = _lang(update, context)
        await q.message.reply_text(_t(lang, "title"))
        await _send_step(update, context, 0)
        return

    ud: Dict[str, Any] = context.user_data.get("ipro") or {}
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
        await q.message.reply_text(_t(lang, "ask_free"))
        return

    if data.startswith("ipro:skip|"):
        _, _, key = data.split("|", 2)
        ud["answers"].setdefault(key, "")
        context.user_data["ipro"] = ud
        await _advance(update, context)
        return


async def _ipro_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Свободный ввод — только если нас об этом попросили на предыдущем шаге."""
    ud: Dict[str, Any] = context.user_data.get("ipro") or {}
    wait_key = ud.get("wait_key")
    if not wait_key:
        # Это не наш текст — пропускаем, другие хендлеры его обработают.
        return

    val = (update.message.text or "").strip()
    if wait_key == "age":
        # извлечь двузначный возраст, если пользователь ввёл фразу
        import re

        m = re.search(r"\d{2}", val)
        if m:
            val = m.group(0)

    ud["answers"][wait_key] = val
    ud["wait_key"] = None
    context.user_data["ipro"] = ud
    await _advance(update, context)


async def _advance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переход к следующему шагу, либо завершение опроса."""
    ud: Dict[str, Any] = context.user_data.get("ipro") or {}
    idx = int(ud.get("idx", 0)) + 1
    ud["idx"] = idx
    context.user_data["ipro"] = ud

    if idx < len(_STEPS):
        await _send_step(update, context, idx)
        return

    # Готово — отдать профиль наверх и очистить состояние.
    lang = _lang(update, context)
    await _reply(update, context, _t(lang, "done"))

    answers: Dict[str, str] = ud.get("answers", {})

    # Нормализуем ключи под main.py (ты их маппишь в _ipro_on_done)
    profile = {
        "sex": answers.get("sex", ""),
        "age": answers.get("age", ""),
        "goal": answers.get("goal", ""),
        "chronic": answers.get("chronic", ""),
        "meds": answers.get("meds", ""),
        "hab_sleep": answers.get("sleep", ""),
        "hab_activity": "",  # в этой мини-версии шага активности нет
        "complaints": set(),
    }

    cfg = context.application.bot_data.get("ipro_cfg") or {}
    on_done: Optional[Callable] = cfg.get("on_complete_cb")
    if callable(on_done):
        try:
            await on_done(update, context, profile)
        except Exception as e:  # не валим бота, просто логируем
            import logging

            logging.error(f"[intake_pro] on_complete_cb error: {e}")

    # очистить стейт опроса
    context.user_data["ipro"] = {}
