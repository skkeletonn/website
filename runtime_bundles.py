"""Read-only delivery of short-lived Kryos runtime bundles.

The Discord bot writes documents to this collection. This website module only
looks up an artifact by ID and verifies a per-artifact token. MongoDB remains
behind the Flask service; no database credential or global API secret is sent
to Lua.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone

try:
    from pymongo import ASCENDING, MongoClient
except ImportError:  # Let non-Mongo website tooling still import the module.
    ASCENDING = 1
    MongoClient = None

logger = logging.getLogger(__name__)

# The existing website uses the vadrifts database. Runtime bundles stay in
# their own collection and can be moved to a separately scoped DB if desired.
_DB_NAME = (os.environ.get("OBF_RUNTIME_BUNDLE_DB") or "vadrifts").strip()
_COLLECTION_NAME = (
    os.environ.get("OBF_RUNTIME_BUNDLE_COLLECTION") or "obf_runtime_bundles"
).strip()

_client = None
_collection = None
_init_attempted = False
_init_lock = threading.Lock()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _get_collection():
    global _client, _collection, _init_attempted

    if _collection is not None:
        return _collection
    if _init_attempted:
        return None

    with _init_lock:
        if _collection is not None:
            return _collection
        if _init_attempted:
            return None
        _init_attempted = True

        uri = (os.environ.get("MONGODB_URI") or "").strip()
        if not uri or MongoClient is None:
            logger.warning("Runtime bundle delivery is unavailable: MongoDB is not configured")
            return None

        try:
            _client = MongoClient(
                uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=10000,
            )
            _client.admin.command("ping")
            _collection = _client[_DB_NAME][_COLLECTION_NAME]
            _collection.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
            _collection.create_index("created_at")
            logger.info(
                "Runtime bundle delivery enabled: %s.%s",
                _DB_NAME,
                _COLLECTION_NAME,
            )
            return _collection
        except Exception as exc:
            logger.warning("Could not initialize runtime bundle delivery: %s", exc)
            _client = None
            _collection = None
            return None


def authorize_runtime_bundle(artifact_id: str, access_token: str) -> bool:
    """Check an artifact token without returning the stored bundle."""
    if not artifact_id or not access_token:
        return False
    if len(artifact_id) > 128 or len(access_token) > 256:
        return False

    collection = _get_collection()
    if collection is None:
        return False

    token_hash = _sha256_text(access_token)
    try:
        document = collection.find_one(
            {"_id": str(artifact_id), "token_sha256": token_hash},
            {"expires_at": 1},
        )
    except Exception:
        logger.exception("Runtime RPC authorization failed for artifact %s", artifact_id[:12])
        return False
    if not document:
        return False

    expires_at = document.get("expires_at")
    if not isinstance(expires_at, datetime):
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < expires_at


def read_runtime_bundle_by_capability(capability: str):
    """Return a bundle using only the VM-hidden capability hash."""
    if not isinstance(capability, str) or len(capability) > 256 or len(capability) < 32:
        return None

    collection = _get_collection()
    if collection is None:
        return None
    capability_hash = _sha256_text(capability)
    try:
        document = collection.find_one(
            {"capability_sha256": capability_hash},
            {"bundle": 1, "bundle_sha256": 1, "expires_at": 1},
        )
    except Exception:
        logger.exception("Capability bundle lookup failed")
        return None
    if not document:
        return None

    expires_at = document.get("expires_at")
    if not isinstance(expires_at, datetime):
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at:
        return None

    bundle = document.get("bundle")
    if not isinstance(bundle, str) or not bundle.startswith("return {"):
        return None
    return bundle, document.get("bundle_sha256")


_CHALLENGE_TTL_SECONDS = 60


def issue_runtime_bundle_challenge(artifact_id: str):
    """Create a short-lived nonce for the experimental challenge mode."""
    if not isinstance(artifact_id, str) or not artifact_id or len(artifact_id) > 128:
        return None

    collection = _get_collection()
    if collection is None:
        return None
    now = datetime.now(timezone.utc)
    try:
        document = collection.find_one(
            {"_id": artifact_id, "kind": "kryos_runtime_bundle"},
            {"expires_at": 1, "challenge_secret": 1},
        )
    except Exception:
        logger.exception("Challenge bundle lookup failed")
        return None
    if not document or not isinstance(document.get("challenge_secret"), str):
        return None

    expires_at = document.get("expires_at")
    if not isinstance(expires_at, datetime):
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    challenge_expires_at = min(
        expires_at,
        now + timedelta(seconds=_CHALLENGE_TTL_SECONDS),
    )
    if now >= challenge_expires_at:
        return None

    nonce = secrets.token_urlsafe(24)
    challenge = {
        "_id": _sha256_text(nonce),
        "kind": "kryos_runtime_challenge",
        "artifact_id": artifact_id,
        "created_at": now,
        "expires_at": challenge_expires_at,
    }
    try:
        collection.insert_one(challenge)
    except Exception:
        logger.exception("Challenge creation failed")
        return None
    return nonce


def claim_runtime_bundle_challenge(artifact_id: str, nonce: str, mac: str):
    """Verify and atomically consume one nonce, returning the bundle."""
    if (
        not isinstance(artifact_id, str)
        or not isinstance(nonce, str)
        or not isinstance(mac, str)
        or not artifact_id
        or len(artifact_id) > 128
        or len(nonce) > 256
        or len(mac) != 64
    ):
        return None

    collection = _get_collection()
    if collection is None:
        return None
    challenge_id = _sha256_text(nonce)
    now = datetime.now(timezone.utc)
    try:
        challenge = collection.find_one(
            {
                "_id": challenge_id,
                "kind": "kryos_runtime_challenge",
                "artifact_id": artifact_id,
            },
            {"expires_at": 1},
        )
        bundle_document = collection.find_one(
            {"_id": artifact_id, "kind": "kryos_runtime_bundle"},
            {"bundle": 1, "bundle_sha256": 1, "expires_at": 1, "challenge_secret": 1},
        )
    except Exception:
        logger.exception("Challenge claim lookup failed")
        return None

    if not challenge or not bundle_document:
        return None
    challenge_expires_at = challenge.get("expires_at")
    bundle_expires_at = bundle_document.get("expires_at")
    if not isinstance(challenge_expires_at, datetime) or not isinstance(bundle_expires_at, datetime):
        return None
    if challenge_expires_at.tzinfo is None:
        challenge_expires_at = challenge_expires_at.replace(tzinfo=timezone.utc)
    if bundle_expires_at.tzinfo is None:
        bundle_expires_at = bundle_expires_at.replace(tzinfo=timezone.utc)
    if now >= challenge_expires_at or now >= bundle_expires_at:
        return None

    secret = bundle_document.get("challenge_secret")
    if not isinstance(secret, str):
        return None
    expected_mac = hmac.new(
        secret.encode("utf-8"),
        nonce.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_mac, mac.lower()):
        return None

    # The delete is the one-use gate. Concurrent valid claims race here; only
    # the request whose delete succeeds receives the bundle.
    try:
        consumed = collection.find_one_and_delete(
            {
                "_id": challenge_id,
                "kind": "kryos_runtime_challenge",
                "artifact_id": artifact_id,
            }
        )
    except Exception:
        logger.exception("Challenge consume failed")
        return None
    if not consumed:
        return None

    bundle = bundle_document.get("bundle")
    if not isinstance(bundle, str) or not bundle.startswith("return {"):
        return None
    return bundle, bundle_document.get("bundle_sha256")


def read_runtime_bundle(artifact_id: str, access_token: str):
    """Return ``(bundle, sha256)`` only for a valid, unexpired artifact."""
    if not artifact_id or not access_token:
        return None
    if len(artifact_id) > 128 or len(access_token) > 256:
        return None

    collection = _get_collection()
    if collection is None:
        return None

    # The token is never stored in plaintext. Including it in the Mongo query
    # also avoids fetching an artifact before authorization succeeds.
    token_hash = _sha256_text(access_token)
    try:
        document = collection.find_one(
            {"_id": str(artifact_id), "token_sha256": token_hash},
            {"bundle": 1, "bundle_sha256": 1, "expires_at": 1},
        )
    except Exception:
        logger.exception("Runtime bundle lookup failed for artifact %s", artifact_id[:12])
        return None

    if not document:
        return None

    expires_at = document.get("expires_at")
    if not isinstance(expires_at, datetime):
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at:
        return None

    bundle = document.get("bundle")
    bundle_sha256 = document.get("bundle_sha256")
    if not isinstance(bundle, str) or not bundle.startswith("return {"):
        logger.warning("Runtime bundle %s is malformed", artifact_id[:12])
        return None

    # Count access without making delivery depend on the counter update. The
    # counter is observability only; the token remains reusable until expiry so
    # restarting a client does not consume its artifact accidentally.
    try:
        collection.update_one(
            {"_id": str(artifact_id)},
            {"$inc": {"access_count": 1}, "$set": {"last_access_at": datetime.now(timezone.utc)}},
        )
    except Exception:
        logger.debug("Could not update runtime bundle access counter", exc_info=True)

    return bundle, bundle_sha256
