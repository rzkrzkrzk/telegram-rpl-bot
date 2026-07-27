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
matchmaking_queue = {} # {user_id: {chat_id, message_id, start_time, first_name}}
active_stick_matches = {} # {match_id: data}

# ---------- БД ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS source_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE, username TEXT, added_by INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS target_chats (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE, link TEXT, added_by INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS support_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, text TEXT, timestamp TEXT, answered INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, last_activity INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_config (key TEXT PRIMARY KEY, value TEXT)''')
    
    # Статистика Дуэли Буллитов
    c.execute('''CREATE TABLE IF NOT EXISTS duel_stats (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, total_shots INTEGER DEFAULT 0, goals INTEGER DEFAULT 0)''')
    
    # Статистика Дуэли Клюшек (MMR)
    c.execute('''CREATE TABLE IF NOT EXISTS stick_stats (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, mmr INTEGER DEFAULT 1000, games_played INTEGER DEFAULT 0)''')
    
    # Инициализация GIF (теперь по 3 штуки)
    for i in range(1, 4):
        c.execute('INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)', (f'gif_goal_{i}', ''))
        c.execute('INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)', (f'gif_save_{i}', ''))
    
    conn.commit()
    conn.close()

init_db()

# --- Функции БД ---
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

def update_stick_stats(user_id, username, first_name, mmr_change):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO stick_stats (user_id, username, first_name, mmr, games_played)
                 VALUES (?, ?, ?, ?, 1) ON CONFLICT(user_id) DO UPDATE SET
                 mmr = mmr + ?, games_played = games_played + 1''', (user_id, username, first_name, 1000 + mmr_change, mmr_change))
    conn.commit()
    conn.close()

def get_mmr(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT mmr FROM stick_stats WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 1000

# ---------- Клавиатуры ----------
def admin_menu_keyboard():
    return ReplyKeyboardMarkup([
        ["➕ Добавить каналы", "➕ Добавить чаты"],
        ["📩 Проверить поддержку", "⚙️ Настройки"],
        ["🎮 Настройки игр", "🚪 Выйти"]
    ], resize_keyboard=True)

def game_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏒 Буллиты: Изменить GIF", callback_data="adm_bul_gifs")],
        [InlineKeyboardButton("🧹 Буллиты: Обнулить рейтинг", callback_data="adm_bul_reset")],
        [InlineKeyboardButton("🧹 Клюшки: Обнулить рейтинг", callback_data="adm_stick_reset")]
    ])

def welcome_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Наш Discord", callback_data="discord")],
        [InlineKeyboardButton("🌐 Наш Сайт", callback_data="website")],
        [InlineKeyboardButton("🆘 Поддержка", callback_data="support")],
        [InlineKeyboardButton("🏒 Дуэль Буллитов (ИИ)", callback_data="duel")],
        [InlineKeyboardButton("⚔️ Дуэль Клюшек (PvP)", callback_data="stick_search")]
    ])

# ---------- ЛОГИКА ДУЭЛИ КЛЮШЕК ----------

async def regrpl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    if user.id in matchmaking_queue:
        await update.message.reply_text("❌ Вы уже ищете соперника!")
        return

    # Проверка кросс-чат поиска
    for uid, data in matchmaking_queue.items():
        if uid != user.id:
            matchmaking_queue.pop(uid)
            await start_stick_match(context, uid, user.id, update.effective_chat.id)
            return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Принять", callback_data=f"stick_accept_{user.id}")],
        [InlineKeyboardButton("🤖 Играть с ИИ", callback_data=f"stick_ai_{user.id}")],
        [InlineKeyboardButton("❌ Отменить", callback_data=f"stick_cancel_{user.id}")]
    ])
    
    msg = await update.message.reply_text(
        f"🔍 {user.first_name} ищет соперника для Дуэли Клюшек!\nОжидание: 45 сек...",
        reply_markup=kb
    )
    
    matchmaking_queue[user.id] = {
        "chat_id": chat_id, "message_id": msg.message_id, 
        "start_time": time.time(), "first_name": user.first_name
    }
    
    # Таймер на 45 секунд
    asyncio.create_task(matchmaking_timer(user.id, context))

async def matchmaking_timer(user_id, context):
    await asyncio.sleep(45)
    if user_id in matchmaking_queue:
        data = matchmaking_queue.pop(user_id)
        await context.bot.edit_message_text(
            chat_id=data["chat_id"], message_id=data["message_id"],
            text=f"⏰ Соперник не найден. {data['first_name']} играет с ботом!"
        )
        await start_stick_match(context, user_id, 0, data["chat_id"]) # 0 - ID бота

async def start_stick_match(context, p1_id, p2_id, chat_id):
    match_id = f"{p1_id}_{p2_id}_{int(time.time())}"
    
    # Получаем имена
    p1_name = (await context.bot.get_chat(p1_id)).first_name
    p2_name = "ИИ Вратарь" if p2_id == 0 else (await context.bot.get_chat(p2_id)).first_name
    
    active_stick_matches[match_id] = {
        "p1": {"id": p1_id, "name": p1_name, "score": 0},
        "p2": {"id": p2_id, "name": p2_name, "score": 0},
        "round": 1, "turn": p1_id, "phase": "shooting", "last_choice": None
    }
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🏒 МАТЧ НАЧАТ!\n👤 Нападающий: {p1_name}\n🧤 Вратарь: {p2_name}\n\n👉 {p1_name}, выбирай куда бросить:",
        reply_markup=stick_shoot_kb(match_id)
    )

def stick_shoot_kb(match_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Правая девятка", callback_data=f"st_s_R_{match_id}")],
        [InlineKeyboardButton("Левая девятка", callback_data=f"st_s_L_{match_id}")],
        [InlineKeyboardButton("Домик", callback_data=f"st_s_D_{match_id}")]
    ])

def stick_defend_kb(match_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Девятки (Л+П)", callback_data=f"st_d_C_{match_id}")],
        [InlineKeyboardButton("Домик", callback_data=f"st_d_D_{match_id}")]
    ])

async def stick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data
    
    # Обработка кнопок поиска
    if data.startswith("stick_accept"):
        owner_id = int(data.split("_")[2])
        if user.id == owner_id: return await query.answer("Вы не можете принять свой поиск!")
        if owner_id in matchmaking_queue:
            matchmaking_queue.pop(owner_id)
            await query.message.delete()
            await start_stick_match(context, owner_id, user.id, query.message.chat_id)
        return

    if data.startswith("stick_ai"):
        owner_id = int(data.split("_")[2])
        if user.id != owner_id: return await query.answer("Только создатель может запустить ИИ!")
        matchmaking_queue.pop(owner_id, None)
        await query.message.delete()
        await start_stick_match(context, owner_id, 0, query.message.chat_id)
        return

    if data.startswith("stick_cancel"):
        owner_id = int(data.split("_")[2])
        if user.id != owner_id: return await query.answer("Только создатель может отменить!")
        matchmaking_queue.pop(owner_id, None)
        await query.edit_message_text("❌ Поиск отменен.")
        return

    # Логика игры
    # st_s (shoot), st_d (defend)
    parts = data.split("_")
    action, choice, match_id = parts[1], parts[2], parts[3]
    
    if match_id not in active_stick_matches:
        return await query.answer("Матч устарел.")
    
    m = active_stick_matches[match_id]
    
    if action == "s": # Ход нападающего
        if user.id != m["turn"]: return await query.answer("Сейчас не ваш ход бросать!")
        m["last_choice"] = choice
        m["phase"] = "defending"
        defender = m["p2"] if m["turn"] == m["p1"]["id"] else m["p1"]
        
        if defender["id"] == 0: # Если вратарь ИИ
            ai_def = random.choice(["C", "D"])
            await process_stick_round(query, context, match_id, ai_def)
        else:
            await query.edit_message_text(
                f"Вратарь: {defender['name']}, Нападающий: {user.first_name}\n"
                f"⏳ Сейчас {defender['name']} выбирает как отбить...",
                reply_markup=stick_defend_kb(match_id)
            )
            
    elif action == "d": # Ход вратаря
        attacker_id = m["p1"]["id"] if m["turn"] == m["p1"]["id"] else m["p2"]["id"]
        defender = m["p2"] if m["turn"] == m["p1"]["id"] else m["p1"]
        if user.id != defender["id"]: return await query.answer("Сейчас не ваш ход защищаться!")
        await process_stick_round(query, context, match_id, choice)

async def process_stick_round(query, context, match_id, def_choice):
    m = active_stick_matches[match_id]
    shot = m["last_choice"]
    attacker = m["p1"] if m["turn"] == m["p1"]["id"] else m["p2"]
    defender = m["p2"] if m["turn"] == m["p1"]["id"] else m["p1"]
    
    # Определение гола
    # Девятки (C) кроют R и L. Домик (D) кроет D.
    scored = True
    if def_choice == "C" and (shot == "R" or shot == "L"): scored = False
    if def_choice == "D" and shot == "D": scored = False
    
    if scored:
        attacker["score"] += 1
        res = "✅ ГОЛ!"
    else:
        res = "🧤 СЕЙВ!"
        
    # Смена хода
    if m["turn"] == m["p1"]["id"]:
        m["turn"] = m["p2"]["id"]
        m["phase"] = "shooting"
        # Если второй игрок ИИ
        if m["p2"]["id"] == 0:
            ai_shot = random.choice(["R", "L", "D"])
            m["last_choice"] = ai_shot
            await query.message.reply_text(f"{res}\nТеперь вы защищаетесь! ИИ бросает...", reply_markup=stick_defend_kb(match_id))
        else:
            await query.message.reply_text(f"{res}\n🔄 Смена сторон! Теперь {m['p2']['name']} выбирает куда бросить.", reply_markup=stick_shoot_kb(match_id))
    else:
        # Конец раунда
        if m["round"] < 3:
            m["round"] += 1
            m["turn"] = m["p1"]["id"]
            await query.message.reply_text(f"{res}\n🏆 Раунд {m['round']}! {m['p1']['name']} бросает.", reply_markup=stick_shoot_kb(match_id))
        else:
            # КОНЕЦ МАТЧА
            p1, p2 = m["p1"], m["p2"]
            result_text = f"🏁 МАТЧ ОКОНЧЕН!\n{p1['name']}: {p1['score']}\n{p2['name']}: {p2['score']}\n\n"
            
            if p1["score"] > p2["score"]:
                result_text += f"🏆 Победитель: {p1['name']}"
                if p2["id"] != 0:
                    update_stick_stats(p1["id"], None, p1["name"], 25)
                    update_stick_stats(p2["id"], None, p2["name"], -20)
            elif p2["score"] > p1["score"]:
                result_text += f"🏆 Победитель: {p2['name']}"
                if p2["id"] != 0:
                    update_stick_stats(p2["id"], None, p2["name"], 25)
                    update_stick_stats(p1["id"], None, p1["name"], -20)
            else:
                result_text += "🤝 Ничья!"
                
            active_stick_matches.pop(match_id)
            await query.message.reply_text(result_text)

# --- Команды ММР ---
async def mymmr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mmr = get_mmr(update.effective_user.id)
    await update.message.reply_text(f"🎖 Ваш текущий MMR в Дуэли Клюшек: {mmr}")

async def ratingmmr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT first_name, mmr FROM stick_stats ORDER BY mmr DESC LIMIT 10')
    rows = c.fetchall()
    conn.close()
    
    if not rows: return await update.message.reply_text("Рейтинг пока пуст.")
    
    text = "🥇 ТОП-10 MMR Дуэли Клюшек:\n\n"
    for i, r in enumerate(rows):
        text += f"{i+1}. {r[0]} — {r[1]} MMR\n"
    await update.message.reply_text(text)

# ---------- ОБНОВЛЕННАЯ ДУЭЛЬ БУЛЛИТОВ ----------

async def duel_shot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    shot_zone = query.data
    user = query.from_user

    goalie_zones = ["shot_left", "shot_right", "shot_five", "shot_low"]
    goalie_choice = random.choice(goalie_zones)
    scored = random.random() < 0.35 if shot_zone != goalie_choice else False

    update_duel_stats(user.id, user.username, user.first_name, scored)

    # Выбор случайной GIF из 3-х возможных
    idx = random.randint(1, 3)
    if scored:
        gif = get_config(f'gif_goal_{idx}') or "https://media.giphy.com/media/3o7aTskHEUdgCQAXde/giphy.gif"
        res = "⚡️ ГОЛ!"
    else:
        gif = get_config(f'gif_save_{idx}') or "https://media.giphy.com/media/3o6Ztq5cG6GZj5F9uo/giphy.gif"
        res = "🧤 СЕЙВ!"

    await query.edit_message_text(f"{res}\nВратарь выбрал: {goalie_choice}")
    try: await query.message.reply_animation(gif)
    except: pass
    return ConversationHandler.END

# ---------- ОБНОВЛЕННАЯ АДМИНКА ----------

async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    text = update.message.text

    if text == "🎮 Настройки игр":
        await update.message.reply_text("Выберите игру для управления:", reply_markup=game_admin_keyboard())
    elif text == "🚪 Выйти":
        remove_admin(user_id)
        await update.message.reply_text("Вышли.", reply_markup=main_menu_keyboard())
    # ... старые обработчики каналов/чатов ...
    return ConversationHandler.END

async def game_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "adm_bul_gifs":
        await query.edit_message_text("Выберите слот для загрузки GIF:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Goal 1", callback_data="setgif_goal_1"), InlineKeyboardButton("Goal 2", callback_data="setgif_goal_2"), InlineKeyboardButton("Goal 3", callback_data="setgif_goal_3")],
            [InlineKeyboardButton("Save 1", callback_data="setgif_save_1"), InlineKeyboardButton("Save 2", callback_data="setgif_save_2"), InlineKeyboardButton("Save 3", callback_data="setgif_save_3")]
        ]))
    elif data == "adm_bul_reset":
        reset_all_ratings()
        await query.edit_message_text("✅ Рейтинг Дуэли Буллитов обнулен!")
    elif data == "adm_stick_reset":
        conn = sqlite3.connect(DB_PATH)
        conn.execute('DELETE FROM stick_stats')
        conn.commit()
        conn.close()
        await query.edit_message_text("✅ Рейтинг Дуэли Клюшек обнулен!")

async def set_gif_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data["gif_key"] = query.data.replace("setgif_", "gif_")
    await query.edit_message_text(f"📤 Отправьте GIF для слота {context.user_data['gif_key']}:")
    return WAITING_GIF_UPLOAD

async def receive_gif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = update.message.animation.file_id if update.message.animation else None
    if file_id:
        set_config(context.user_data.get("gif_key"), file_id)
        await update.message.reply_text("✅ Сохранено!")
        return ConversationHandler.END
    return WAITING_GIF_UPLOAD

# ---------- МЕНЮ КОМАНД ----------
async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("duelrpl", "Буллиты с ИИ"),
        BotCommand("regrpl", "Дуэль Клюшек (PvP)"),
        BotCommand("mymmr", "Мой MMR"),
        BotCommand("ratingmmr", "Топ MMR"),
        BotCommand("rating", "Топ Буллитов"),
    ], scope=BotCommandScopeDefault())

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # Дуэль Клюшек (PvP)
    app.add_handler(CommandHandler("regrpl", regrpl))
    app.add_handler(CommandHandler("mymmr", mymmr))
    app.add_handler(CommandHandler("ratingmmr", ratingmmr))
    app.add_handler(CallbackQueryHandler(stick_callback, pattern="^(stick_|st_)"))

    # Дуэль Буллитов (ИИ)
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("duelrpl", start_duel_command), CallbackQueryHandler(inline_callback, pattern="^duel$")],
        states={WAITING_DUEL_SHOT: [CallbackQueryHandler(duel_shot, pattern="^shot_")]},
        fallbacks=[CommandHandler("cancel", start)]
    ))

    # Админка GIF
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(set_gif_start, pattern="^setgif_")],
        states={WAITING_GIF_UPLOAD: [MessageHandler(filters.ANIMATION, receive_gif)]},
        fallbacks=[CommandHandler("cancel", start)]
    ))

    app.add_handler(CallbackQueryHandler(game_admin_callback, pattern="^adm_"))
    app.add_handler(MessageHandler(filters.Regex("^🎮 Настройки игр$"), admin_buttons))
    app.add_handler(CommandHandler("adminkarpl", adminkarpl))
    app.add_handler(CommandHandler("rating", rating_command))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^🏠 Главное меню$"), main_menu))

    # (Добавьте остальные хендлеры из вашего старого кода сюда)
    app.add_handler(CallbackQueryHandler(inline_callback, pattern="^(discord|website|support)$"))

    app.run_polling()

if __name__ == "__main__":
    main()
