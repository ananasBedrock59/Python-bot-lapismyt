import asyncio
import json
import os
from typing import Any, Dict, Callable, Awaitable

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNotFound
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton
from aiogram.types import Message
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from dotenv import load_dotenv
from loguru import logger

from database import Database

load_dotenv()

BOT_TOKEN: str = os.getenv('BOT_TOKEN')
MAX_WARNINGS: int = int(os.getenv('MAX_WARNINGS') or 10)
CLEANUP_INTERVAL: int = int(os.getenv('CLEANUP_INTERVAL') or 3600)
DB_URI: str = os.getenv('DB_URI') or 'mongodb://localhost:27017'
OWNER_ID: int = int(os.getenv('OWNER_ID') or 1938789303)
DB_NAME: str = os.getenv('DB_NAME') or 'anonbot'
SEARCH_TIMEOUT: int = int(os.getenv('SEARCH_TIMEOUT') or 5)


with open('lang.json') as f:
    translations = json.load(f)


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

db = Database(DB_URI, DB_NAME)

reporting = {}


class PartnerStates(StatesGroup):
    searching = State()
    in_dialogue = State()
    reporting = State()


class AdminStates(StatesGroup):
    waiting_for_mail = State()


async def find_partner(user_id: int, state: FSMContext):
    waiting_user = await db.pop_waiting_user()
    if waiting_user:
        partner_id = waiting_user['user_id']
        await db.create_active_pair(user_id, partner_id)
        await state.set_state(PartnerStates.in_dialogue)
        await connect_users(user_id, partner_id)
    else:
        await db.add_to_waiting(user_id)
        await state.set_state(PartnerStates.searching)
        await send_message(user_id, 'partnerFinding')
        await start_search_monitoring(user_id, state)


async def start_search_monitoring(user_id: int, state: FSMContext):
    async def check_status():
        for _ in range(60):  # 60 attempts with 5 sec interval = 5 minutes
            await asyncio.sleep(SEARCH_TIMEOUT)
            if not await db.is_waiting(user_id):
                return
            if await db.is_banned(user_id):
                await cleanup_user(user_id)
                return
        await db.remove_from_waiting(user_id)
        await send_message(user_id, 'searchTimeout')
        await state.clear()

    asyncio.create_task(check_status())


async def get_translation(user_id: int) -> dict:
    lang = await db.get_user_language(user_id) or 'en'
    return translations.get(lang, translations['en'])


async def send_message(user_id: int, key: str, **kwargs):
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
    await db.create_active_pair(user1_id, user2_id)
    user1_state = dp.fsm.get_context(bot, user1_id, user1_id)
    await user1_state.set_state(PartnerStates.in_dialogue)
    user2_state = dp.fsm.get_context(bot, user2_id, user2_id)
    await user2_state.set_state(PartnerStates.in_dialogue)
    await send_message(user1_id, 'partnerFind')
    await send_message(user2_id, 'partnerFind')


async def disconnect_users(user1_id: int, user2_id: int):
    await db.remove_active_pair_bulk(user1_id, user2_id)
    user1_state = dp.fsm.get_context(bot, user1_id, user1_id)
    await user1_state.set_state(PartnerStates.in_dialogue)
    user2_state = dp.fsm.get_context(bot, user2_id, user2_id)
    await user2_state.set_state(PartnerStates.in_dialogue)
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


@dp.message.middleware()
async def message_middleware(
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        message: Message,
        data: Dict[str, Any]
    ) -> Any:
    if await db.is_banned(message.from_user.id):
        await send_message(message.from_user.id, 'banMessage')
        return None
    if not await db.is_existing_user(message.from_user.id):
        await db.set_user_language(message.from_user.id, message.from_user.language_code or 'en')
    return await handler(message, data)



@dp.message(CommandStart())
async def start_handler(message: Message):
    user_id = message.from_user.id

    if await db.is_banned(user_id):
        await send_message(user_id, 'banMessage')
        return
    
    lang = message.from_user.language_code or 'en'
    await db.set_user_language(user_id, lang)
    await send_message(user_id, 'hello')


@dp.message(Command('help'))
async def help_handler(message: Message):
    await send_message(message.from_user.id, 'helpText')


@dp.message(Command('next'))
async def next_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id

    if await db.is_banned(user_id):
        await send_message(user_id, 'banMessage')
        return

    if await db.is_in_dialogue(user_id):
        await send_message(user_id, 'inDialogueWarning')
        return

    warnings = await db.get_warnings(user_id)
    if warnings >= MAX_WARNINGS:
        await db.ban_user(user_id)
        await cleanup_user(user_id)
        await send_message(user_id, 'banMessage')
        return

    await find_partner(user_id, state)


@dp.message(Command('stop'))
async def stop_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id

    if await db.is_banned(user_id):
        await send_message(user_id, 'banMessage')
        return

    partner_id = await db.get_partner_id(int(user_id))
    current_state = await state.get_state()
    if current_state == PartnerStates.in_dialogue:
        partner_state = dp.fsm.get_context(bot, int(partner_id), int(partner_id))
        await partner_state.clear()
        await state.clear()
        await disconnect_users(user_id, partner_id)
    elif current_state == PartnerStates.searching:
        await db.remove_from_waiting(user_id)
        await send_message(user_id, 'searchStopped')
        await state.clear()
    else:
        await send_message(user_id, 'noActiveDialogue')


@dp.message(Command('report'))
async def report_handler(message: Message):
    user_id = message.from_user.id
    
    if not await db.is_in_dialogue(user_id):
        await send_message(user_id, 'dialogueHaveError')
        return
    
    partner_id = await db.get_partner_id(user_id)
    reporting[user_id] = partner_id
    
    translation = await get_translation(user_id)
    keyboard = ReplyKeyboardBuilder()
    
    for reason in translation.get('reportReasons', []):
        keyboard.add(KeyboardButton(text=reason))
    
    await bot.send_message(
        user_id,
        translation['reportOptions'],
        reply_markup=keyboard.as_markup(resize_keyboard=True, one_time_keyboard=True)
    )


@dp.message(Command('stats'))
async def stats_command(message: Message):
    user_count = await db.get_user_count()
    premium_count = await db.get_premium_user_count()
    await send_message(
        message.from_user.id,
        'stats',
        user_count=user_count,
        premium_count=premium_count
    )


@dp.message(Command('mail'))
async def mail_command(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        await send_message(message.from_user.id, 'permissionDenied')
        return
    await state.set_state(AdminStates.waiting_for_mail)
    await send_message(message.from_user.id, 'sendMailingMessage')


@dp.message(AdminStates.waiting_for_mail)
async def process_mailing_message(message: Message, state: FSMContext):
    user_ids = await db.get_all_users()
    success = 0
    failed = 0

    for user_id in user_ids:
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=message.reply_markup
            )
            success += 1
        except (TelegramForbiddenError, TelegramNotFound):
            failed += 1
        except Exception as e:
            logger.error(f"Ошибка рассылки для {user_id}: {e}")
            failed += 1
        await asyncio.sleep(0.1)
        if not success + failed % 100:
            await send_message(
                message.from_user.id,
                'mailingProgress',
                success=success,
                failed=failed
            )
            await asyncio.sleep(0.1)

    await state.clear()
    await send_message(
        message.from_user.id,
        'mailingResult',
        success=success,
        failed=failed
    )


@dp.message(F.text)
async def text_handler(message: Message, state: FSMContext):
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
        partner_id = await db.get_partner_id(user_id)
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
    me = await bot.get_me()
    logger.info(f'Bot started: @{me.username} (ID {me.id})')
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())