import os
import logging
import sqlite3
import asyncio
import random
import uuid
import time
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    BotCommand,
    BotCommandScopeDefault,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN не задан!")

DB_PATH = os.getenv("DB_PATH", "bot_data.db")
ADMIN_SESSION_MINUTES = 30

# Состояния ConversationHandler
(WAITING_LOGIN, WAITING_PASSWORD, WAITING_CHANNEL_USERNAME, 
 WAITING_CHAT_LINK, WAITING_REPLY_TEXT, WAITING_SUPPORT_MSG) = range(6)
WAITING_DUEL_SHOT = 10
WAITING_GIF_UPLOAD = 11
WAITING_GAME_SETTINGS = 12

# Настройки «Дуэли Клюшек»
STICK_DUEL_SEARCH_SECONDS = 45
STICK_DUEL_TOTAL_TURNS = 6
INITIAL_MMR = 1000

# Глобальные переменные
stick_duel_searches = {}  
stick_duel_games = {}     
stick_duel_by_user = {}   
stick_duel_lock = asyncio.Lock()
bullet_cooldowns = {} # user_id -> timestamp

SHOT_LABELS = {
    "right": "Правая девятка",
    "left": "Левая девятка",
    "home": "Домик",
}
SAVE_LABELS = {
    "nines": "Девятки",
    "home": "Домик",
}

# ---------- БД ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS source_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE,
        username TEXT, added_by INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS target_chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE,
        link TEXT, added_by INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS support_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        username TEXT, text TEXT, timestamp TEXT, answered INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY, last_activity INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_config (
        key TEXT PRIMARY KEY, value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS duel_stats (
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        total_shots INTEGER DEFAULT 0, goals INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS mmr_stats (
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        mmr INTEGER DEFAULT 1000, games_played INTEGER DEFAULT 0
    )''')
    for i in range(1, 4):
        c.execute('INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)', (f'gif_goal_{i}', ''))
        c.execute('INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)', (f'gif_save_{i}', ''))
    conn.commit()
    conn.close()

init_db()

# ---------- MMR Логика ----------
def calculate_dynamic_mmr(player_mmr, won):
    """
    Разброс: 1000 ммр = +/- 15.
    Если ммр ниже 1000, плюс больше, минус меньше.
    """
    base = 15
    diff = 1000 - player_mmr
    # За каждые 50 ммр ниже 1000 добавляем 2 к выигрышу и вычитаем 1 из проигрыша
    modifier = diff // 50 
    
    if won:
        change = base + (modifier * 2 if modifier > 0 else modifier)
    else:
        change = -(base - (modifier if modifier > 0 else modifier // 2))
    
    return int(max(5, change)) if won else int(min(-5, change))

def update_stick_duel_mmr_stats(winner_data, loser_data):
    if winner_data["is_bot"] or loser_data["is_bot"]:
        return

    w_info = get_mmr_user(winner_data["id"], winner_data.get("username"), winner_data["name"])
    l_info = get_mmr_user(loser_data["id"], loser_data.get("username"), loser_data["name"])

    w_change = calculate_dynamic_mmr(w_info["mmr"], True)
    l_change = calculate_dynamic_mmr(l_info["mmr"], False)

    update_mmr(winner_data["id"], winner_data.get("username"), winner_data["name"], w_change)
    update_mmr(loser_data["id"], loser_data.get("username"), loser_data["name"], l_change)

# ---------- Функции БД ----------
def update_duel_stats(user_id, username, first_name, is_goal):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    goal_inc = 1 if is_goal else 0
    c.execute('''INSERT INTO duel_stats (user_id, username, first_name, total_shots, goals)
                 VALUES (?, ?, ?, 1, ?)
                 ON CONFLICT(user_id) DO UPDATE SET
                 username = excluded.username, first_name = excluded.first_name,
                 total_shots = total_shots + 1, goals = goals + ?''',
              (user_id, username, first_name, goal_inc, goal_inc))
    conn.commit()
    conn.close()

def get_mmr_user(user_id, username, first_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT mmr, games_played FROM mmr_stats WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    if not row:
        c.execute('INSERT INTO mmr_stats (user_id, username, first_name, mmr, games_played) VALUES (?, ?, ?, 1000, 0)',
                  (user_id, username, first_name))
        conn.commit()
        mmr, games = 1000, 0
    else:
        mmr, games = row
    conn.close()
    return {"mmr": mmr, "games_played": games}

def update_mmr(user_id, username, first_name, mmr_change):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''UPDATE mmr_stats SET mmr = mmr + ?, games_played = games_played + 1,
                 username = ?, first_name = ? WHERE user_id = ?''', 
              (mmr_change, username, first_name, user_id))
    conn.commit()
    conn.close()

def get_top_rating():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT first_name, username, goals, total_shots,
                 (CAST(goals AS FLOAT) / total_shots * 100) AS percent
                 FROM duel_stats WHERE total_shots >= 7
                 ORDER BY percent DESC LIMIT 10''')
    rows = c.fetchall()
    conn.close()
    return rows

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

def get_all_gifs(prefix):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    gifs = []
    for i in range(1, 4):
        c.execute('SELECT value FROM bot_config WHERE key = ?', (f'{prefix}_{i}',))
        row = c.fetchone()
        if row and row[0]: gifs.append(row[0])
    conn.close()
    return gifs if gifs else None

def is_admin(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT last_activity FROM admins WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        if (datetime.now().timestamp() - row[0]) < ADMIN_SESSION_MINUTES * 60: return True
    return False

def add_admin(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO admins (user_id, last_activity) VALUES (?, ?)',
              (user_id, int(datetime.now().timestamp())))
    conn.commit()
    conn.close()

def remove_admin(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM admins WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def check_credentials(login, password):
    credentials = {"goyda1488": "goydarpl", "rzk1488": "rzksigma"}
    return credentials.get(login) == password

# ---------- Клавиатуры ----------
def main_menu_keyboard():
    return ReplyKeyboardMarkup([["🏠 Главное меню"], ["👤 Мой Профиль"]], resize_keyboard=True)

def welcome_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏒 Дуэль Буллитов", callback_data="duel"), 
         InlineKeyboardButton("⚔️ Дуэль Клюшек", callback_data="regrpl_btn")],
        [InlineKeyboardButton("💬 Наш Discord", callback_data="discord")],
        [InlineKeyboardButton("🌐 Наш Сайт", callback_data="website")],
        [InlineKeyboardButton("🆘 Поддержка", callback_data="support")],
    ])

def duel_shot_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🥅 Лев. девятка", callback_data="shot_left"), InlineKeyboardButton("🥅 Пр. девятка", callback_data="shot_right")],
        [InlineKeyboardButton("🧤 Домик", callback_data="shot_five"), InlineKeyboardButton("🥅 Низ в угол", callback_data="shot_low")],
    ])

def stick_search_keyboard(owner_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Принять", callback_data=f"rpl_accept:{owner_id}")],
        [InlineKeyboardButton("🤖 С ботом", callback_data=f"rpl_ai:{owner_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"rpl_cancel:{owner_id}")],
    ])

def stick_shot_keyboard(game_id, turn):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Правая девятка", callback_data=f"rpl_shot:{game_id}:{turn}:right")],
        [InlineKeyboardButton("Левая девятка", callback_data=f"rpl_shot:{game_id}:{turn}:left")],
        [InlineKeyboardButton("Домик", callback_data=f"rpl_shot:{game_id}:{turn}:home")],
    ])

def stick_save_keyboard(game_id, turn):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Девятки", callback_data=f"rpl_save:{game_id}:{turn}:nines")],
        [InlineKeyboardButton("Домик", callback_data=f"rpl_save:{game_id}:{turn}:home")],
    ])

# ---------- Общие обработчики ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Добро пожаловать в Russian Puck League!", reply_markup=main_menu_keyboard())
    await update.message.reply_text("📌 Выберите раздел:", reply_markup=welcome_inline_keyboard())

async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT goals, total_shots FROM duel_stats WHERE user_id = ?', (user.id,))
    bullet = c.fetchone()
    c.execute('SELECT mmr, games_played FROM mmr_stats WHERE user_id = ?', (user.id,))
    stick = c.fetchone()
    conn.close()

    text = f"👤 **Профиль игрока {user.first_name}**\n\n"
    
    if bullet and bullet[1] >= 7:
        perc = (bullet[0] / bullet[1]) * 100
        text += f"🏒 **Дуэль Буллитов:**\n└ Рейтинг: {perc:.1f}% ({bullet[0]}/{bullet[1]} гол.)\n\n"
    else:
        text += f"🏒 **Дуэль Буллитов:**\n└ *Недостаточно матчей для рейтинга (нужно 7)*\n\n"

    if stick and stick[1] > 0:
        mmr = stick[0]
        rank = "Новичок"
        if mmr > 1200: rank = "Профи"
        if mmr > 1500: rank = "Легенда RPL"
        text += f"⚔️ **Дуэль Клюшек:**\n└ MMR: {mmr}\n└ Игр: {stick[1]}\n└ Звание: {rank}\n\n"
    else:
        text += f"⚔️ **Дуэль Клюшек:**\n└ *Еще не играл*\n\n"

    text += "🔥 Удачи на льду!"
    await update.message.reply_text(text, parse_mode="Markdown")

async def inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "regrpl_btn":
        await regrpl_command(update, context)
    elif query.data == "duel":
        await start_duel_command(update, context)
    elif query.data == "support":
        context.user_data["in_conversation_support"] = True
        await query.edit_message_text("✍️ Напишите сообщение поддержке или /cancel")
        return WAITING_SUPPORT_MSG
    # ... остальные кнопки (discord, website) ...

# ---------- Дуэль Буллитов (с КД) ----------
async def start_duel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = time.time()
    
    if user.id in bullet_cooldowns:
        remains = int(300 - (now - bullet_cooldowns[user.id]))
        if remains > 0:
            msg = f"⏳ Подожди еще {remains // 60} мин. {remains % 60} сек. перед следующей дуэлью!"
            if update.callback_query:
                await update.callback_query.message.reply_text(msg)
            else:
                await update.message.reply_text(msg)
            return ConversationHandler.END

    bullet_cooldowns[user.id] = now
    text = "🏒 Дуэль Буллитов! Выбери зону для броска:"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=duel_shot_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=duel_shot_keyboard())
    return WAITING_DUEL_SHOT

async def duel_shot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    shot_zone = query.data
    
    goalie_zones = ["shot_left", "shot_right", "shot_five", "shot_low"]
    goalie_choice = random.choice(goalie_zones)
    scored = random.random() < 0.35 if shot_zone != goalie_choice else False

    update_duel_stats(user.id, user.username, user.first_name, scored)

    res = "🚨 **ГОЛ!**" if scored else "🧤 **СЕЙВ!**"
    gifs = get_all_gifs('gif_goal' if scored else 'gif_save')
    gif = random.choice(gifs) if gifs else None

    await query.edit_message_text(f"{res}\nТвой бросок: {shot_zone}\nВратарь: {goalie_choice}")
    if gif:
        try: await query.message.reply_animation(gif)
        except: pass
    
    await query.message.reply_text("📌 Выберите раздел:", reply_markup=welcome_inline_keyboard())
    return ConversationHandler.END

# ---------- Дуэль Клюшек (с Анти-спамом и сложностью) ----------
def stick_player(user, chat_id):
    return {"id": user.id, "name": user.first_name or "Игрок", "username": user.username, "chat_id": chat_id, "is_bot": False, "last_msg": None}

def stick_ai_player():
    return {"id": 0, "name": "Бот-Вратарь", "username": None, "chat_id": None, "is_bot": True}

async def send_to_stick_game(bot, game, text, reply_markup=None):
    """Удаляет старое и пишет новое сообщение для каждого игрока."""
    for player in game["players"]:
        if player["is_bot"] or not player["chat_id"]: continue
        
        # Удаляем старое
        if player["last_msg"]:
            try: await bot.delete_message(player["chat_id"], player["last_msg"])
            except: pass
            
        # Пишем новое
        try:
            msg = await bot.send_message(player["chat_id"], text, reply_markup=reply_markup)
            player["last_msg"] = msg.message_id
        except: pass

async def resolve_stick_shot_locked(bot, game, save_choice):
    turn = game["turn"]
    att_idx = turn % 2
    goal_idx = 1 - att_idx
    att, goalie = game["players"][att_idx], game["players"][goal_idx]
    shot = game["shot"]

    saved = (save_choice == "nines" and shot in ("right", "left")) or (save_choice == "home" and shot == "home")
    is_goal = not saved
    if is_goal: game["score"][att_idx] += 1

    res = "🚨 ГОЛ!" if is_goal else "🧤 СЕЙВ!"
    score_text = f"Счёт: {game['players'][0]['name']} {game['score'][0]}:{game['score'][1]} {game['players'][1]['name']}"
    
    await send_to_stick_game(bot, game, f"{res}\n{att['name']} бил в {SHOT_LABELS[shot]}\n{goalie['name']} выбрал {SAVE_LABELS[save_choice]}\n\n{score_text}")
    
    game["turn"] += 1
    game["shot"] = None
    if game["turn"] >= STICK_DUEL_TOTAL_TURNS:
        await finish_stick_game_locked(bot, game)
    else:
        await asyncio.sleep(2)
        await send_stick_turn_locked(bot, game)

async def send_stick_turn_locked(bot, game):
    turn = game["turn"]
    att_idx = turn % 2
    goal_idx = 1 - att_idx
    att, goalie = game["players"][att_idx], game["players"][goal_idx]
    
    header = f"🏒 Раунд {turn//2 + 1}/3\nНападает: {att['name']}\nВратарь: {goalie['name']}\n"

    if att["is_bot"]:
        game["shot"] = random.choice(list(SHOT_LABELS))
        game["stage"] = "save"
        await send_to_stick_game(bot, game, header + "Бот сделал выбор! Вратарь, твой выход:", reply_markup=stick_save_keyboard(game["id"], turn))
    else:
        game["stage"] = "shot"
        await send_to_stick_game(bot, game, header + "Нападающий, выбирай зону:", reply_markup=stick_shot_keyboard(game["id"], turn))

async def finish_stick_game_locked(bot, game):
    p1, p2 = game["players"]
    s1, s2 = game["score"]
    
    win_text = "🤝 Ничья!"
    if s1 > s2:
        win_text = f"🏆 Победил {p1['name']}!"
        update_stick_duel_mmr_stats(p1, p2)
    elif s2 > s1:
        win_text = f"🏆 Победил {p2['name']}!"
        update_stick_duel_mmr_stats(p2, p1)

    await send_to_stick_game(bot, game, f"🏁 Игра окончена!\nСчёт {s1}:{s2}\n{win_text}\n\nMMR обновлен в профиле.")
    for p in game["players"]:
        if not p["is_bot"]: stick_duel_by_user.pop(p["id"], None)
    stick_duel_games.pop(game["id"], None)

async def regrpl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    async with stick_duel_lock:
        if user.id in stick_duel_by_user:
            await update.effective_message.reply_text("⚠️ Ты уже в игре!")
            return
        
        opp_id = next(iter(stick_duel_searches), None)
        if opp_id and opp_id != user.id:
            search = stick_duel_searches.pop(opp_id)
            game_id = uuid.uuid4().hex[:8]
            game = {"id": game_id, "players": [search["player"], stick_player(user, chat_id)], "turn": 0, "score": [0,0], "stage": "start"}
            stick_duel_games[game_id] = game
            stick_duel_by_user[user.id] = game_id
            stick_duel_by_user[opp_id] = game_id
            await send_stick_turn_locked(context.bot, game)
        else:
            msg = await update.effective_message.reply_text("🔎 Поиск соперника...", reply_markup=stick_search_keyboard(user.id))
            stick_duel_searches[user.id] = {"player": stick_player(user, chat_id), "msg_id": msg.message_id}

async def stick_duel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data.split(":")
    
    async with stick_duel_lock:
        if data[0] == "rpl_ai":
            owner = int(data[1])
            if user.id != owner: return
            search = stick_duel_searches.pop(user.id, None)
            game_id = uuid.uuid4().hex[:8]
            game = {"id": game_id, "players": [search["player"], stick_ai_player()], "turn": 0, "score": [0,0], "stage": "start"}
            stick_duel_games[game_id] = game
            stick_duel_by_user[user.id] = game_id
            await send_stick_turn_locked(context.bot, game)
            return

        # Игровая логика (броски/сейвы)
        action, g_id, turn, choice = data[0], data[1], int(data[2]), data[3]
        game = stick_duel_games.get(g_id)
        if not game or game["turn"] != turn: return

        att_idx = turn % 2
        goal_idx = 1 - att_idx

        if action == "rpl_shot" and user.id == game["players"][att_idx]["id"]:
            game["shot"] = choice
            game["stage"] = "save"
            
            # Повышенная сложность бота
            if game["players"][goal_idx]["is_bot"]:
                # 60% шанс, что бот выберет правильный сейв
                if random.random() < 0.60:
                    ai_choice = "home" if choice == "home" else "nines"
                else:
                    ai_choice = random.choice(list(SAVE_LABELS.keys()))
                await resolve_stick_shot_locked(context.bot, game, ai_choice)
            else:
                await send_stick_turn_locked(context.bot, game)
        
        elif action == "rpl_save" and user.id == game["players"][goal_idx]["id"]:
            await resolve_stick_shot_locked(context.bot, game, choice)

# ---------- Настройка и запуск ----------
def main():
    app = Application.builder().token(TOKEN).build()

    # Основные команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_handler))
    app.add_handler(MessageHandler(filters.Regex("^👤 Мой Профиль$"), profile_handler))
    app.add_handler(MessageHandler(filters.Regex("^🏠 Главное меню$"), start))

    # Дуэли
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(inline_callback, pattern="^duel$"), CommandHandler("duelrpl", start_duel_command)],
        states={WAITING_DUEL_SHOT: [CallbackQueryHandler(duel_shot, pattern="^shot_")]},
        fallbacks=[CommandHandler("cancel", start)]
    ))
    
    app.add_handler(CommandHandler("regrpl", regrpl_command))
    app.add_handler(CallbackQueryHandler(stick_duel_callback, pattern="^rpl_"))
    app.add_handler(CallbackQueryHandler(inline_callback, pattern="^(discord|website|support|regrpl_btn)$"))

    logger.info("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
