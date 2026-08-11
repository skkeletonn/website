import os
from pymongo import MongoClient

MONGODB_URI = os.environ.get("MONGODB_URI")
keys_collection = None

if MONGODB_URI:
    try:
        client = MongoClient(MONGODB_URI)
        db = client["vadrifts"]
        keys_collection = db["discord_keys"]
        client.admin.command('ping')
        print("✅ Connected to MongoDB for Discord keys")
    except Exception as e:
        print(f"❌ MongoDB Discord keys connection failed: {e}")
else:
    print("⚠️ MONGODB_URI not set for Discord keys")


def load_discord_keys():
    if keys_collection is None:
        return {}
    try:
        keys = {}
        for doc in keys_collection.find():
            key = doc["_id"]
            keys[key] = {k: v for k, v in doc.items() if k != "_id"}
        return keys
    except Exception as e:
        print(f"❌ Failed to load Discord keys: {e}")
        return {}


def save_discord_keys(keys):
    if keys_collection is None:
        return
    try:
        keys_collection.delete_many({})
        if keys:
            docs = [{"_id": k, **v} for k, v in keys.items()]
            keys_collection.insert_many(docs)
    except Exception as e:
        print(f"❌ Failed to save Discord keys: {e}")


def save_single_key(key, data):
    if keys_collection is None:
        return
    try:
        keys_collection.update_one(
            {"_id": key},
            {"$set": data},
            upsert=True
        )
    except Exception as e:
        print(f"❌ Failed to save key: {e}")


def delete_single_key(key):
    if keys_collection is None:
        return
    try:
        keys_collection.delete_one({"_id": key})
    except Exception as e:
        print(f"❌ Failed to delete key: {e}")
