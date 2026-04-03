"""
NutriBot – Telegram бот для планирования питания
Запуск: python bot.py
"""

import logging
import os
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from meals import DAYS, BASE_CALS, SHOPPING_LIST

# ─── Логирование ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Состояния диалога ────────────────────────────────────────────────────────
(
    ASK_NAME,
    ASK_AGE,
    ASK_GENDER,
    ASK_HEIGHT,
    ASK_WEIGHT,
    ASK_ACTIVITY,
    ASK_GOAL,
    SHOW_PLAN,
) = range(8)

# ─── Вспомогательные функции ──────────────────────────────────────────────────

def calc_calories(data: dict) -> dict:
    """Расчёт по формуле Миффлина-Сан Жеора."""
    w = float(data["weight"])
    h = float(data["height"])
    a = float(data["age"])
    act = float(data["activity"])

    if data["gender"] == "male":
        bmr = 10 * w + 6.25 * h - 5 * a + 5
    else:
        bmr = 10 * w + 6.25 * h - 5 * a - 161

    tdee = round(bmr * act)
    adj = {"lose": -500, "maintain": 0, "gain": 500}[data["goal"]]
    target = max(1200, tdee + adj)

    return {
        "bmr": round(bmr),
        "tdee": tdee,
        "target": target,
        "protein": round(target * 0.30 / 4),
        "carbs":   round(target * 0.45 / 4),
        "fat":     round(target * 0.25 / 9),
    }


def scale_amount(text: str, factor: float) -> str:
    """Масштабирует числа внутри строки с граммовкой."""
    skip = {"по вкусу", "по желанию", "щепотка", "—", ""}
    if not text or text.strip() in skip:
        return text

    def replace(m):
        val = float(m.group())
        scaled = round(val * factor, 1)
        return str(int(scaled)) if scaled == int(scaled) else str(scaled)

    return re.sub(r"\d+(?:\.\d+)?", replace, text)


def build_day_keyboard(active_day: int) -> InlineKeyboardMarkup:
    """Строит клавиатуру с кнопками дней."""
    buttons = []
    row = []
    for i, day in enumerate(DAYS):
        label = f"{'✅ ' if i == active_day else ''}{day['day_label']}"
        row.append(InlineKeyboardButton(label, callback_data=f"day_{i}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🛒 Список продуктов на неделю", callback_data="grocery")])
    buttons.append([InlineKeyboardButton("🔄 Пересчитать", callback_data="restart")])
    return InlineKeyboardMarkup(buttons)


def normalize_name(name: str) -> tuple[str, str]:
    """
    Возвращает (нормализованное_имя, категория).
    Объединяет похожие ингредиенты под одним ключом.
    """
    n = name.lower().strip()
    n = re.sub(r"\(.*?\)", "", n).strip()   # убираем скобки
    n = re.sub(r"\s+", " ", n)

    # Яйца
    if re.search(r"яйц|яйцо", n):
        return "Яйца", "🥚 Яйца и молочное"
    # Творог
    if "творог" in n:
        return "Творог", "🥚 Яйца и молочное"
    # Молоко / вода или молоко
    if "молоко" in n or n == "вода или молоко":
        return "Молоко", "🥚 Яйца и молочное"
    # Масло сливочное
    if "масло сливочное" in n or "сливочное масло" in n:
        return "Масло сливочное", "🥚 Яйца и молочное"
    # Сметана
    if "сметан" in n:
        return "Сметана", "🥚 Яйца и молочное"
    # Рис
    if n.startswith("рис"):
        return "Рис", "🌾 Крупы и макароны"
    # Гречка
    if "гречк" in n:
        return "Гречка", "🌾 Крупы и макароны"
    # Овсянка
    if "овсян" in n:
        return "Овсяные хлопья", "🌾 Крупы и макароны"
    # Макароны
    if "макарон" in n:
        return "Макароны", "🌾 Крупы и макароны"
    # Хлеб (включая замоченный)
    if "хлеб" in n:
        return "Хлеб", "🍞 Хлеб"
    # Картофель
    if "картоф" in n or "картошк" in n:
        return "Картофель", "🥔 Овощи"
    # Лук
    if re.match(r"^лук", n):
        return "Лук репчатый", "🥔 Овощи"
    # Морковь
    if re.match(r"^морковь", n) or re.match(r"^морков", n):
        return "Морковь", "🥔 Овощи"
    # Капуста
    if "капуст" in n:
        return "Капуста", "🥔 Овощи"
    # Чеснок
    if "чеснок" in n:
        return "Чеснок", "🥔 Овощи"
    # Банан
    if "банан" in n:
        return "Бананы", "🫙 Прочее"
    # Томатная паста
    if "томатн" in n:
        return "Томатная паста", "🫙 Прочее"
    # Вода
    if n == "вода":
        return "Вода", "🫙 Прочее"

    return name.strip().capitalize(), "🫙 Прочее"


def build_grocery_list(cal: dict) -> str:
    """Список продуктов на 7 дней, сгруппированный по категориям."""
    scale = cal["target"] / BASE_CALS

    FIXED = {
        "🐔 Курица (если выбрал курицу)": [
            ("Куриное филе / грудка",    round(870  * scale), "г"),
            ("Куриное бедро без кости",  round(530  * scale), "г"),
            ("Куриный фарш",             round(200  * scale), "г"),
        ],
        "🥩 Говядина (если выбрал мясо)": [
            ("Говядина (нарезка)",       round(775  * scale), "г"),
            ("Говяжий фарш",             round(150  * scale), "г"),
        ],
        "🥚 Яйца и молочное": [
            ("Яйца",                     round(30   * scale), "шт"),
            ("Творог 5–9%",              round(700  * scale), "г"),
            ("Масло сливочное",          round(150  * scale), "г"),
            ("Сметана (по желанию)",     round(100  * scale), "г"),
        ],
        "🌾 Крупы и макароны": [
            ("Рис белый",                round(380  * scale), "г"),
            ("Гречка",                   round(440  * scale), "г"),
            ("Овсяные хлопья",           round(210  * scale), "г"),
            ("Макароны",                 round(160  * scale), "г"),
        ],
        "🥔 Овощи": [
            ("Картофель",                round(1400 * scale), "г"),
            ("Лук репчатый",             round(560  * scale), "г"),
            ("Морковь",                  round(480  * scale), "г"),
            ("Капуста белокочанная",     round(300  * scale), "г"),
            ("Чеснок",                   2,                    "головки"),
        ],
        "🍌 Фрукты": [
            ("Бананы",                   2,                    "шт"),
        ],
        "🍞 Хлеб и базовые продукты": [
            ("Хлеб",                     1,                    "буханка"),
            ("Томатная паста",           round(60   * scale), "г"),
            ("Масло растительное",       round(200  * scale), "мл"),
            ("Соль",                     1,                    "пачка"),
            ("Перец чёрный молотый",     1,                    "пачка"),
            ("Лавровый лист",            1,                    "пачка"),
            ("Паприка молотая",          1,                    "пачка"),
        ],
    }

    lines = [
        f"🛒 *Список продуктов на 7 дней*",
        f"_Норма: {cal['target']} ккал/день_\n",
    ]

    for category, items in FIXED.items():
        lines.append(f"*{category}*")
        for name, amount, unit in items:
            if unit == "г" and amount >= 1000:
                kg = round(amount / 1000, 1)
                lines.append(f"  • {name} — *{amount} г* (~{kg} кг)")
            else:
                lines.append(f"  • {name} — *{amount} {unit}*")
        lines.append("")

    lines.append("_⚠️ Покупай курицу ИЛИ говядину — не оба сразу_")
    lines.append("_💡 Специи и масло — смотри что есть дома_")
    return "\n".join(lines)


def format_day(day_index: int, cal: dict) -> str:
    """Форматирует день с рецептами в текст."""
    day = DAYS[day_index]
    scale = cal["target"] / BASE_CALS

    lines = [f"📅 *{day['day_label']}* — {cal['target']} ккал\n"]

    type_icons = {"Завтрак": "🌅", "Обед": "☀️", "Ужин": "🌙", "Перекус": "🍎"}

    for meal in day["meals"]:
        icon = type_icons.get(meal["type"], "🍽")
        meal_cal = round(meal["cal"] * scale)
        lines.append(f"{icon} *{meal['type']}* — {meal['name']} (~{meal_cal} ккал)")

        # Количество протеина
        if meal["protein"]:
            c_scaled = scale_amount(meal["protein"]["c"], scale)
            m_scaled = scale_amount(meal["protein"]["m"], scale)
            lines.append(f"   🐔 Курица: *{c_scaled}*")
            lines.append(f"   🥩 Мясо:   *{m_scaled}*")

        # Ингредиенты
        lines.append("   📋 *Ингредиенты:*")
        for ing in meal["ingredients"]:
            c = scale_amount(ing["c"], scale)
            m = scale_amount(ing["m"], scale)
            if c == m:
                lines.append(f"   • {ing['name']}: {c}")
            else:
                lines.append(f"   • {ing['name']}: 🐔{c} / 🥩{m}")

        # Приготовление
        lines.append("   👨‍🍳 *Приготовление:*")
        for idx, step in enumerate(meal["steps"], 1):
            lines.append(f"   {idx}. {step}")

        lines.append("")  # пустая строка между блюдами

    return "\n".join(lines)


# ─── Handlers ────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Привет! Я *NutriBot* — твой помощник по питанию.\n\n"
        "Я составлю персональный план питания на 7 дней с простыми рецептами "
        "из курицы и мяса. 🍗🥩\n\n"
        "Для начала — как тебя зовут?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_NAME


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if not name or len(name) > 30:
        await update.message.reply_text("Введи имя (не длиннее 30 символов).")
        return ASK_NAME
    context.user_data["name"] = name
    await update.message.reply_text(
        f"Приятно познакомиться, *{name}*! 😊\n\nСколько тебе лет?",
        parse_mode="Markdown",
    )
    return ASK_AGE


async def got_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        age = int(update.message.text.strip())
        assert 10 <= age <= 100
    except (ValueError, AssertionError):
        await update.message.reply_text("Введи возраст от 10 до 100.")
        return ASK_AGE
    context.user_data["age"] = age

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👨 Мужчина", callback_data="gender_male"),
            InlineKeyboardButton("👩 Женщина", callback_data="gender_female"),
        ]
    ])
    await update.message.reply_text("Укажи пол:", reply_markup=keyboard)
    return ASK_GENDER


async def got_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    gender = query.data.split("_")[1]
    context.user_data["gender"] = gender
    label = "Мужчина" if gender == "male" else "Женщина"
    await query.edit_message_text(f"Пол: *{label}* ✅\n\nВведи свой рост (в см):", parse_mode="Markdown")
    return ASK_HEIGHT


async def got_height(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        h = int(update.message.text.strip())
        assert 100 <= h <= 250
    except (ValueError, AssertionError):
        await update.message.reply_text("Введи рост от 100 до 250 см.")
        return ASK_HEIGHT
    context.user_data["height"] = h
    await update.message.reply_text("Хорошо! Теперь введи свой вес (в кг):")
    return ASK_WEIGHT


async def got_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        w = float(update.message.text.strip().replace(",", "."))
        assert 30 <= w <= 300
    except (ValueError, AssertionError):
        await update.message.reply_text("Введи вес от 30 до 300 кг.")
        return ASK_WEIGHT
    context.user_data["weight"] = w

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛋️ Почти не двигаюсь",       callback_data="act_1.2")],
        [InlineKeyboardButton("🚶 Лёгкая (1–3 трен/нед)",   callback_data="act_1.375")],
        [InlineKeyboardButton("🏃 Средняя (3–5 трен/нед)",  callback_data="act_1.55")],
        [InlineKeyboardButton("💪 Высокая (6–7 трен/нед)",  callback_data="act_1.725")],
        [InlineKeyboardButton("🔥 Очень высокая (физ. труд + спорт)", callback_data="act_1.9")],
    ])
    await update.message.reply_text("Уровень физической активности:", reply_markup=keyboard)
    return ASK_ACTIVITY


async def got_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    act = query.data.split("_")[1]
    context.user_data["activity"] = act

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Похудеть",        callback_data="goal_lose")],
        [InlineKeyboardButton("⚖️ Поддержать вес",  callback_data="goal_maintain")],
        [InlineKeyboardButton("⬆️ Набрать массу",   callback_data="goal_gain")],
    ])
    await query.edit_message_text("Отлично! Теперь выбери свою цель:", reply_markup=keyboard)
    return ASK_GOAL


async def got_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    goal = query.data.split("_")[1]
    context.user_data["goal"] = goal

    cal = calc_calories(context.user_data)
    context.user_data["cal"] = cal

    ud = context.user_data
    goal_labels = {"lose": "Похудение −500 ккал", "maintain": "Поддержание веса", "gain": "Набор массы +500 ккал"}

    summary = (
        f"✅ *Расчёт готов, {ud['name']}!*\n\n"
        f"📊 Твои показатели:\n"
        f"• Базовый обмен (BMR): *{cal['bmr']} ккал*\n"
        f"• Суточная норма (TDEE): *{cal['tdee']} ккал*\n"
        f"• Цель ({goal_labels[goal]}): *{cal['target']} ккал/день*\n\n"
        f"🥗 Макросы на день:\n"
        f"• Белки:    *{cal['protein']} г*\n"
        f"• Углеводы: *{cal['carbs']} г*\n"
        f"• Жиры:     *{cal['fat']} г*\n\n"
        f"Выбери день чтобы увидеть меню 👇"
    )

    await query.edit_message_text(
        summary,
        parse_mode="Markdown",
        reply_markup=build_day_keyboard(0),
    )
    return SHOW_PLAN


async def show_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "restart":
        context.user_data.clear()
        await query.edit_message_text(
            "👋 Начинаем заново! Как тебя зовут?",
            reply_markup=None,
        )
        return ASK_NAME

    if query.data == "grocery":
        cal = context.user_data.get("cal")
        if not cal:
            await query.answer("Сначала пройди опрос /start", show_alert=True)
            return SHOW_PLAN
        text = build_grocery_list(cal)
        back_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("← Назад к меню", callback_data="day_0")
        ]])
        if len(text) > 4000:
            parts = []
            current = ""
            for line in text.split("\n"):
                if len(current) + len(line) + 1 > 4000:
                    parts.append(current)
                    current = line + "\n"
                else:
                    current += line + "\n"
            if current:
                parts.append(current)
            await query.edit_message_text(parts[0], parse_mode="Markdown", reply_markup=None)
            for part in parts[1:-1]:
                await query.message.reply_text(part, parse_mode="Markdown")
            await query.message.reply_text(parts[-1], parse_mode="Markdown", reply_markup=back_kb)
        else:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_kb)
        return SHOW_PLAN

    day_index = int(query.data.split("_")[1])
    cal = context.user_data.get("cal")
    if not cal:
        await query.edit_message_text("Что-то пошло не так. Напиши /start")
        return ConversationHandler.END

    text = format_day(day_index, cal)

    # Telegram лимит 4096 символов — разбиваем если надо
    if len(text) > 4000:
        parts = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > 4000:
                parts.append(current)
                current = line + "\n"
            else:
                current += line + "\n"
        if current:
            parts.append(current)

        await query.edit_message_text(
            parts[0],
            parse_mode="Markdown",
            reply_markup=None,
        )
        for part in parts[1:-1]:
            await query.message.reply_text(part, parse_mode="Markdown")
        await query.message.reply_text(
            parts[-1],
            parse_mode="Markdown",
            reply_markup=build_day_keyboard(day_index),
        )
    else:
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=build_day_keyboard(day_index),
        )

    return SHOW_PLAN


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "До свидания! Напиши /start когда захочешь вернуться. 👋",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception:", exc_info=context.error)


# ─── Точка входа ─────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Переменная BOT_TOKEN не задана!")

    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
            ASK_AGE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, got_age)],
            ASK_GENDER:   [CallbackQueryHandler(got_gender, pattern=r"^gender_")],
            ASK_HEIGHT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_height)],
            ASK_WEIGHT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_weight)],
            ASK_ACTIVITY: [CallbackQueryHandler(got_activity, pattern=r"^act_")],
            ASK_GOAL:     [CallbackQueryHandler(got_goal, pattern=r"^goal_")],
            SHOW_PLAN:    [CallbackQueryHandler(show_day, pattern=r"^(day_\d+|restart|grocery)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_error_handler(error_handler)

    logger.info("Бот запущен...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
