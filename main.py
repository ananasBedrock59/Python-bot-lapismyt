import asyncio
import json
import os

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNotFound
)
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton
from aiogram.types import Message
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from dotenv import load_dotenv
from loguru import logger

from database import Database


load_dotenv()

BOT_TOKEN: str = os.getenv('BOT_TOKEN')
MAX_WARNINGS: int = int(os.getenv('MAX_WARNINGS'))
CLEANUP_INTERVAL: int = int(os.getenv('CLEANUP_INTERVAL'))
DB_URI: str = os.getenv('DB_URI')
OWNER_ID: int = int(os.getenv('OWNER_ID'))
DB_NAME: str =os.getenv('DB_NAME')


with open('lang.json') as f:
    translations = json.load(f)


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

db = Database(DB_URI, DB_NAME)


class ChatState:
    def __init__(self):
        self.user_languages = {}
        self.active_pairs = {}
        self.waiting_queue = set()
        self.warnings = {} 
        self.banned_users = set()
        self.reporting = {}
        self.invite_links = {}
        self.premium_users = {OWNER_ID: 'infinity'} 
        
state = ChatState()


async def get_translation(user_id: int) -> dict:
    lang = await db.get_user_language(user_id) or 'en'
    return translations.get(lang, translations['en'])


async def send_message(user_id: int, key: str, **kwargs):
    if user_id in state.banned_users:
        return
    
    try:
        translation = await get_translation(user_id)
        message = translation.get(key, translations['en'].get(key, key))
        if message:
            await bot.send_message(user_id, message.format(**kwargs))
    except (TelegramForbiddenError, TelegramNotFound):
        logger.info(f'User {user_id} blocked the bot or chat not found')
        await cleanup_user(user_id)
    except Exception as e:
        logger.error(f'Error sending to {user_id}: {e}')


async def connect_users(user1_id: int, user2_id: int):
    await db.add_active_pair(user1_id, user2_id)
    await send_message(user1_id, 'partnerFind')
    await send_message(user2_id, 'partnerFind')


async def disconnect_users(user1_id: int, user2_id: int):
    await db.remove_active_pair(user1_id)
    await db.remove_active_pair(user2_id)
    await send_message(user1_id, 'skipDialogue')
    await send_message(user2_id, 'partnerSkipDialogue')


async def cleanup_user(user_id: int):
    partner_id = await db.remove_active_pair(user_id)
    if partner_id:
        await send_message(partner_id, 'partnerSkipDialogue')

    await db.remove_from_waiting(user_id)

    global reporting
    if user_id in reporting:
        del reporting[user_id]


@dp.message(CommandStart())
async def start_handler(message: Message):
    user_id = message.from_user.id
    
    if user_id in state.banned_users:
        await send_message(user_id, 'banMessage')
        return
    
    lang = message.from_user.language_code or 'en'
    await db.set_user_language(user_id, lang)
    await send_message(user_id, 'hello')


@dp.message(Command('help'))
async def help_handler(message: Message):
    await send_message(message.from_user.id, 'helpText')


@dp.message(Command('next'))
async def next_handler(message: Message):
    user_id = message.from_user.id

    if await db.is_banned(user_id):
        await send_message(user_id, 'banMessage')
        return

    if await db.active_pairs.find_one({"user_id": user_id}):
        await send_message(user_id, 'inDialogueWarning')
        return

    warnings = await db.get_warnings(user_id)
    if warnings >= MAX_WARNINGS:
        await db.ban_user(user_id)
        await cleanup_user(user_id)
        await send_message(user_id, 'banMessage')
        return

    waiting_user = await db.waiting_queue.find_one_and_delete({})
    if waiting_user:
        partner_id = waiting_user['user_id']
        await db.add_active_pair(user_id, partner_id)
        await connect_users(user_id, partner_id)
    else:
        await db.add_to_waiting(user_id)
        await send_message(user_id, 'partnerFinding')


@dp.message(Command('stop'))
async def stop_handler(message: Message):
    user_id = message.from_user.id

    if await db.is_banned(user_id):
        await send_message(user_id, 'banMessage')
        return

    active_pair = await db.active_pairs.find_one({"user_id": user_id})
    if active_pair:
        partner_id = active_pair["pair_id"]
        await disconnect_users(user_id, partner_id)
    else:
        await db.remove_from_waiting(user_id)


@dp.message(Command('report'))
async def report_handler(message: Message):
    user_id = message.from_user.id
    
    if user_id in state.banned_users:
        return
    
    if user_id not in state.active_pairs:
        await send_message(user_id, 'dialogueHaveError')
        return
    
    partner_id = state.active_pairs[user_id]
    state.reporting[user_id] = partner_id
    
    translation = await get_translation(user_id)
    keyboard = ReplyKeyboardBuilder()
    
    for reason in translation.get('reportReasons', []):
        keyboard.add(KeyboardButton(text=reason))
    
    await bot.send_message(
        user_id,
        translation['reportOptions'],
        reply_markup=keyboard.as_markup(resize_keyboard=True, one_time_keyboard=True)
    )


@dp.message(F.text)
async def text_handler(message: Message):
    user_id = message.from_user.id
    text = message.text

    if await db.is_banned(user_id):
        return

    global reporting
    if user_id in reporting:
        partner_id = reporting[user_id]
        del reporting[user_id]

        await db.add_warning(partner_id)
        warnings = await db.get_warnings(partner_id)

        if warnings >= MAX_WARNINGS:
            await db.ban_user(partner_id)
            await cleanup_user(partner_id)
            await send_message(partner_id, 'banMessage')

        await disconnect_users(user_id, partner_id)
        await send_message(user_id, 'reportSuccess')
        return

    if text.startswith('/'):
        return

    active_pair = await db.active_pairs.find_one({"user_id": user_id})
    if active_pair:
        partner_id = active_pair["pair_id"]
        try:
            await bot.send_message(partner_id, text)
        except Exception as e:
            logger.error(f'Forward error: {e}')
            await disconnect_users(user_id, partner_id)
    else:
        await send_message(user_id, 'sendNext')


async def main():
    await db.init_indexes()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())