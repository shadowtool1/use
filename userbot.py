import asyncio
import sqlite3
import os
import sys
import hashlib
import secrets
import requests
from datetime import datetime, timedelta

from flask import Flask, jsonify, request
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
from aiogram.utils import executor
import nest_asyncio

nest_asyncio.apply()

# ========== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ==========
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
ADMIN_ID = os.environ.get('ADMIN_ID', '')  # Просто число, без запятых
WHITELIST_GIST_URL = os.environ.get('WHITELIST_GIST_URL', 'https://gist.githubusercontent.com/shadowtool1/af1959fc4b147c2ddc549c862d63cd9b/raw/whitelist.json')
API_BASE_URL = os.environ.get('API_BASE_URL', 'https://твой-сайт.ru')  # Адрес твоего сайта

# Проверка
if not BOT_TOKEN:
    print("❌ Ошибка: BOT_TOKEN не задан")
    sys.exit(1)

if not ADMIN_ID:
    print("❌ Ошибка: ADMIN_ID не задан")
    sys.exit(1)

ADMIN_ID = int(ADMIN_ID)

# ========== БАЗА ДАННЫХ ==========
VOLUME_PATH = os.environ.get('VOLUME_MOUNTS', '/app/data')
if not os.path.exists(VOLUME_PATH):
    VOLUME_PATH = '.'
    os.makedirs(VOLUME_PATH, exist_ok=True)

DB_PATH = os.path.join(VOLUME_PATH, 'shadowtool.db')
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

# Таблицы
cursor.execute('''
    CREATE TABLE IF NOT EXISTS web_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        login TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS web_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token TEXT UNIQUE NOT NULL,
        expires_at TEXT NOT NULL
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        login TEXT NOT NULL,
        plan_type TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        status TEXT DEFAULT 'active'
    )
''')
conn.commit()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def generate_token():
    return secrets.token_urlsafe(32)

def is_admin(user_id):
    return user_id == ADMIN_ID

# ========== FLASK API ==========
flask_app = Flask(__name__)

@flask_app.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    login = data.get('login')
    password = data.get('password')
    
    if not login or not password:
        return jsonify({'error': 'Login and password required'}), 400
    
    cursor.execute('SELECT id FROM web_users WHERE login=?', (login,))
    if cursor.fetchone():
        return jsonify({'error': 'Login already exists'}), 400
    
    password_hash = hash_password(password)
    cursor.execute('INSERT INTO web_users (login, password_hash) VALUES (?, ?)', (login, password_hash))
    conn.commit()
    user_id = cursor.lastrowid
    
    token = generate_token()
    expires_at = (datetime.now() + timedelta(days=30)).isoformat()
    cursor.execute('INSERT INTO web_sessions (user_id, token, expires_at) VALUES (?, ?, ?)', (user_id, token, expires_at))
    conn.commit()
    
    return jsonify({'success': True, 'token': token, 'login': login})

@flask_app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    login = data.get('login')
    password = data.get('password')
    
    if not login or not password:
        return jsonify({'error': 'Login and password required'}), 400
    
    password_hash = hash_password(password)
    cursor.execute('SELECT id FROM web_users WHERE login=? AND password_hash=?', (login, password_hash))
    row = cursor.fetchone()
    
    if not row:
        return jsonify({'error': 'Invalid credentials'}), 401
    
    user_id = row[0]
    
    cursor.execute('DELETE FROM web_sessions WHERE user_id=?', (user_id,))
    
    token = generate_token()
    expires_at = (datetime.now() + timedelta(days=30)).isoformat()
    cursor.execute('INSERT INTO web_sessions (user_id, token, expires_at) VALUES (?, ?, ?)', (user_id, token, expires_at))
    conn.commit()
    
    return jsonify({'success': True, 'token': token, 'login': login})

@flask_app.route('/me', methods=['GET'])
def me():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return jsonify({'error': 'No token'}), 401
    
    cursor.execute('''
        SELECT web_users.id, web_users.login, web_sessions.expires_at 
        FROM web_sessions 
        JOIN web_users ON web_sessions.user_id = web_users.id 
        WHERE web_sessions.token=? AND web_sessions.expires_at > ?
    ''', (token, datetime.now().isoformat()))
    row = cursor.fetchone()
    
    if not row:
        return jsonify({'error': 'Invalid or expired token'}), 401
    
    return jsonify({'id': row[0], 'login': row[1]})

@flask_app.route('/check_subscription', methods=['GET'])
def check_subscription():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return jsonify({'has_access': False, 'error': 'No token'}), 401
    
    cursor.execute('SELECT user_id FROM web_sessions WHERE token=? AND expires_at > ?', (token, datetime.now().isoformat()))
    row = cursor.fetchone()
    if not row:
        return jsonify({'has_access': False, 'error': 'Invalid session'}), 401
    
    user_id = row[0]
    
    cursor.execute('''
        SELECT plan_type, end_date, status 
        FROM subscriptions 
        WHERE user_id=? AND status='active' AND end_date > ?
    ''', (user_id, datetime.now().isoformat()))
    sub = cursor.fetchone()
    
    if sub:
        return jsonify({'has_access': True, 'plan': sub[0], 'expires_at': sub[1]})
    else:
        return jsonify({'has_access': False})

@flask_app.route('/is_whitelisted', methods=['POST'])
def is_whitelisted():
    data = request.get_json()
    phone = data.get('phone', '')
    
    # Нормализация номера
    clean = ''.join(c for c in phone if c.isdigit())
    if clean.startswith('8'):
        clean = '7' + clean[1:]
    if not clean.startswith('7'):
        clean = '7' + clean
    normalized = '+' + clean
    
    try:
        resp = requests.get(WHITELIST_GIST_URL, timeout=10)
        data = resp.json()
        whitelist = data.get('phones', [])
        is_blocked = any(item.get('number') == normalized for item in whitelist)
        return jsonify({'blocked': is_blocked})
    except Exception as e:
        print(f"Ошибка загрузки белого списка: {e}")
        return jsonify({'blocked': False})

@flask_app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

# ========== TELEGRAM БОТ ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

@dp.message_handler(commands=['start'])
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён")
        return
    
    await message.answer(
        "🔰 <b>SHADOW TOOL - АДМИН ПАНЕЛЬ</b>\n\n"
        "<b>📋 ПОДПИСКИ</b>\n"
        "/add_sub логин план - выдать подписку\n"
        "/subs - список подписок\n"
        "/users - список пользователей\n\n"
        "<b>⚙️ ПЛАНЫ</b>\n"
        "2_days, 1_week, 1_month, forever\n\n"
        "<b>📊 СТАТУС</b>\n"
        "/stats - статистика",
        parse_mode='HTML'
    )

@dp.message_handler(commands=['add_sub'])
async def add_subscription(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    args = message.get_args().split()
    if len(args) < 2:
        await message.answer("❌ /add_sub логин_сайта план\nВарианты: 2_days, 1_week, 1_month, forever")
        return
    
    login = args[0]
    plan = args[1]
    
    cursor.execute('SELECT id, login FROM web_users WHERE login=?', (login,))
    row = cursor.fetchone()
    
    if not row:
        await message.answer(f"❌ Пользователь с логином '{login}' не найден")
        return
    
    user_id = row[0]
    user_login = row[1]
    
    start_date = datetime.now()
    
    if plan == '2_days':
        end_date = start_date + timedelta(days=2)
    elif plan == '1_week':
        end_date = start_date + timedelta(days=7)
    elif plan == '1_month':
        end_date = start_date + timedelta(days=30)
    elif plan == 'forever':
        end_date = start_date + timedelta(days=365*10)
    else:
        await message.answer("❌ Неверный план. Доступны: 2_days, 1_week, 1_month, forever")
        return
    
    cursor.execute('''
        INSERT OR REPLACE INTO subscriptions (user_id, login, plan_type, start_date, end_date, status)
        VALUES (?, ?, ?, ?, ?, 'active')
    ''', (user_id, user_login, plan, start_date.isoformat(), end_date.isoformat()))
    conn.commit()
    
    await message.answer(f"✅ Подписка {plan} выдана для {user_login}\n📅 Истекает: {end_date.strftime('%d.%m.%Y')}")

@dp.message_handler(commands=['subs'])
async def list_subscriptions(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    cursor.execute('SELECT login, plan_type, end_date, status FROM subscriptions ORDER BY end_date DESC LIMIT 30')
    rows = cursor.fetchall()
    
    if not rows:
        await message.answer("📭 Нет подписок")
        return
    
    text = "📋 <b>ПОДПИСКИ</b>\n\n"
    for login, plan, end_date, status in rows:
        emoji = "✅" if status == "active" else "❌"
        text += f"{emoji} {login} — {plan}\n   до {end_date[:10]}\n"
    
    await message.answer(text[:4000], parse_mode='HTML')

@dp.message_handler(commands=['users'])
async def cmd_users(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    cursor.execute('''
        SELECT w.id, w.login, w.created_at, 
               s.plan_type, s.end_date, s.status
        FROM web_users w
        LEFT JOIN subscriptions s ON w.id = s.user_id AND s.status = 'active'
        ORDER BY w.id DESC
    ''')
    rows = cursor.fetchall()
    
    if not rows:
        await message.answer("📭 Нет пользователей на сайте")
        return
    
    out = "👥 <b>ПОЛЬЗОВАТЕЛИ САЙТА</b>\n\n"
    for uid, login, created, plan, end_date, status in rows:
        sub_info = ""
        if plan:
            sub_info = f"\n   📅 {plan} до {end_date[:10]}"
        out += f"• {login} (ID: {uid})\n   📅 Рег: {created[:10]}{sub_info}\n\n"
        if len(out) > 3500:
            await message.answer(out, parse_mode='HTML')
            out = ""
    if out:
        await message.answer(out, parse_mode='HTML')

@dp.message_handler(commands=['stats'])
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    cursor.execute('SELECT COUNT(*) FROM web_users')
    users_count = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM subscriptions WHERE status="active"')
    active_subs = cursor.fetchone()[0]
    
    await message.answer(
        f"📊 <b>СТАТИСТИКА</b>\n\n"
        f"👥 Пользователей: {users_count}\n"
        f"✅ Активных подписок: {active_subs}",
        parse_mode='HTML'
    )

# ========== ВЕБ-СЕРВЕР ==========
def run_web():
    flask_app.run(host='0.0.0.0', port=8080, debug=False)

# ========== ЗАПУСК ==========
async def main():
    print(f"🚀 SHADOW TOOL запущен")
    print(f"📡 API: http://0.0.0.0:8080")
    print(f"🤖 Бот: @{bot.get_me().username}")

if __name__ == '__main__':
    from threading import Thread
    Thread(target=run_web, daemon=True).start()
    executor.start_polling(dp, skip_updates=True)
    try:
        asyncio.run(main())
    except RuntimeError:
        loop = asyncio.get_event_loop()
        loop.create_task(main())
