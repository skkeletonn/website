import os
import time
import secrets
import string
import logging
from pymongo import MongoClient, ASCENDING

logger = logging.getLogger(__name__)

MONGODB_URI = os.environ.get("MONGODB_URI")
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL", "https://vadrifts.onrender.com")

MIN_COMPLETION_SECONDS = 25

guild_configs_collection = None
guild_sessions_collection = None
guild_keys_collection = None
script_profiles_collection = None

if MONGODB_URI:
    try:
        _client = MongoClient(MONGODB_URI)
        _db = _client["vadrifts"]
        guild_configs_collection = _db["guild_key_configs"]
        guild_sessions_collection = _db["guild_key_sessions"]
        guild_keys_collection = _db["guild_keys"]
        script_profiles_collection = _db["script_profiles"]

        guild_sessions_collection.create_index("expires_at", expireAfterSeconds=0)
        guild_sessions_collection.create_index([("guild_id", ASCENDING), ("discord_id", ASCENDING)])
        guild_sessions_collection.create_index([("guild_id", ASCENDING), ("ip", ASCENDING), ("completed", ASCENDING)])
        guild_keys_collection.create_index("expires_at")
        guild_keys_collection.create_index([("guild_id", ASCENDING), ("discord_id", ASCENDING), ("profile_id", ASCENDING)])
        guild_configs_collection.create_index("api_secret", unique=True, sparse=True)
        script_profiles_collection.create_index([("guild_id", ASCENDING), ("name", ASCENDING)], unique=True)
        script_profiles_collection.create_index("api_secret", unique=True, sparse=True)

        _client.admin.command('ping')
        logger.info("Connected to MongoDB for guild key system")
    except Exception as e:
        logger.error(f"MongoDB guild key system connection failed: {e}")
else:
    logger.warning("MONGODB_URI not set for guild key system")


def _generate_api_secret():
    return secrets.token_urlsafe(32)


def _generate_key_string():
    parts = []
    for _ in range(4):
        parts.append(''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(5)))
    return '-'.join(parts)


def _generate_profile_id():
    return ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(12))


def get_destination_url(guild_id, profile_id):
    return f"{SERVER_BASE_URL}/ks/done/{guild_id}/{profile_id}"


def get_guild_config(guild_id):
    if guild_configs_collection is None:
        return None
    try:
        doc = guild_configs_collection.find_one({"_id": str(guild_id)})
        if doc:
            config = {k: v for k, v in doc.items() if k != "_id"}
            config["guild_id"] = doc["_id"]
            return config
        return None
    except Exception as e:
        logger.error(f"Failed to get guild config: {e}")
        return None


def save_guild_config(guild_id, config_data):
    if guild_configs_collection is None:
        return False
    try:
        doc = dict(config_data)
        doc.pop("guild_id", None)
        guild_configs_collection.update_one(
            {"_id": str(guild_id)},
            {"$set": doc},
            upsert=True
        )
        return True
    except Exception as e:
        logger.error(f"Failed to save guild config: {e}")
        return False


def init_guild_config(guild_id, guild_name, admin_id):
    existing = get_guild_config(guild_id)

    config = {
        "guild_name": guild_name,
        "admin_id": str(admin_id),
        "enabled": True,
        "created_at": existing.get("created_at", time.time()) if existing else time.time(),
        "updated_at": time.time()
    }

    success = save_guild_config(guild_id, config)
    if success:
        config["guild_id"] = str(guild_id)
        return config
    return None


def delete_guild_config(guild_id):
    if guild_configs_collection is None:
        return False
    try:
        guild_configs_collection.delete_one({"_id": str(guild_id)})
        if script_profiles_collection:
            script_profiles_collection.delete_many({"guild_id": str(guild_id)})
        return True
    except Exception as e:
        logger.error(f"Failed to delete guild config: {e}")
        return False


def create_script_profile(guild_id, name, key_type, key_duration_hours=24, required_role_id=None):
    if script_profiles_collection is None:
        return None
    try:
        profile_id = _generate_profile_id()
        api_secret = _generate_api_secret()

        profile = {
            "_id": profile_id,
            "guild_id": str(guild_id),
            "name": name,
            "key_type": key_type,
            "api_secret": api_secret,
            "key_duration_hours": key_duration_hours,
            "required_role_id": str(required_role_id) if required_role_id else None,
            "workink_url": "",
            "lootlabs_url": "",
            "linkvertise_url": "",
            "require_membership": True,
            "enabled": True,
            "created_at": time.time(),
            "updated_at": time.time()
        }

        script_profiles_collection.insert_one(profile)
        profile["profile_id"] = profile_id
        return profile
    except Exception as e:
        logger.error(f"Failed to create script profile: {e}")
        return None


def get_script_profile(profile_id):
    if script_profiles_collection is None:
        return None
    try:
        doc = script_profiles_collection.find_one({"_id": profile_id})
        if doc:
            profile = {k: v for k, v in doc.items() if k != "_id"}
            profile["profile_id"] = doc["_id"]
            return profile
        return None
    except Exception as e:
        logger.error(f"Failed to get script profile: {e}")
        return None


def get_script_profiles(guild_id):
    if script_profiles_collection is None:
        return []
    try:
        profiles = []
        for doc in script_profiles_collection.find({"guild_id": str(guild_id)}):
            profile = {k: v for k, v in doc.items() if k != "_id"}
            profile["profile_id"] = doc["_id"]
            profiles.append(profile)
        return profiles
    except Exception as e:
        logger.error(f"Failed to get script profiles: {e}")
        return []


def update_script_profile(profile_id, updates):
    if script_profiles_collection is None:
        return False
    try:
        updates["updated_at"] = time.time()
        script_profiles_collection.update_one(
            {"_id": profile_id},
            {"$set": updates}
        )
        return True
    except Exception as e:
        logger.error(f"Failed to update script profile: {e}")
        return False


def delete_script_profile(profile_id):
    if script_profiles_collection is None:
        return False
    try:
        script_profiles_collection.delete_one({"_id": profile_id})
        if guild_keys_collection:
            guild_keys_collection.delete_many({"profile_id": profile_id})
        return True
    except Exception as e:
        logger.error(f"Failed to delete script profile: {e}")
        return False


def get_profile_by_secret(api_secret):
    if script_profiles_collection is None:
        return None
    try:
        doc = script_profiles_collection.find_one({"api_secret": api_secret})
        if doc:
            profile = {k: v for k, v in doc.items() if k != "_id"}
            profile["profile_id"] = doc["_id"]
            return profile
        return None
    except Exception as e:
        logger.error(f"Failed to lookup profile by secret: {e}")
        return None


def get_profile_by_name(guild_id, name):
    if script_profiles_collection is None:
        return None
    try:
        doc = script_profiles_collection.find_one({
            "guild_id": str(guild_id),
            "name": {"$regex": f"^{name}$", "$options": "i"}
        })
        if doc:
            profile = {k: v for k, v in doc.items() if k != "_id"}
            profile["profile_id"] = doc["_id"]
            return profile
        return None
    except Exception as e:
        logger.error(f"Failed to lookup profile by name: {e}")
        return None


def create_session(guild_id, discord_id, discord_name, profile_id):
    if guild_sessions_collection is None:
        return None
    try:
        guild_sessions_collection.delete_many({
            "guild_id": str(guild_id),
            "discord_id": str(discord_id),
            "profile_id": profile_id,
            "completed": False
        })

        token = secrets.token_urlsafe(32)
        now = time.time()
        session = {
            "_id": token,
            "guild_id": str(guild_id),
            "discord_id": str(discord_id),
            "discord_name": discord_name,
            "profile_id": profile_id,
            "ip": None,
            "timer_started": False,
            "timer_started_at": None,
            "completed": False,
            "completed_at": None,
            "key_claimed": False,
            "created_at": now,
            "expires_at": now + 1800
        }
        guild_sessions_collection.insert_one(session)
        return token
    except Exception as e:
        logger.error(f"Failed to create session: {e}")
        return None


def get_session(token):
    if guild_sessions_collection is None:
        return None
    try:
        doc = guild_sessions_collection.find_one({"_id": token})
        if not doc:
            return None
        if time.time() > doc.get("expires_at", 0):
            guild_sessions_collection.delete_one({"_id": token})
            return None
        session = {k: v for k, v in doc.items() if k != "_id"}
        session["token"] = doc["_id"]
        return session
    except Exception as e:
        logger.error(f"Failed to get session: {e}")
        return None


def update_session(token, updates):
    if guild_sessions_collection is None:
        return False
    try:
        guild_sessions_collection.update_one(
            {"_id": token},
            {"$set": updates}
        )
        return True
    except Exception as e:
        logger.error(f"Failed to update session: {e}")
        return False


def find_session_by_ip_and_profile(ip, guild_id, profile_id):
    if guild_sessions_collection is None:
        return None
    try:
        doc = guild_sessions_collection.find_one(
            {
                "guild_id": str(guild_id),
                "profile_id": profile_id,
                "ip": ip,
                "completed": False,
                "timer_started": True,
                "expires_at": {"$gt": time.time()}
            },
            sort=[("created_at", -1)]
        )
        if doc:
            session = {k: v for k, v in doc.items() if k != "_id"}
            session["token"] = doc["_id"]
            return session
        return None
    except Exception as e:
        logger.error(f"Failed to find session by IP: {e}")
        return None


def get_pending_session(discord_id, guild_id, profile_id):
    if guild_sessions_collection is None:
        return None
    try:
        doc = guild_sessions_collection.find_one(
            {
                "guild_id": str(guild_id),
                "discord_id": str(discord_id),
                "profile_id": profile_id,
                "completed": True,
                "key_claimed": False,
                "expires_at": {"$gt": time.time()}
            },
            sort=[("completed_at", -1)]
        )
        if doc:
            session = {k: v for k, v in doc.items() if k != "_id"}
            session["token"] = doc["_id"]
            return session
        return None
    except Exception as e:
        logger.error(f"Failed to get pending session: {e}")
        return None


def create_guild_key(guild_id, discord_id, discord_name, duration_hours, profile_id):
    if guild_keys_collection is None:
        return None
    try:
        guild_keys_collection.delete_many({
            "guild_id": str(guild_id),
            "discord_id": str(discord_id),
            "profile_id": profile_id
        })

        key = _generate_key_string()
        now = time.time()
        key_doc = {
            "_id": key,
            "guild_id": str(guild_id),
            "discord_id": str(discord_id),
            "discord_name": discord_name,
            "profile_id": profile_id,
            "hwid": None,
            "created_at": now,
            "expires_at": now + (duration_hours * 3600)
        }
        guild_keys_collection.insert_one(key_doc)
        return key
    except Exception as e:
        logger.error(f"Failed to create guild key: {e}")
        return None


def validate_guild_key(key, hwid, api_secret):
    if guild_keys_collection is None:
        return False, "Key system unavailable"

    try:
        profile = get_profile_by_secret(api_secret)
        if not profile:
            logger.warning(f"Guild key validation: no profile for secret")
            return False, "Invalid API secret"

        profile_id = profile["profile_id"]
        guild_id = profile["guild_id"]

        key_doc = guild_keys_collection.find_one({"_id": key})
        if not key_doc:
            logger.warning(f"Guild key validation: key not found")
            return False, "Invalid key"

        if key_doc.get("profile_id") != profile_id:
            logger.warning(f"Guild key validation: profile mismatch")
            return False, "Invalid key"

        if key_doc.get("guild_id") != guild_id:
            logger.warning(f"Guild key validation: guild mismatch")
            return False, "Invalid key"

        if time.time() > key_doc.get("expires_at", 0):
            guild_keys_collection.delete_one({"_id": key})
            return False, "Key expired. Get a new one from Discord."

        stored_hwid = key_doc.get("hwid")
        if stored_hwid and stored_hwid != hwid:
            return False, "Key locked to another device."

        if not stored_hwid:
            guild_keys_collection.update_one(
                {"_id": key},
                {"$set": {"hwid": hwid}}
            )

        if profile.get("require_membership", True):
            discord_id = key_doc.get("discord_id")
            if discord_id and DISCORD_TOKEN:
                try:
                    import requests as req
                    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
                    check_url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{discord_id}"
                    resp = req.get(check_url, headers=headers, timeout=10)
                    logger.info(f"Membership check: guild={guild_id} user={discord_id} status={resp.status_code}")
                    if resp.status_code == 404:
                        guild_keys_collection.delete_one({"_id": key})
                        return False, "You must be in the Discord server."
                    elif resp.status_code != 200:
                        logger.warning(f"Membership check returned {resp.status_code}, allowing through")
                except Exception as ex:
                    logger.error(f"Membership check error: {ex}")

        logger.info(f"Guild key validation: SUCCESS for key '{key[:8]}...'")
        return True, "Authenticated"

    except Exception as e:
        logger.error(f"Key validation error: {e}")
        return False, "Validation error"


def delete_guild_keys_by_user(guild_id, discord_id, profile_id=None):
    if guild_keys_collection is None:
        return 0
    try:
        query = {
            "guild_id": str(guild_id),
            "discord_id": str(discord_id)
        }
        if profile_id:
            query["profile_id"] = profile_id
        result = guild_keys_collection.delete_many(query)
        return result.deleted_count
    except Exception as e:
        logger.error(f"Failed to delete keys: {e}")
        return 0


def get_guild_key_stats(guild_id, profile_id=None):
    if guild_keys_collection is None:
        return {"total": 0, "active": 0, "expired": 0, "hwid_locked": 0}
    try:
        now = time.time()
        match_query = {"guild_id": str(guild_id)}
        if profile_id:
            match_query["profile_id"] = profile_id
        pipeline = [
            {"$match": match_query},
            {"$group": {
                "_id": None,
                "total": {"$sum": 1},
                "active": {"$sum": {"$cond": [{"$gt": ["$expires_at", now]}, 1, 0]}},
                "expired": {"$sum": {"$cond": [{"$lte": ["$expires_at", now]}, 1, 0]}},
                "hwid_locked": {"$sum": {"$cond": [{"$ne": ["$hwid", None]}, 1, 0]}}
            }}
        ]
        result = list(guild_keys_collection.aggregate(pipeline))
        if result:
            r = result[0]
            return {
                "total": r.get("total", 0),
                "active": r.get("active", 0),
                "expired": r.get("expired", 0),
                "hwid_locked": r.get("hwid_locked", 0)
            }
        return {"total": 0, "active": 0, "expired": 0, "hwid_locked": 0}
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        return {"total": 0, "active": 0, "expired": 0, "hwid_locked": 0}


def cleanup_expired_guild_keys():
    if guild_keys_collection is None:
        return 0
    try:
        result = guild_keys_collection.delete_many({
            "expires_at": {"$lte": time.time()}
        })
        return result.deleted_count
    except Exception as e:
        logger.error(f"Failed to cleanup keys: {e}")
        return 0
