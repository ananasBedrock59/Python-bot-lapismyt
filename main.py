from loguru import logger
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.filters.state import StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.exceptions import (
    TelegramForbiddenError, 
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramBadRequest
)
import json
import os


from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN: str = os.getenv('BOT_TOKEN')
MAX_WARNINGS: int = int(os.getenv('MAX_WARNINGS'))
CLEANUP_INTERVAL: int = int(os.getenv('CLEANUP_INTERVAL'))
DB_URI: str | None = os.getenv('DB_URI')  # не используется
OWNER_ID: int = int(os.getenv('OWNER_ID'))


# загрузка translations из lang.json
with open('lang.json') as f:
    translations = json.load(f)


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


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
    lang = state.user_languages.get(user_id, 'en')
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
    state.active_pairs[user1_id] = user2_id
    state.active_pairs[user2_id] = user1_id
    await send_message(user1_id, 'partnerFind')
    await send_message(user2_id, 'partnerFind')


async def disconnect_users(user1_id: int, user2_id: int):
    if user1_id in state.active_pairs:
        del state.active_pairs[user1_id]
    if user2_id in state.active_pairs:
        del state.active_pairs[user2_id]
    
    await send_message(user1_id, 'skipDialogue')
    await send_message(user2_id, 'partnerSkipDialogue')


async def cleanup_user(user_id: int):
    if user_id in state.active_pairs:
        partner_id = state.active_pairs[user_id]
        del state.active_pairs[user_id]
        if partner_id in state.active_pairs:
            del state.active_pairs[partner_id]
        await send_message(partner_id, 'partnerSkipDialogue')
    
    if user_id in state.waiting_queue:
        state.waiting_queue.remove(user_id)
    
    if user_id in state.reporting:
        del state.reporting[user_id]


@dp.message(CommandStart())
async def start_handler(message: Message):
    user_id = message.from_user.id
    
    if user_id in state.banned_users:
        await send_message(user_id, 'banMessage')
        return
    
    lang = message.from_user.language_code or 'en'
    state.user_languages[user_id] = lang if lang in translations else 'en'
    await send_message(user_id, 'hello')


@dp.message(Command('help'))
async def help_handler(message: Message):
    await send_message(message.from_user.id, 'helpText')


@dp.message(Command('next'))
async def next_handler(message: Message):
    user_id = message.from_user.id
    
    if user_id in state.banned_users:
        await send_message(user_id, 'banMessage')
        return
    
    if user_id in state.active_pairs:
        await send_message(user_id, 'inDialogueWarning')
        return
    
    if user_id in state.waiting_queue:
        await send_message(user_id, 'queue')
        return
        
    if state.warnings.get(user_id, 0) >= MAX_WARNINGS:
        state.banned_users.add(user_id)
        await cleanup_user(user_id)
        await send_message(user_id, 'banMessage')
        return
    
    if state.waiting_queue:
        partner_id = state.waiting_queue.pop()
        await connect_users(user_id, partner_id)
    else:
        state.waiting_queue.add(user_id)
        await send_message(user_id, 'partnerFinding')


@dp.message(Command('stop'))
async def stop_handler(message: Message):
    user_id = message.from_user.id
    
    if user_id in state.banned_users:
        await send_message(user_id, 'banMessage')
        return
    
    if user_id in state.active_pairs:
        partner_id = state.active_pairs[user_id]
        await disconnect_users(user_id, partner_id)
    elif user_id in state.waiting_queue:
        state.waiting_queue.remove(user_id)


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
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    
    for reason in translation.get('reportReasons', []):
        keyboard.add(KeyboardButton(reason))
    
    await bot.send_message(
        user_id,
        translation['reportOptions'],
        reply_markup=keyboard
    )


@dp.message(F.text)
async def text_handler(message: Message):
    user_id = message.from_user.id
    text = message.text
    
    if user_id in state.banned_users:
        return
    
    if user_id in state.reporting:
        partner_id = state.reporting[user_id]
        del state.reporting[user_id]
        
        state.warnings[partner_id] = state.warnings.get(partner_id, 0) + 1
        
        if state.warnings[partner_id] >= MAX_WARNINGS:
            state.banned_users.add(partner_id)
            await cleanup_user(partner_id)
            await send_message(partner_id, 'banMessage')
            # TODO сделать баны через Middleware
            # (шутка которая обрабатывает апдейты до хандлеров и возможно отменяет обработку апдейта)
        
        await disconnect_users(user_id, partner_id)
        await send_message(user_id, 'reportSuccess')
        return
    
    if text.startswith('/'):
        return
    
    if user_id in state.active_pairs:
        partner_id = state.active_pairs[user_id]
        try:
            await bot.send_message(partner_id, text)
        except Exception as e:
            logger.error(f'Forward error: {e}')
            await disconnect_users(user_id, partner_id)
    else:
        await send_message(user_id, 'sendNext')


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())