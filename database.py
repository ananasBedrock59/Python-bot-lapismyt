from datetime import datetime, timedelta
import random
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import ReturnDocument, ASCENDING


class Database:
    def __init__(self, mongo_uri: str, db_name: str):
        self.client = AsyncIOMotorClient(mongo_uri)
        self.db: AsyncIOMotorDatabase = self.client[db_name]
        
        self.user_settings: AsyncIOMotorCollection = self.db.user_settings
        self.active_pairs: AsyncIOMotorCollection = self.db.active_pairs
        self.waiting_queue: AsyncIOMotorCollection = self.db.waiting_queue
        self.warnings: AsyncIOMotorCollection = self.db.warnings
        self.banned_users: AsyncIOMotorCollection = self.db.banned_users
        self.reports: AsyncIOMotorCollection = self.db.reports
        self.invite_links: AsyncIOMotorCollection = self.db.invite_links
        self.premium_users: AsyncIOMotorCollection = self.db.premium_users


    async def init_indexes(self):
        await self.waiting_queue.create_index(
            [("timestamp", ASCENDING)],
            expireAfterSeconds=300
        )

        if "user_id" not in await self.user_settings.index_information():
            await self.user_settings.create_index("user_id", unique=True)

        if "user_id" not in await self.active_pairs.index_information():
            await self.active_pairs.create_index("user_id")

        if "user_id" not in await self.waiting_queue.index_information():
            await self.waiting_queue.create_index("user_id", unique=True)

        if "user_id" not in await self.warnings.index_information():
            await self.warnings.create_index("user_id", unique=True)

        if "user_id" not in await self.banned_users.index_information():
            await self.banned_users.create_index("user_id", unique=True)

        if "report_id" not in await self.reports.index_information():
            await self.reports.create_index("report_id", unique=True)

        if "link" not in await self.invite_links.index_information():
            await self.invite_links.create_index("link", unique=True)

        if "user_id" not in await self.premium_users.index_information():
            await self.premium_users.create_index("user_id", unique=True)


    async def set_user_language(self, user_id, lang):
        await self.user_settings.update_one(
            {"user_id": user_id},
            {"$set": {"language": lang}},
            upsert=True
        )

    async def get_user_language(self, user_id):
        doc = await self.user_settings.find_one({"user_id": user_id})
        if doc:
            return doc.get("language", "en")
        return None

    async def get_active_pairs(self, user_id: int) -> list:
        return await self.active_pairs.find({"user_id": user_id}).to_list(None)

    async def get_waiting_users(self) -> list:
        return await self.waiting_queue.find().to_list(None)

    async def is_waiting(self, user_id: int) -> bool:
        return await self.waiting_queue.find_one({"user_id": user_id}) is not None

    async def create_active_pair(self, user1: int, user2: int):
        pair_id = random.randint(10000000, 99999999)
        await self.active_pairs.insert_many([
            {"user_id": user1, "pair_id": pair_id, "timestamp": datetime.now()},
            {"user_id": user2, "pair_id": pair_id, "timestamp": datetime.now()}
        ])

        await self.waiting_queue.delete_many({
            "$or": [{"user_id": user1}, {"user_id": user2}]
        })


    async def remove_active_pair(self, user_id):
        usr = await self.active_pairs.find_one_and_delete(
            {"user_id": user_id},
            projection={"pair_id": 1}
        )
        if usr:
            partner = await self.active_pairs.delete_one({"pair_id": usr["pair_id"]})
            return partner
        return None

    async def remove_active_pair_bulk(self, *user_ids):
        partners = await self.active_pairs.delete_many({"user_id": {"$in": user_ids}})
        return partners

    async def add_to_waiting(self, user_id: int):
        await self.waiting_queue.update_one(
            {"user_id": user_id},
            {"$set": {
                "user_id": user_id,
                "timestamp": datetime.now(),
                "lang": await self.get_user_language(user_id)
            }},
            upsert=True
        )

    async def pop_waiting_user(self) -> Optional[dict]:
        user = await self.waiting_queue.find_one_and_delete(
            {},
            sort=[("timestamp", ASCENDING)]
        )
        return user

    async def cleanup_inactive_pairs(self):
        cutoff = datetime.now() - timedelta(hours=24)
        pairs = await self.active_pairs.find({
            "timestamp": {"$lt": cutoff}
        }).to_list(None)

        for pair in pairs:
            await self.remove_active_pair(pair["user_id"])


    async def add_warning(self, user_id):
        doc = await self.warnings.find_one_and_update(
            {"user_id": user_id},
            {"$inc": {"count": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        return doc["count"]


    async def get_warnings(self, user_id):
        doc = await self.warnings.find_one({"user_id": user_id})
        return doc.get("count", 0) if doc else 0


    async def ban_user(self, user_id):
        await self.banned_users.update_one(
            {"user_id": user_id},
            {"$setOnInsert": {"user_id": user_id}},
            upsert=True
        )

    async def is_in_dialogue(self, user_id) -> bool:
        doc = await self.active_pairs.find_one({"user_id": user_id})
        return doc is not None


    async def get_partner_id(self, user_id: int) -> int | None:
        user_doc = await self.active_pairs.find_one(
            {"user_id": user_id},
            projection={"pair_id": 1}
        )
        if not user_doc:
            return None

        pair_id = user_doc.get("pair_id")
        if not pair_id:
            return None

        pair_user_doc = await self.active_pairs.find_one(
            {
                "pair_id": pair_id,
                "user_id": {"$ne": user_id}
            },
            projection={"user_id": 1}
        )
        return pair_user_doc.get("user_id") if pair_user_doc else None



    async def unban_user(self, user_id):
        result = await self.banned_users.delete_one({"user_id": user_id})
        return result.deleted_count > 0


    async def is_banned(self, user_id):
        doc = await self.banned_users.find_one({"user_id": user_id})
        return doc is not None


    async def add_premium(self, user_id, duration):
        await self.premium_users.update_one(
            {"user_id": user_id},
            {"$set": {"expires": duration}},
            upsert=True
        )


    async def get_premium(self, user_id):
        doc = await self.premium_users.find_one({"user_id": user_id})
        return doc.get("expires") if doc else None

    async def remove_from_waiting(self, user_id):
        result = await self.waiting_queue.delete_one({"user_id": user_id})
        return result.deleted_count > 0

