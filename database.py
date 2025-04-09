import random
from datetime import datetime, timedelta
from typing import Optional, Literal

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import ASCENDING


class Database:
    def __init__(self, mongo_uri: str, db_name: str, client: AsyncIOMotorClient | None = None):
        self.client = AsyncIOMotorClient(mongo_uri) or client
        self.db: AsyncIOMotorDatabase = self.client[db_name]

        self.users: AsyncIOMotorCollection = self.db.users
        self.active_pairs: AsyncIOMotorCollection = self.db.active_pairs
        self.waiting_queue: AsyncIOMotorCollection = self.db.waiting_queue
        self.reports: AsyncIOMotorCollection = self.db.reports


    async def init_indexes(self):
        if not 'user_id' in await self.users.index_information():
            await self.users.create_index("user_id", unique=True)
        if not 'timestamp' in await self.waiting_queue.index_information():
            await self.waiting_queue.create_index(
                [("timestamp", ASCENDING)],
                expireAfterSeconds=300
            )
        if not 'last_activity' in await self.active_pairs.index_information():
            await self.active_pairs.create_index(
                [("last_activity", ASCENDING)],
                expireAfterSeconds=86400
            )
        if not 'user1_id' in await self.active_pairs.index_information():
            await self.active_pairs.create_index("user1_id")
        if not 'user2_id' in await self.active_pairs.index_information():
            await self.active_pairs.create_index("user2_id")
        if not 'pair_id' in await self.active_pairs.index_information():
            await self.active_pairs.create_index("pair_id", unique=True)


    async def get_user(self, user_id: int) -> Optional[dict]:
        return await self.users.find_one({"user_id": user_id})


    async def update_user(self, user_id: int, update_data: dict):
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": update_data},
            upsert=True
        )


    async def set_user_language(self, user_id: int, lang: str):
        await self.update_user(user_id, {"language": lang})


    async def get_user_language(self, user_id: int):
        user = await self.get_user(user_id)
        if user:
            return user.get("language", "en")
        return None


    async def get_pair(self, user_id: int) -> Optional[dict]:
        return await self.active_pairs.find_one({
            "$or": [
                {"user1_id": user_id},
                {"user2_id": user_id}
            ],
            "status": "active"
        })


    async def get_waiting_users(self) -> list:
        return await self.waiting_queue.find().to_list(None)


    async def is_waiting(self, user_id: int) -> bool:
        return await self.waiting_queue.find_one({"user_id": user_id}) is not None

    async def create_pair(self, user1: int, user2: int) -> int:
        pair_id = random.randint(1_000_000, 9_999_999)
        now = datetime.now().timestamp()

        await self.active_pairs.insert_one({
            "user1_id": min(user1, user2),
            "user2_id": max(user1, user2),
            "pair_id": pair_id,
            "created_at": now,
            "last_activity": now,
            "status": "active"
        })
        return pair_id


    async def delete_pair(self, pair_id: int):
        await self.active_pairs.delete_one({"pair_id": pair_id})

    async def update_pair_activity(self, user_id: int):
        pair = await self.get_pair(user_id)
        if pair:
            await self.active_pairs.update_one(
                {"pair_id": pair["pair_id"]},
                {"$set": {"last_activity": datetime.now().timestamp()}}
            )

    async def end_pair(self, pair_id: int, status: str = "ended"):
        await self.active_pairs.update_one(
            {"pair_id": pair_id},
            {"$set": {"status": status}}
        )


    async def add_user(self, user_id: int, lang: str):
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {
                "user_id": user_id,
                "language": lang,
                "warnings": 0,
                "premium_expires": None,
                "banned": False,
                "created_at": datetime.now().timestamp(),
                "last_active": datetime.now().timestamp()
            }},
            upsert=True
        )


    async def add_to_waiting(self, user_id: int, lang: str):
        await self.waiting_queue.update_one(
            {"user_id": user_id},
            {"$set": {
                "user_id": user_id,
                "timestamp": datetime.now(),
                "lang": lang
            }},
            upsert=True
        )


    async def update_user_activity(self, user_id: int):
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"last_active": datetime.now()}}
        )
        return await self.check_premium(user_id)

    async def cleanup_old_pairs(self):
        cutoff = datetime.now() - timedelta(hours=24)
        await self.active_pairs.delete_many({
            "last_activity": {"$lt": cutoff}
        })

    async def add_warning(self, user_id: int):
        result = await self.users.update_one(
            {"user_id": user_id},
            {"$inc": {"warnings": 1}},
            upsert=True
        )
        user = await self.get_user(user_id)
        return user.get("warnings", 0)


    async def is_existing_user(self, user_id):
        return await self.get_user(user_id) is not None


    async def get_user_count(self):
        return await self.users.count_documents({})


    async def get_premium_user_count(self):
        return await self.users.count_documents({"premium_expires": {"$gt": datetime.now().timestamp()}})

    async def get_warnings(self, user_id: int):
        user = await self.get_user(user_id)
        return user.get("warnings", 0) if user else 0

    async def get_all_users(self):
        cursor = self.users.find({}, {"user_id": 1})
        users = await cursor.to_list(length=None)
        return [user["user_id"] for user in users]

    async def ban_user(self, user_id: int):
        await self.update_user(user_id, {'banned': True})

    async def is_in_dialogue(self, user_id: int) -> bool:
        return await self.get_pair(user_id) is not None


    async def get_partner_id(self, user_id: int) -> Optional[int]:
        pair = await self.get_pair(user_id)
        if not pair:
            return None
        return pair["user2_id"] if pair["user1_id"] == user_id else pair["user1_id"]



    async def unban_user(self, user_id):
        await self.update_user(user_id, {"banned": False})

    async def is_banned(self, user_id: int) -> bool:
        user = await self.get_user(user_id)
        return user.get("banned", False) if user else False


    async def add_premium(self, user_id: int, duration: int):
        user = await self.get_user(user_id)
        if user and user.get("premium_expires", None) is not None and datetime.fromtimestamp(user.get("premium_expires")) > datetime.now():
            expire_time = datetime.fromtimestamp(user.get("premium_expires"))
        else:
            expire_time = datetime.now()
        expire_time = expire_time + timedelta(seconds=duration)
        await self.update_user(user_id, {"premium_expires": expire_time.timestamp()})


    async def remove_premium(self, user_id: int):
        await self.update_user(user_id, {"premium_expires": None})

    async def check_premium(self, user_id: int) -> Literal['no', 'expired', 'yes']:
        user = await self.get_user(user_id)
        if not user or user.get('premium_expires', None) is None:
            return 'no'

        if datetime.fromtimestamp(user["premium_expires"]) < datetime.now():
            await self.update_user(user_id, {"premium_expires": None})
            return 'expired'
        return 'yes'


    async def get_premium(self, user_id: int):
        user = await self.get_user(user_id)
        return user.get("premium_expires") if user else None

    async def remove_from_waiting(self, user_id: int):
        result = await self.waiting_queue.delete_one({"user_id": user_id})
        return result.deleted_count > 0

