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
OWNER_IDS: list[int] = list(map(int, os.getenv('OWNER_IDS', '').split(','))) if os.getenv('OWNER_IDS') else []
DB_NAME: str = os.getenv('DB_NAME') or 'anonbot'
SEARCH_TIMEOUT: int = int(os.getenv('SEARCH_TIMEOUT') or 5)
REPORT_LOGGING_CHAT: int = int(os.getenv('REPORT_LOGGING_CHAT'))


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


async def cleanup_user(user_id: int):
    pair = await db.get_pair(user_id)
    if pair:
        partner_id = await db.get_partner_id(user_id)
        await disconnect_users(user_id, partner_id)

    await db.remove_from_waiting(user_id)
    await db.ban_user(user_id)
    await db.update_user(user_id, {"warnings": MAX_WARNINGS})

    state = dp.fsm.get_context(bot, user_id, user_id)
    await state.clear()
    logger.info(f"User {user_id} cleaned up")


async def find_partner(user_data: dict):
    user_id = user_data["user_id"]
    user_lang = user_data.get("language", "en")

    await db.remove_from_waiting(user_id)
    existing_users = await db.get_waiting_users()

    partner = None
    for candidate in existing_users:
        if candidate["user_id"] != user_id and candidate.get("lang") == user_lang:
            partner = candidate
            break

    if partner:
        await db.remove_from_waiting(user_id)
        await db.remove_from_waiting(partner["user_id"])
        await connect_users(user_id, partner["user_id"])
    else:
        await db.add_to_waiting(user_id, user_lang)
        await send_message(user_id, "partnerFinding")
        state = dp.fsm.get_context(bot, user_id, user_id)
        await state.set_state(PartnerStates.searching)
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


async def send_message(user_id: int, key: str, reply_markup = None, **kwargs):
    try:
        translation = await get_translation(user_id)
        message = translation.get(key, translations['en'].get(key, key))
        if message:
            await bot.send_message(user_id, message.format(**kwargs), reply_markup=reply_markup)
    except (TelegramForbiddenError, TelegramNotFound):
        logger.info(f'User {user_id} blocked the bot or chat not found')
        await cleanup_user(user_id)
    except Exception as e:
        logger.error(f'Error sending to {user_id}: {e}')


async def connect_users(user1_id: int, user2_id: int):
    pair_id = await db.create_pair(user1_id, user2_id)
    for user_id in (user1_id, user2_id):
        state = dp.fsm.get_context(bot, user_id, user_id)
        await send_message(user_id, 'partnerFind')
        await state.set_state(PartnerStates.in_dialogue)


async def disconnect_users(user1_id: int, user2_id: int):
    pair = await db.get_pair(user1_id)
    if pair:
        await db.end_pair(pair["pair_id"])
        for user_id in (user1_id, user2_id):
            state = dp.fsm.get_context(bot, user_id, user_id)
            await state.clear()
        await send_message(user1_id, 'skipDialogue')
        await send_message(user2_id, 'partnerSkipDialogue')



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
        await db.add_user(message.from_user.id, message.from_user.language_code or 'en')
        if message.from_user.id in OWNER_IDS:
            ten_years = 10 * 365 * 24 * 60 * 60
            await db.add_premium(message.from_user.id, duration=ten_years)
    premium = await db.update_user_activity(message.from_user.id)
    if premium == 'expired':
        await send_message(message.from_user.id, 'premiumExpired')
    return await handler(message, data)


@dp.message(CommandStart())
async def start_handler(message: Message):
    await send_message(message.from_user.id, 'hello')


@dp.message(Command('help'))
async def help_handler(message: Message):
    await send_message(message.from_user.id, 'helpText')


@dp.message(Command('next'))
async def next_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = await db.get_user(user_id)

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

    await find_partner(user_data)


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
    if message.from_user.id not in OWNER_IDS:
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


@dp.message(PartnerStates.in_dialogue)
async def in_dialogue_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text

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

    if text is not None and text.startswith('/'):
        return

    partner_id = await db.get_partner_id(user_id)
    if partner_id:
        try:
            await message.copy_to(partner_id)
        except Exception as e:
            logger.error(f'Forward error: {e}')
            await disconnect_users(user_id, partner_id)
    else:
        await send_message(user_id, 'sendNext')

@dp.message(F.text)
async def text_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text
    await send_message(user_id, 'sendNext')


async def main():
    await db.init_indexes()
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    logger.info(f'Bot started: @{me.username} (ID {me.id})')
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())