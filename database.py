from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase, AsyncIOMotorCollection
from pymongo import ReturnDocument


# на будущее
class MongoChatState:
    def __init__(self, mongo_uri, db_name):
        self.client = AsyncIOMotorClient(mongo_uri)
        self.db = self.client[db_name]
        
        self.user_settings = self.db.user_settings
        self.active_pairs = self.db.active_pairs
        self.waiting_queue = self.db.waiting_queue
        self.warnings = self.db.warnings
        self.banned_users = self.db.banned_users
        self.reports = self.db.reports
        self.invite_links = self.db.invite_links
        self.premium_users = self.db.premium_users


    async def init_indexes(self):
        if "user_id" not in await self.user_settings.index_information():
            await self.user_settings.create_index("user_id", unique=True)
            
        if "user_id" not in await self.active_pairs.index_information():
            await self.active_pairs.create_index("user_id", unique=True)
            
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
        return doc.get("language") if doc else None


    async def add_active_pair(self, user1, user2):
        await self.active_pairs.insert_many([
            {"user_id": user1, "pair_id": user2},
            {"user_id": user2, "pair_id": user1}
        ])


    async def remove_active_pair(self, user_id):
        pair = await self.active_pairs.find_one_and_delete(
            {"user_id": user_id},
            projection={"pair_id": 1}
        )
        if pair:
            await self.active_pairs.delete_one({"user_id": pair["pair_id"]})
            return pair["pair_id"]
        return None


    async def add_to_waiting(self, user_id):
        await self.waiting_queue.update_one(
            {"user_id": user_id},
            {"$setOnInsert": {"user_id": user_id}},
            upsert=True
        )


    async def remove_from_waiting(self, user_id):
        result = await self.waiting_queue.delete_one({"user_id": user_id})
        return result.deleted_count > 0


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


