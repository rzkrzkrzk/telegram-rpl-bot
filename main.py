import os
import logging
import sqlite3
import asyncio
import random
import time
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, BotCommand, BotCommandScopeDefault
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

# Настройка логов
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN не задан!")

DB_PATH = os.getenv("DB_PATH", "bot_data.db")
ADMIN_SESSION_MINUTES = 30

# Состояния
(WAITING_LOGIN, WAITING_PASSWORD, WAITING_CHANNEL_USERNAME, WAITING_CHAT_LINK,
 WAITING_REPLY_TEXT, WAITING_SUPPORT_MSG, WAITING_DUEL_SHOT, WAITING_GIF_UPLOAD) = range(8)

# Глобальные переменные для матчмейкинга "Дуэль Клюшек"
matchmaking_queue = {}  # {user_id: {chat_id, message_id, start_time, first_name}}
active_stick_matches = {}  # {match_id: data}

# ---------- БАЗА ДАННЫХ ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS source_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE, username TEXT, added_by INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS target_chats (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE, link TEXT, added_by INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS support_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, text TEXT, timestamp TEXT, answered INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, last_activity INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_config (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS duel_stats (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, total_shots INTEGER DEFAULT 0, goals INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS stick_stats (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, mmr INTEGER DEFAULT 1000, games INTEGER DEFAULT 0)''')
    for i in range(1, 4):
        c.execute('INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)', (f'gif_goal_{i}', ''))
        c.execute('INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)', (f'gif_save_{i}', ''))
    conn.commit()
    conn.close()

init_db()

def get_config(key):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT value FROM bot_config WHERE key = ?', (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else ''

def set_config(key, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()

def update_duel_stats(user_id, username, first_name, is_goal):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    goal_inc = 1 if is_goal else 0
    c.execute('''INSERT INTO duel_stats (user_id, username, first_name, total_shots, goals)
                 VALUES (?, ?, ?, 1, ?) ON CONFLICT(user_id) DO UPDATE SET
                 total_shots = total_shots + 1, goals = goals + ?''', (user_id, username, first_name, goal_inc, goal_inc))
    conn.commit()
    conn.close()

def update_stick_mmr(user_id, first_name, username, change):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO stick_stats (user_id, first_name, username, mmr, games)
                 VALUES (?, ?, ?, ?, 1) ON CONFLICT(user_id) DO UPDATE SET
                 mmr = mmr + ?, games = games + 1''', (user_id, first_name, username, 1000 + change, change))
    conn.commit()
    conn.close()

def is_admin(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT last_activity FROM admins WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        if (datetime.now().timestamp() - row[0]) < ADMIN_SESSION_MINUTES * 60:
            return True
    return False

def add_admin(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO admins (user_id, last_activity) VALUES (?, ?)', (user_id, int(datetime.now().timestamp())))
    conn.commit()
    conn.close()

def update_admin_activity(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE admins SET last_activity = ? WHERE user_id = ?', (int(datetime.now().timestamp()), user_id))
    conn.commit()
    conn.close()

def remove_admin(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM admins WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def reset_all_ratings():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM duel_stats")
    conn.commit()
    conn.close()

# ---------- КЛАВИАТУРЫ ----------
def main_menu_keyboard():
    return ReplyKeyboardMarkup([["🏠 Главное меню"]], resize_keyboard=True)

def admin_menu_keyboard():
    return ReplyKeyboardMarkup([
        ["➕ Добавить каналы", "➕ Добавить чаты"],
        ["📩 Проверить поддержку", "⚙️ Настройки"],
        ["🎮 Настройки игр", "🚪 Выйти"]
    ], resize_keyboard=True)

def welcome_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Наш Discord", callback_data="discord")],
        [InlineKeyboardButton("🌐 Наш Сайт", callback_data="website")],
        [InlineKeyboardButton("🆘 Поддержка", callback_data="support")],
        [InlineKeyboardButton("🏒 Дуэль Буллитов (ИИ)", callback_data="duel")],
        [InlineKeyboardButton("⚔️ Дуэль Клюшек (PvP)", callback_data="stick_search")]
    ])

def duel_shot_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➡️ Правый верхний", callback_data="shot_right")],
        [InlineKeyboardButton("⬅️ Левый верхний", callback_data="shot_left")],
        [InlineKeyboardButton("⬇️ Пятак", callback_data="shot_five")],
        [InlineKeyboardButton("⬇️ Низ", callback_data="shot_low")]
    ])

# ---------- ЛОГИКА ДУЭЛИ КЛЮШЕК ----------
async def regrpl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in matchmaking_queue:
        await update.message.reply_text("❌ Вы уже в поиске!")
        return

    for uid, data in matchmaking_queue.items():
        if uid != user.id:
            matchmaking_queue.pop(uid)
            await start_stick_match(context, uid, user.id, update.effective_chat.id)
            return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Принять", callback_data=f"st_accept_{user.id}")],
        [InlineKeyboardButton("🤖 С ботом", callback_data=f"st_ai_{user.id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"st_cancel_{user.id}")]
    ])
    msg = await update.message.reply_text(f"🏒 {user.first_name} ищет соперника для Дуэли Клюшек...\nОжидание: 45 сек.", reply_markup=kb)
    matchmaking_queue[user.id] = {"chat_id": update.effective_chat.id, "message_id": msg.message_id, "start_time": time.time(), "name": user.first_name}
    asyncio.create_task(matchmaking_timeout(user.id, context))

async def matchmaking_timeout(user_id, context):
    await asyncio.sleep(45)
    if user_id in matchmaking_queue:
        data = matchmaking_queue.pop(user_id)
        await context.bot.edit_message_text(chat_id=data["chat_id"], message_id=data["message_id"], text="⏰ Соперник не найден. Игра с ботом!")
        await start_stick_match(context, user_id, 0, data["chat_id"])

async def start_stick_match(context, p1_id, p2_id, chat_id):
    match_id = f"{p1_id}_{p2_id}_{int(time.time())}"
    p1_name = (await context.bot.get_chat(p1_id)).first_name
    p2_name = "ИИ Вратарь" if p2_id == 0 else (await context.bot.get_chat(p2_id)).first_name

    active_stick_matches[match_id] = {
        "p1": {"id": p1_id, "name": p1_name, "goals": 0},
        "p2": {"id": p2_id, "name": p2_name, "goals": 0},
        "round": 1, "turn": p1_id, "phase": "shoot", "last_shot": None
    }

    await context.bot.send_message(chat_id, f"🏒 МАТЧ НАЧАТ!\nНападающий: {p1_name}\nВратарь: {p2_name}\n\n{p1_name}, выбирай куда бросать:",
                                   reply_markup=stick_kb("shoot", match_id))

def stick_kb(type, mid):
    if type == "shoot":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Правая девятка", callback_data=f"st_s_R_{mid}")],
            [InlineKeyboardButton("Левая девятка", callback_data=f"st_s_L_{mid}")],
            [InlineKeyboardButton("Домик", callback_data=f"st_s_D_{mid}")]
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Девятки (Л+П)", callback_data=f"st_d_C_{mid}")],
        [InlineKeyboardButton("Домик", callback_data=f"st_d_D_{mid}")]
    ])

async def stick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data.split("_")

    if data[1] == "accept":
        owner = int(data[2])
        if user.id == owner:
            return await query.answer("Это ваш поиск!")
        if owner in matchmaking_queue:
            matchmaking_queue.pop(owner)
            await query.message.delete()
            await start_stick_match(context, owner, user.id, query.message.chat_id)
        return
    if data[1] == "ai":
        owner = int(data[2])
        if user.id != owner:
            return await query.answer("Только создатель!")
        matchmaking_queue.pop(owner, None)
        await query.message.delete()
        await start_stick_match(context, owner, 0, query.message.chat_id)
        return
    if data[1] == "cancel":
        owner = int(data[2])
        if user.id != owner:
            return await query.answer("Только создатель!")
        matchmaking_queue.pop(owner, None)
        await query.edit_message_text("❌ Отменено.")
        return

    action, choice, mid = data[1], data[2], data[3]
    if mid not in active_stick_matches:
        return await query.answer("Матч окончен.")
    m = active_stick_matches[mid]

    if action == "s":  # Shoot
        if user.id != m["turn"]:
            return await query.answer("Не ваш ход!")
        m["last_shot"] = choice
        m["phase"] = "defend"
        defender = m["p2"] if m["turn"] == m["p1"]["id"] else m["p1"]
        if defender["id"] == 0:  # AI
            await process_stick_round(query, context, mid, random.choice(["C", "D"]))
        else:
            await query.edit_message_text(f"Вратарь: {defender['name']}, Нападающий: {user.first_name}\nСейчас {defender['name']} выбирает защиту...", reply_markup=stick_kb("def", mid))
    elif action == "d":  # Defend
        defender = m["p2"] if m["turn"] == m["p1"]["id"] else m["p1"]
        if user.id != defender["id"]:
            return await query.answer("Сейчас не ваша очередь отбивать!")
        await process_stick_round(query, context, mid, choice)

async def process_stick_round(query, context, mid, def_choice):
    m = active_stick_matches[mid]
    shot = m["last_shot"]
    attacker = m["p1"] if m["turn"] == m["p1"]["id"] else m["p2"]

    scored = True
    if def_choice == "C" and shot in ["R", "L"]:
        scored = False
    if def_choice == "D" and shot == "D":
        scored = False

    if scored:
        attacker["goals"] += 1
    res = "✅ ГОЛ!" if scored else "🧤 СЕЙВ!"

    if m["turn"] == m["p1"]["id"]:
        m["turn"] = m["p2"]["id"]
        if m["p2"]["id"] == 0:  # AI Turn
            m["last_shot"] = random.choice(["R", "L", "D"])
            await query.message.reply_text(f"{res}\nТеперь вы вратарь! ИИ бросает...", reply_markup=stick_kb("def", mid))
        else:
            await query.message.reply_text(f"{res}\n🔄 Смена сторон! Теперь бросает {m['p2']['name']}.", reply_markup=stick_kb("shoot", mid))
    else:
        if m["round"] < 3:
            m["round"] += 1
            m["turn"] = m["p1"]["id"]
            await query.message.reply_text(f"{res}\n🏁 Раунд {m['round']}! Бросает {m['p1']['name']}.", reply_markup=stick_kb("shoot", mid))
        else:
            p1, p2 = m["p1"], m["p2"]
            win_text = f"🏁 МАТЧ ОКОНЧЕН!\n{p1['name']}: {p1['goals']}\n{p2['name']}: {p2['goals']}\n\n"
            if p1["goals"] > p2["goals"]:
                win_text += f"🏆 Победитель: {p1['name']}"
                if p2["id"] != 0:
                    update_stick_mmr(p1["id"], p1["name"], "", 25)
                    update_stick_mmr(p2["id"], p2["name"], "", -20)
            elif p2["goals"] > p1["goals"]:
                win_text += f"🏆 Победитель: {p2['name']}"
                if p2["id"] != 0:
                    update_stick_mmr(p2["id"], p2["name"], "", 25)
                    update_stick_mmr(p1["id"], p1["name"], "", -20)
            else:
                win_text += "🤝 Ничья!"
            active_stick_matches.pop(mid)
            await query.message.reply_text(win_text)

# --- MMR Команды ---
async def mymmr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    res = conn.execute("SELECT mmr FROM stick_stats WHERE user_id = ?", (update.effective_user.id,)).fetchone()
    conn.close()
    val = res[0] if res else 1000
    await update.message.reply_text(f"🎖 Ваш MMR: {val}")

async def ratingmmr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT first_name, mmr FROM stick_stats ORDER BY mmr DESC LIMIT 10").fetchall()
    conn.close()
    if not rows:
        return await update.message.reply_text("Рейтинг пуст.")
    txt = "🥇 ТОП-10 MMR Клюшек:\n" + "\n".join([f"{i+1}. {r[0]} - {r[1]}" for i, r in enumerate(rows)])
    await update.message.reply_text(txt)

# ---------- ЛОГИКА ДУЭЛИ БУЛЛИТОВ ----------
async def start_duel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"🏒 {user.first_name}, выбери зону броска:", reply_markup=duel_shot_keyboard())
    return WAITING_DUEL_SHOT

async def duel_shot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    shot_zone = query.data
    user = query.from_user

    goalie_choice = random.choice(["shot_left", "shot_right", "shot_five", "shot_low"])
    scored = random.random() < 0.35 if shot_zone != goalie_choice else False
    update_duel_stats(user.id, user.username, user.first_name, scored)

    idx = random.randint(1, 3)
    if scored:
        gif = get_config(f'gif_goal_{idx}') or "https://media.giphy.com/media/3o7aTskHEUdgCQAXde/giphy.gif"
        msg = "⚡️ ГОЛ!"
    else:
        gif = get_config(f'gif_save_{idx}') or "https://media.giphy.com/media/3o6Ztq5cG6GZj5F9uo/giphy.gif"
        msg = "🧤 СЕЙВ!"

    await query.edit_message_text(f"{msg}\nВратарь выбрал: {goalie_choice}")
    try:
        await query.message.reply_animation(gif)
    except:
        pass
    return ConversationHandler.END

# ---------- АДМИНКА ----------
async def show_game_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏒 Буллиты: GIF", callback_data="adm_bul_gifs"), InlineKeyboardButton("🧹 Буллиты: Reset", callback_data="adm_bul_reset")],
        [InlineKeyboardButton("🧹 Клюшки: Reset", callback_data="adm_stick_reset")]
    ])
    await update.message.reply_text("🎮 Настройки игр:", reply_markup=kb)

async def admin_callback_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "adm_bul_gifs":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Goal 1", callback_data="setg_goal_1"), InlineKeyboardButton("Goal 2", callback_data="setg_goal_2"), InlineKeyboardButton("Goal 3", callback_data="setg_goal_3")],
            [InlineKeyboardButton("Save 1", callback_data="setg_save_1"), InlineKeyboardButton("Save 2", callback_data="setg_save_2"), InlineKeyboardButton("Save 3", callback_data="setg_save_3")]
        ])
        await query.edit_message_text("Выберите слот для GIF:", reply_markup=kb)
    elif data == "adm_bul_reset":
        reset_all_ratings()
        await query.edit_message_text("✅ Рейтинг буллитов обнулен!")
    elif data == "adm_stick_reset":
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM stick_stats")
        conn.commit()
        conn.close()
        await query.edit_message_text("✅ Рейтинг клюшек обнулен!")

async def set_gif_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data["gif_key"] = query.data.replace("setg_", "gif_")
    await query.edit_message_text(f"📤 Отправьте GIF для {context.user_data['gif_key']}:")
    return WAITING_GIF_UPLOAD

async def receive_gif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = update.message.animation.file_id if update.message.animation else None
    if file_id:
        set_config(context.user_data.get("gif_key"), file_id)
        await update.message.reply_text("✅ Сохранено!")
        return ConversationHandler.END
    return WAITING_GIF_UPLOAD

# ---------- ОСНОВНЫЕ КОМАНДЫ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню с инлайн-кнопками."""
    await update.message.reply_text(
        "Добро пожаловать! Выберите действие:",
        reply_markup=welcome_inline_keyboard()
    )

async def rating_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Топ буллитов."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT first_name, goals, total_shots FROM duel_stats ORDER BY goals DESC LIMIT 10").fetchall()
    conn.close()
    if not rows:
        return await update.message.reply_text("Рейтинг буллитов пуст.")
    txt = "🥇 ТОП-10 Буллитов:\n" + "\n".join([f"{i+1}. {r[0]} - {r[1]} голов (всего {r[2]} бросков)" for i, r in enumerate(rows)])
    await update.message.reply_text(txt)

async def adminkarpl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if is_admin(update.effective_user.id):
        await update.message.reply_text("✅ Админ-панель", reply_markup=admin_menu_keyboard())
        return
    await update.message.reply_text("🔑 Логин:")
    return WAITING_LOGIN

async def wait_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["login"] = update.message.text
    await update.message.reply_text("🔒 Пароль:")
    return WAITING_PASSWORD

async def wait_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    login, pwd = context.user_data.get("login"), update.message.text
    if login == "goyda1488" and pwd == "goydarpl":  # Упрощено для примера
        add_admin(update.effective_user.id)
        await update.message.reply_text("✅ Вход!", reply_markup=admin_menu_keyboard())
        return ConversationHandler.END
    await update.message.reply_text("❌ Ошибка")
    return WAITING_PASSWORD

async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = update.message.text
    if text == "🎮 Настройки игр":
        await show_game_settings(update, context)
    elif text == "🏠 Главное меню":
        await start(update, context)
    elif text == "🚪 Выйти":
        remove_admin(update.effective_user.id)
        await update.message.reply_text("🚪 Вышли", reply_markup=main_menu_keyboard())

# ---------- ОБРАБОТЧИКИ INLINE КНОПОК ----------
async def inline_callback_duel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Дуэль Буллитов' – запускает игру."""
    query = update.callback_query
    await query.answer()
    user = query.from_user
    await query.message.reply_text(f"🏒 {user.first_name}, выбери зону броска:", reply_markup=duel_shot_keyboard())
    return WAITING_DUEL_SHOT

async def inline_callback_general(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик остальных кнопок: Discord, сайт, поддержка, поиск дуэли клюшек."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "discord":
        await query.edit_message_text(
            "Наш Discord: https://discord.gg/example",
            reply_markup=welcome_inline_keyboard()
        )
    elif data == "website":
        await query.edit_message_text(
            "Наш сайт: https://example.com",
            reply_markup=welcome_inline_keyboard()
        )
    elif data == "support":
        await query.edit_message_text(
            "Напишите ваше сообщение для поддержки. (Функция в разработке)",
            reply_markup=welcome_inline_keyboard()
        )
    elif data == "stick_search":
        user = query.from_user
        if user.id in matchmaking_queue:
            await query.message.reply_text("❌ Вы уже в поиске!")
            return

        for uid, _ in list(matchmaking_queue.items()):
            if uid != user.id:
                matchmaking_queue.pop(uid)
                await start_stick_match(context, uid, user.id, query.message.chat_id)
                return

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Принять", callback_data=f"st_accept_{user.id}")],
            [InlineKeyboardButton("🤖 С ботом", callback_data=f"st_ai_{user.id}")],
            [InlineKeyboardButton("❌ Отмена", callback_data=f"st_cancel_{user.id}")]
        ])
        msg = await query.message.reply_text(
            f"🏒 {user.first_name} ищет соперника для Дуэли Клюшек...\nОжидание: 45 сек.",
            reply_markup=kb
        )
        matchmaking_queue[user.id] = {
            "chat_id": query.message.chat_id,
            "message_id": msg.message_id,
            "start_time": time.time(),
            "name": user.first_name
        }
        asyncio.create_task(matchmaking_timeout(user.id, context))
    else:
        await query.edit_message_text("Неизвестная команда.")

async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("duelrpl", "Буллиты с ИИ"),
        BotCommand("regrpl", "Дуэль Клюшек (PvP)"),
        BotCommand("mymmr", "Ваш MMR"),
        BotCommand("ratingmmr", "Топ MMR"),
        BotCommand("rating", "Топ Буллитов")
    ])

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # --- Игры ---
    app.add_handler(CommandHandler("regrpl", regrpl))
    app.add_handler(CommandHandler("mymmr", mymmr))
    app.add_handler(CommandHandler("ratingmmr", ratingmmr))
    app.add_handler(CallbackQueryHandler(stick_callback, pattern="^(st_|stick_)"))

    # Дуэль Буллитов (ConversationHandler)
    app.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("duelrpl", start_duel_command),
            CallbackQueryHandler(inline_callback_duel, pattern="^duel$")
        ],
        states={
            WAITING_DUEL_SHOT: [CallbackQueryHandler(duel_shot, pattern="^shot_")]
        },
        fallbacks=[CommandHandler("cancel", start)]
    ))

    # --- Админ ---
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("adminkarpl", adminkarpl)],
        states={
            WAITING_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, wait_login)],
            WAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, wait_password)]
        },
        fallbacks=[CommandHandler("cancel", start)]
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(set_gif_start, pattern="^setg_")],
        states={
            WAITING_GIF_UPLOAD: [MessageHandler(filters.ANIMATION, receive_gif)]
        },
        fallbacks=[CommandHandler("cancel", start)]
    ))

    app.add_handler(CallbackQueryHandler(admin_callback_games, pattern="^adm_"))

    # --- Общие команды ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rating", rating_command))

    # Обработка кнопок Discord, сайт, поддержка, поиск дуэли клюшек
    app.add_handler(CallbackQueryHandler(inline_callback_general, pattern="^(discord|website|support|stick_search)$"))

    # Кнопки админ-меню (текстовые)
    app.add_handler(MessageHandler(filters.Regex("^(🎮 Настройки игр|🏠 Главное меню|🚪 Выйти)$"), admin_buttons))

    app.run_polling()

if __name__ == "__main__":
    main()
