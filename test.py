import asyncio
import aiohttp
import logging
import sqlite3
import os
from dotenv import load_dotenv
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command

load_dotenv(override=True)
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Ошибка: BOT_TOKEN не найден.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

courier_sessions = {}

def init_db():
    conn = sqlite3.connect("delivery.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            address TEXT,
            lat REAL,
            lon REAL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER,
            lat REAL,
            lon REAL,
            status TEXT DEFAULT 'pending',
            courier_id INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS couriers (
            id INTEGER PRIMARY KEY,
            current_store_id INTEGER,
            status TEXT DEFAULT 'free'
        )
    """)
    
    # Создаем аптеку
    cursor.execute("SELECT COUNT(*) FROM stores")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO stores (name, address, lat, lon) VALUES ('Neman', 'Чынгыза Айтматова 97/4', 42.8228, 74.5857)")
        
    # ДОБАВЛЯЕМ ФЕЙКОВЫЕ ЗАКАЗЫ ДЛЯ ТЕСТА, если их нет
    cursor.execute("SELECT COUNT(*) FROM orders")
    if cursor.fetchone()[0] == 0:
        # Три случайные координаты в Бишкеке для теста
        cursor.execute("INSERT INTO orders (store_id, lat, lon, status) VALUES (1, 42.8300, 74.5800, 'pending')")
        cursor.execute("INSERT INTO orders (store_id, lat, lon, status) VALUES (1, 42.8400, 74.5900, 'pending')")
        cursor.execute("INSERT INTO orders (store_id, lat, lon, status) VALUES (1, 42.8250, 74.6000, 'pending')")
        
    conn.commit()
    conn.close()

async def handle_add_order(request):
    try:
        data = await request.json()
        store_id = int(data["store_id"])
        lat = float(data["lat"])
        lon = float(data["lon"])
        
        conn = sqlite3.connect("delivery.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO orders (store_id, lat, lon, status) VALUES (?, ?, ?, 'pending')", (store_id, lat, lon))
        conn.commit()
        conn.close()
        
        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=400)

async def optimize_route(shop_coords, delivery_coords):
    all_points = [shop_coords] + delivery_coords
    
    coords_str = ";".join([f"{lon},{lat}" for lat, lon in all_points])
    osrm_url = f"http://router.project-osrm.org/table/v1/driving/{coords_str}?annotations=duration"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(osrm_url) as response:
            data = await response.json()
            if data["code"] != "Ok":
                raise Exception("Ошибка при обращении к OSRM API")
            
            durations = data["durations"]

    unvisited = list(range(1, len(all_points)))
    current_node = 0
    ordered_route = []

    while unvisited:
        next_node = min(unvisited, key=lambda x: durations[current_node][x])
        unvisited.remove(next_node)
        ordered_route.append(all_points[next_node])
        current_node = next_node

    return ordered_route

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Начать доставку")]
        ],
        resize_keyboard=True
    )

def get_2gis_link(lat, lon):
    return f"https://2gis.ru/routeSearch/rsType/car/to/{lon},{lat}"

async def send_next_point(user_id: int):
    session = courier_sessions.get(user_id)
    if not session:
        return
        
    route = session["route"]
    shop_coords = session["shop_coords"]
    shop_name = session["shop_name"]
    shop_address = session["shop_address"]
    
    if not route:
        conn = sqlite3.connect("delivery.db")
        cursor = conn.cursor()
        cursor.execute("UPDATE orders SET status = 'completed' WHERE courier_id = ? AND status = 'delivering'", (user_id,))
        conn.commit()
        conn.close()
        
        courier_sessions.pop(user_id, None)
        
        await bot.send_message(user_id, f"Все заказы доставлены. Возвращайтесь в {shop_name} - {shop_address}.", reply_markup=get_main_keyboard())
        
        map_link = get_2gis_link(shop_coords[0], shop_coords[1])
        await bot.send_message(user_id, f"Ссылка для возвращения в аптеку:\n{map_link}")
        return

    next_coord = route.pop(0)
    
    await bot.send_message(user_id, f"Следующая точка доставки.\nОсталось адресов: {len(route) + 1}")
    
    map_link = get_2gis_link(next_coord[0], next_coord[1])
    delivered_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Заказ доставлен", callback_data="delivered")],
        [InlineKeyboardButton(text="Открыть навигатор 2ГИС", url=map_link)]
    ])
    
    await bot.send_location(
        user_id, 
        latitude=next_coord[0], 
        longitude=next_coord[1], 
        reply_markup=delivered_keyboard
    )

@dp.message(Command("start"))
async def cmd_start(message: Message):
    conn = sqlite3.connect("delivery.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO couriers (id, current_store_id, status) VALUES (?, 1, 'free')", (message.from_user.id,))
    conn.commit()
    conn.close()
    
    await message.answer("Добро пожаловать. Используйте кнопку Начать доставку для построения оптимального маршрута.", reply_markup=get_main_keyboard())

@dp.message(F.text == "Начать доставку")
async def start_delivery(message: Message):
    user_id = message.from_user.id
    conn = sqlite3.connect("delivery.db")
    cursor = conn.cursor()
    cursor.execute("SELECT current_store_id FROM couriers WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if not row or not row[0]:
        conn.close()
        await message.answer("Ошибка инициализации. Пожалуйста, отправьте команду /start повторно.")
        return
        
    store_id = row[0]
    cursor.execute("SELECT name, address, lat, lon FROM stores WHERE id = ?", (store_id,))
    store_info = cursor.fetchone()
    shop_name, shop_address, shop_lat, shop_lon = store_info[0], store_info[1], store_info[2], store_info[3]
    shop_coords = (shop_lat, shop_lon)
    
    if shop_coords == (0.0, 0.0):
        conn.close()
        await message.answer("Заполните координаты аптеки в базе данных. По координатам 0.0, 0.0 невозможно построить маршрут.")
        return
        
    cursor.execute("SELECT id, lat, lon FROM orders WHERE store_id = ? AND status = 'pending'", (store_id,))
    orders = cursor.fetchall()
    
    if not orders:
        conn.close()
        await message.answer("Нет доступных заказов для вашей аптеки.")
        return
        
    delivery_coords = [(row[1], row[2]) for row in orders]
    order_ids = [row[0] for row in orders]
    
    try:
        optimized_route = await optimize_route(shop_coords, delivery_coords)
        
        for o_id in order_ids:
            cursor.execute("UPDATE orders SET status = 'delivering', courier_id = ? WHERE id = ?", (user_id, o_id))
        conn.commit()
        conn.close()
        
        courier_sessions[user_id] = {
            "route": optimized_route,
            "order_ids": order_ids,
            "shop_coords": shop_coords,
            "shop_name": shop_name,
            "shop_address": shop_address
        }
        
        await message.answer("Маршрут построен. Поехали!")
        await send_next_point(user_id)
        
    except Exception as e:
        conn.close()
        await message.answer(f"Произошла ошибка при построении маршрута: {e}")

@dp.callback_query(F.data == "delivered")
async def process_delivery(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply("Отлично. Переходим к следующему адресу.")
    await send_next_point(callback.from_user.id)

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    
    app = web.Application()
    app.router.add_post("/add_order", handle_add_order)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())