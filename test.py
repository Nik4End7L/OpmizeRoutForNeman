import asyncio
import aiohttp
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command

BOT_TOKEN = "8989670940:AAF5PYdk_VP2zhqW0FCwhqdUQyxyMyyD3N8"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

courier_sessions = {}

SHOP_1 = (42.82977846070033, 74.5835613036102) 

TEST_DELIVERIES = [
    (42.82484134552818, 74.58425820737811),
    (42.83137280778167, 74.5642618636122),
    (42.83832688117094, 74.56632454957837),
    (42.8248802556268, 74.57405841675455),
]

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

def get_delivered_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Заказ доставлен", callback_data="delivered")]
    ])

async def send_next_point(user_id: int):
    route = courier_sessions.get(user_id, [])
    
    if not route:
        await bot.send_message(user_id, "Все заказы доставлены. Возвращайтесь в магазин.", reply_markup=get_main_keyboard())
        await bot.send_location(user_id, latitude=SHOP_1[0], longitude=SHOP_1[1])
        return

    next_coord = route.pop(0)
    
    await bot.send_message(user_id, f"Следующая точка доставки.\nОсталось адресов: {len(route) + 1}")
    await bot.send_location(
        user_id, 
        latitude=next_coord[0], 
        longitude=next_coord[1], 
        reply_markup=get_delivered_keyboard()
    )

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Добро пожаловать. Используйте кнопку Начать доставку для построения оптимального маршрута.", reply_markup=get_main_keyboard())

@dp.message(F.text == "Начать доставку")
async def start_delivery(message: Message):
    await message.answer("Строю оптимальный маршрут от магазина. Пожалуйста, подождите.")
    
    try:
        optimized_route = await optimize_route(SHOP_1, TEST_DELIVERIES)
        courier_sessions[message.from_user.id] = optimized_route
        await message.answer("Маршрут построен. Поехали!")
        await send_next_point(message.from_user.id)
        
    except Exception as e:
        await message.answer(f"Произошла ошибка при построении маршрута: {e}")

@dp.callback_query(F.data == "delivered")
async def process_delivery(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply("Отлично. Переходим к следующему адресу.")
    await send_next_point(callback.from_user.id)

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())