"""Durable guild service access and four-step LootLabs renewals.

This module is intentionally separate from guild_key_system.py.  The Discord bot
and website are separate deployments, but both point at the same MongoDB
collections and use this same document/state-machine contract.
"""

import hashlib
import html
import logging
import os
import re
import secrets
import smtplib
import time
from contextlib import nullcontext
from datetime import datetime, time as datetime_time, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
try:
    from pymongo import ASCENDING, MongoClient
    from pymongo.errors import DuplicateKeyError
except ImportError:  # Pure state/date tests do not need the database driver.
    ASCENDING = 1
    MongoClient = None

    class DuplicateKeyError(Exception):
        pass

logger = logging.getLogger(__name__)

MONGODB_URI = os.environ.get("MONGODB_URI")
SERVER_BASE_URL = (
    os.environ.get("SERVER_BASE_URL") or "https://vadrifts.onrender.com"
).strip().rstrip("/")
OWNER_GUILD_ID = (os.environ.get("OWNER_GUILD_ID") or "1241797935100989594").strip()


def is_renewal_exempt(guild_id):
    """Owner guild skips the paid cycle. Guild id comes from the server, never the client."""
    return str(guild_id or "") == OWNER_GUILD_ID

RENEWAL_PERIOD_DAYS = 3


def _nonnegative_int_env(name, default=0):
    raw = (os.environ.get(name) or str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; expected a whole number", name, raw)
        return default
    return max(0, value)


# Optional operator-only timing override for staging/smoke tests. Leaving these
# unset preserves the production three-local-day cycle and 30-minute grace.
RENEWAL_TEST_CYCLE_MINUTES = _nonnegative_int_env(
    "RENEWAL_TEST_CYCLE_MINUTES"
)
RENEWAL_TEST_GRACE_MINUTES = _nonnegative_int_env(
    "RENEWAL_TEST_GRACE_MINUTES"
)
GRACE_PERIOD_SECONDS = (
    RENEWAL_TEST_GRACE_MINUTES * 60
    if RENEWAL_TEST_CYCLE_MINUTES and RENEWAL_TEST_GRACE_MINUTES
    else 30 * 60
)
CHECKPOINT_COUNT = 4
RENEWAL_OPEN_SECONDS = 24 * 60 * 60
RENEWAL_SESSION_SECONDS = 6 * 60 * 60
MIN_CHECKPOINT_SECONDS = int(os.environ.get("RENEWAL_MIN_CHECKPOINT_SECONDS", "25"))
EMAIL_VERIFY_SECONDS = 15 * 60
EMAIL_VERIFY_ATTEMPTS = 5
EMAIL_RESEND_SECONDS = 45

if RENEWAL_TEST_CYCLE_MINUTES:
    logger.warning(
        "RENEWAL TEST TIMING ENABLED: cycle=%sm grace=%sm; remove the test variables before production",
        RENEWAL_TEST_CYCLE_MINUTES,
        GRACE_PERIOD_SECONDS // 60,
    )

LOOTLABS_API_URL = "https://creators.lootlabs.gg/api/public/content_locker"
BREVO_EMAIL_API_URL = "https://api.brevo.com/v3/smtp/email"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "aol.com",
    "proton.me", "protonmail.com", "gmx.com", "gmx.net", "mail.com",
    "yandex.com", "zoho.com",
}

renewal_entitlements_collection = None
renewal_sessions_collection = None
renewal_notifications_collection = None
renewal_email_verifications_collection = None
_email_missing_logged = False

if MONGODB_URI and MongoClient is not None:
    try:
        _client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
        )
        _db = _client["vadrifts"]
        renewal_entitlements_collection = _db["guild_renewal_entitlements"]
        renewal_sessions_collection = _db["guild_renewal_sessions"]
        renewal_notifications_collection = _db["guild_renewal_notifications"]
        renewal_email_verifications_collection = _db["guild_renewal_email_verifications"]

        renewal_entitlements_collection.create_index(
            [("enabled", ASCENDING), ("due_at", ASCENDING)]
        )
        renewal_sessions_collection.create_index(
            "expires_at_ttl", expireAfterSeconds=0
        )
        renewal_sessions_collection.create_index(
            [("guild_id", ASCENDING), ("admin_discord_id", ASCENDING), ("completed", ASCENDING)]
        )
        renewal_notifications_collection.create_index(
            "expires_at_ttl", expireAfterSeconds=0
        )
        renewal_email_verifications_collection.create_index(
            "expires_at_ttl", expireAfterSeconds=0
        )
        logger.info("Guild renewal collections initialized")
    except Exception as exc:
        logger.error("Guild renewal MongoDB initialization failed: %s", exc)
elif MONGODB_URI:
    logger.error("pymongo is required when MONGODB_URI is configured")
else:
    logger.warning("MONGODB_URI not set for guild renewal system")


def validate_renewal_settings(email, timezone_name, local_time):
    """Normalize and validate owner-entered renewal settings."""
    email = (email or "").strip().lower()
    timezone_name = (timezone_name or "").strip()
    local_time = (local_time or "").strip()

    if email and (len(email) > 254 or not _EMAIL_RE.fullmatch(email)):
        raise ValueError("Enter a valid notification email address, or leave it blank.")
    if not _TIME_RE.fullmatch(local_time):
        raise ValueError("Renewal time must use 24-hour HH:MM format, for example 18:30.")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            "Enter a valid IANA timezone, for example UTC or America/New_York."
        )
    return email, timezone_name, local_time


def _time_parts(local_time):
    hour, minute = local_time.split(":", 1)
    return int(hour), int(minute)


def _local_wall_timestamp(local_date, timezone_name, local_time):
    """Turn a local date/time into UTC seconds, normalizing DST gaps forward."""
    tz = ZoneInfo(timezone_name)
    hour, minute = _time_parts(local_time)
    wall = datetime.combine(
        local_date, datetime_time(hour=hour, minute=minute), tzinfo=tz
    )
    timestamp = wall.timestamp()
    normalized = datetime.fromtimestamp(timestamp, tz)
    if (
        normalized.date() != local_date
        or normalized.hour != hour
        or normalized.minute != minute
    ):
        # zoneinfo maps a nonexistent DST wall time through the gap.  The
        # round-tripped value is the first real local time with that mapping.
        timestamp = normalized.timestamp()
    return timestamp


def renewal_schedule_description():
    if RENEWAL_TEST_CYCLE_MINUTES:
        return (
            f"{RENEWAL_TEST_CYCLE_MINUTES}-minute test cycle with a "
            f"{GRACE_PERIOD_SECONDS // 60}-minute grace period"
        )
    return "three-local-calendar-day cycle with a 30-minute grace period"


def compute_first_due(now, timezone_name, local_time):
    """First due date: production local-calendar timing or an explicit test offset."""
    now = float(now)
    if RENEWAL_TEST_CYCLE_MINUTES:
        return now + RENEWAL_TEST_CYCLE_MINUTES * 60
    local_now = datetime.fromtimestamp(now, ZoneInfo(timezone_name))
    target_date = local_now.date() + timedelta(days=RENEWAL_PERIOD_DAYS)
    return _local_wall_timestamp(target_date, timezone_name, local_time)


def compute_renewed_due(entitlement, now):
    """Advance an on-time cycle from its anchor; restart a long-blocked cycle."""
    now = float(now)
    timezone_name = entitlement["timezone"]
    local_time = entitlement["local_time"]
    due_at = float(entitlement["due_at"])
    grace_ends_at = float(
        entitlement.get("grace_ends_at", due_at + GRACE_PERIOD_SECONDS)
    )

    if RENEWAL_TEST_CYCLE_MINUTES:
        anchor = due_at if now < grace_ends_at else now
        return anchor + RENEWAL_TEST_CYCLE_MINUTES * 60

    tz = ZoneInfo(timezone_name)
    if now < grace_ends_at:
        anchor_date = datetime.fromtimestamp(due_at, tz).date()
    else:
        anchor_date = datetime.fromtimestamp(now, tz).date()
    target_date = anchor_date + timedelta(days=RENEWAL_PERIOD_DAYS)
    return _local_wall_timestamp(target_date, timezone_name, local_time)


def derive_renewal_status(entitlement, now=None):
    """Pure state derivation used by both runtime code and focused tests."""
    now = time.time() if now is None else float(now)
    if not entitlement or not entitlement.get("enabled"):
        return {
            "configured": False,
            "state": "legacy",
            "allows_access": True,
            "renewal_available": False,
            "message": "Service renewal is not configured; legacy access remains active.",
        }

    due_at = float(entitlement.get("due_at", 0))
    grace_ends_at = float(
        entitlement.get("grace_ends_at", due_at + GRACE_PERIOD_SECONDS)
    )
    if now < due_at:
        state = "active"
        allows_access = True
    elif now < grace_ends_at:
        state = "grace"
        allows_access = True
    else:
        state = "blocked"
        allows_access = False

    stored_email = entitlement.get("email", "")
    verified_flag = entitlement.get("email_verified")
    if verified_flag is None:
        email_verified = bool(stored_email)
    else:
        email_verified = bool(verified_flag)

    result = {
        "configured": True,
        "state": state,
        "allows_access": allows_access,
        "renewal_available": now >= due_at - RENEWAL_OPEN_SECONDS,
        "renewal_opens_at": due_at - RENEWAL_OPEN_SECONDS,
        "due_at": due_at,
        "grace_ends_at": grace_ends_at,
        "cycle": int(entitlement.get("cycle", 1)),
        "timezone": entitlement.get("timezone", "UTC"),
        "local_time": entitlement.get("local_time", "00:00"),
        "email": stored_email,
        "email_verified": email_verified,
        "guild_name": entitlement.get("guild_name", "Discord server"),
        "owner_discord_id": entitlement.get("owner_discord_id"),
    }
    if state == "active":
        result["message"] = "Service access is active."
    elif state == "grace":
        grace_minutes = GRACE_PERIOD_SECONDS // 60
        result["message"] = (
            f"Service access is in its {grace_minutes}-minute grace period."
        )
    else:
        result["message"] = (
            "Service access expired. A server admin must complete the four renewal checkpoints."
        )
    return result


def get_renewal_entitlement(guild_id):
    if renewal_entitlements_collection is None:
        return None
    return renewal_entitlements_collection.find_one({"_id": str(guild_id)})


def get_renewal_status(guild_id, now=None):
    """Get dynamic access state. Missing records deliberately keep legacy access."""
    if is_renewal_exempt(guild_id):
        return {
            "configured": True,
            "state": "exempt",
            "allows_access": True,
            "renewal_available": False,
            "exempt": True,
            "message": "This server has permanent service access.",
            "cycle": 0,
            "timezone": "UTC",
            "local_time": "00:00",
            "email": "",
            "email_verified": False,
            "guild_name": "Discord server",
            "owner_discord_id": None,
        }
    if renewal_entitlements_collection is None:
        if MONGODB_URI:
            return {
                "configured": True,
                "state": "unavailable",
                "allows_access": False,
                "renewal_available": False,
                "message": "Service access could not be checked. Please try again shortly.",
            }
        return derive_renewal_status(None, now)
    try:
        return derive_renewal_status(get_renewal_entitlement(guild_id), now)
    except Exception as exc:
        logger.error("Failed to read renewal state for guild %s: %s", guild_id, exc)
        return {
            "configured": True,
            "state": "unavailable",
            "allows_access": False,
            "renewal_available": False,
            "message": "Service access could not be checked. Please try again shortly.",
        }


def email_already_verified(guild_id, email):
    """True when this guild already owns this address as a verified reminder inbox."""
    email = (email or "").strip().lower()
    entitlement = get_renewal_entitlement(guild_id)
    if not entitlement or not email:
        return False
    stored = (entitlement.get("email") or "").strip().lower()
    if stored != email:
        return False
    flag = entitlement.get("email_verified")
    if flag is None:
        return True
    return bool(flag)


def get_pending_email_verification(guild_id, now=None):
    if renewal_email_verifications_collection is None:
        return None
    now = time.time() if now is None else float(now)
    doc = renewal_email_verifications_collection.find_one({"_id": str(guild_id)})
    if not doc:
        return None
    if now >= float(doc.get("expires_at", 0)):
        renewal_email_verifications_collection.delete_one({"_id": str(guild_id)})
        return None
    return {
        "email": doc.get("email", ""),
        "timezone": doc.get("timezone", "UTC"),
        "local_time": doc.get("local_time", "18:00"),
        "expires_at": float(doc.get("expires_at", 0)),
        "attempts": int(doc.get("attempts", 0)),
        "last_sent_at": float(doc.get("last_sent_at", 0)),
    }


def _code_digest(guild_id, code):
    secret = (
        os.environ.get("DISCORD_KEY_API_SECRET")
        or os.environ.get("BREVO_API_KEY")
        or "vadrifts-email-verify"
    )
    return hashlib.sha256(f"{secret}:{guild_id}:{code}".encode("utf-8")).hexdigest()


def request_email_verification(
    guild_id,
    guild_name,
    owner_discord_id,
    email,
    timezone_name,
    local_time,
    now=None,
    force_resend=False,
):
    """Store pending settings and email a one-time 6-digit code."""
    if renewal_email_verifications_collection is None:
        raise RuntimeError("Renewal database is unavailable.")
    if not _email_settings():
        raise RuntimeError(
            "Email delivery is not configured. Set BREVO_API_KEY and BREVO_FROM_EMAIL."
        )

    email, timezone_name, local_time = validate_renewal_settings(
        email, timezone_name, local_time
    )
    if not email:
        raise ValueError("Enter an email to verify, or leave it blank to use Discord DMs only.")
    now = time.time() if now is None else float(now)
    guild_id = str(guild_id)
    existing = renewal_email_verifications_collection.find_one({"_id": guild_id})
    last_sent = float((existing or {}).get("last_sent_at", 0))
    if existing and not force_resend and now - last_sent < EMAIL_RESEND_SECONDS:
        wait = int(EMAIL_RESEND_SECONDS - (now - last_sent))
        raise ValueError(f"Wait {wait}s before requesting another code.")

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = now + EMAIL_VERIFY_SECONDS
    document = {
        "guild_id": guild_id,
        "guild_name": (guild_name or "Discord server")[:200],
        "owner_discord_id": str(owner_discord_id),
        "email": email,
        "timezone": timezone_name,
        "local_time": local_time,
        "code_hash": _code_digest(guild_id, code),
        "attempts": 0,
        "created_at": float((existing or {}).get("created_at", now)),
        "last_sent_at": now,
        "expires_at": expires_at,
        "expires_at_ttl": datetime.fromtimestamp(expires_at, timezone.utc),
    }
    renewal_email_verifications_collection.update_one(
        {"_id": guild_id}, {"$set": document}, upsert=True
    )

    subject = "Verify your Vadrifts service email"
    text_body = (
        f"Use this code to confirm the reminder email for {document['guild_name']}:\n\n"
        f"    {code}\n\n"
        "It expires in 15 minutes. If you did not request this, ignore the email.\n"
    )
    html_body = _html_email(
        "Confirm your reminder email",
        [
            f"Use this code to confirm the reminder inbox for {document['guild_name']}.",
            "It expires in 15 minutes. If you did not request this, you can ignore the email.",
        ],
        code=code,
    )
    _send_email(email, subject, text_body, html_body=html_body)
    return {
        "email": email,
        "expires_in": EMAIL_VERIFY_SECONDS,
        "from_is_freemail": sender_is_freemail(),
    }


def confirm_email_verification(guild_id, code, now=None):
    """Apply pending settings after the owner proves they control the inbox."""
    if renewal_email_verifications_collection is None:
        raise RuntimeError("Renewal database is unavailable.")
    now = time.time() if now is None else float(now)
    guild_id = str(guild_id)
    pending = renewal_email_verifications_collection.find_one({"_id": guild_id})
    if not pending or now >= float(pending.get("expires_at", 0)):
        if pending:
            renewal_email_verifications_collection.delete_one({"_id": guild_id})
        raise ValueError("That code expired. Submit your email again to get a new one.")

    attempts = int(pending.get("attempts", 0))
    if attempts >= EMAIL_VERIFY_ATTEMPTS:
        renewal_email_verifications_collection.delete_one({"_id": guild_id})
        raise ValueError("Too many incorrect attempts. Request a new code.")

    submitted = re.sub(r"\s+", "", str(code or ""))
    expected = pending.get("code_hash") or ""
    if len(submitted) != 6 or not submitted.isdigit() or not expected:
        renewal_email_verifications_collection.update_one(
            {"_id": guild_id}, {"$inc": {"attempts": 1}}
        )
        remaining = EMAIL_VERIFY_ATTEMPTS - attempts - 1
        raise ValueError(f"That code is incorrect. {max(remaining, 0)} attempt(s) left.")
    if not secrets.compare_digest(_code_digest(guild_id, submitted), expected):
        renewal_email_verifications_collection.update_one(
            {"_id": guild_id}, {"$inc": {"attempts": 1}}
        )
        remaining = EMAIL_VERIFY_ATTEMPTS - attempts - 1
        raise ValueError(f"That code is incorrect. {max(remaining, 0)} attempt(s) left.")

    document = configure_renewal(
        pending["guild_id"],
        pending.get("guild_name"),
        pending.get("owner_discord_id"),
        pending["email"],
        pending["timezone"],
        pending["local_time"],
        now=now,
        email_verified=True,
    )
    renewal_email_verifications_collection.delete_one({"_id": guild_id})
    return document


def configure_renewal(
    guild_id,
    guild_name,
    owner_discord_id,
    email,
    timezone_name,
    local_time,
    now=None,
    email_verified=None,
):
    """Enable/update settings; only explicit test-mode transitions reset the due date."""
    if renewal_entitlements_collection is None:
        raise RuntimeError("Renewal database is unavailable.")
    email, timezone_name, local_time = validate_renewal_settings(
        email, timezone_name, local_time
    )
    now = time.time() if now is None else float(now)
    guild_id = str(guild_id)
    existing = renewal_entitlements_collection.find_one({"_id": guild_id})

    same_email = bool(existing and (existing.get("email") or "").lower() == email)
    if not email:
        verified = False
    elif same_email:
        stored_flag = existing.get("email_verified")
        inherited = True if stored_flag is None else bool(stored_flag)
        verified = inherited if email_verified is None else bool(email_verified)
    else:
        verified = bool(email_verified)
        if not verified:
            raise ValueError("Verify this email before saving it.")

    test_cycle_enabled = bool(RENEWAL_TEST_CYCLE_MINUTES)
    existing_was_test = bool(existing and existing.get("timing_mode") == "test")
    preserve_due = bool(
        existing
        and existing.get("due_at")
        and not test_cycle_enabled
        and not existing_was_test
    )
    if preserve_due:
        due_at = float(existing["due_at"])
        cycle = int(existing.get("cycle", 1))
        created_at = float(existing.get("created_at", now))
    else:
        # Entering or leaving explicit test timing resets the due date when the
        # Discord form is saved, so no MongoDB console access is required.
        due_at = compute_first_due(now, timezone_name, local_time)
        cycle = int(existing.get("cycle", 1)) if existing else 1
        created_at = float(existing.get("created_at", now)) if existing else now

    document = {
        "guild_id": guild_id,
        "guild_name": (guild_name or "Discord server")[:200],
        "owner_discord_id": str(owner_discord_id),
        "email": email,
        "email_verified": verified,
        "email_verified_at": (
            now if verified else existing.get("email_verified_at") if existing else None
        ),
        "timezone": timezone_name,
        "local_time": local_time,
        "timing_mode": "test" if test_cycle_enabled else "production",
        "due_at": due_at,
        "grace_ends_at": due_at + GRACE_PERIOD_SECONDS,
        "cycle": cycle,
        "enabled": True,
        "created_at": created_at,
        "updated_at": now,
    }
    renewal_entitlements_collection.update_one(
        {"_id": guild_id}, {"$set": document}, upsert=True
    )
    document["_id"] = guild_id
    return document


def _mongo_deadline(seconds=6):
    try:
        from pymongo import timeout as mongo_timeout
        return mongo_timeout(seconds)
    except Exception:
        return nullcontext()


def create_or_get_renewal_session(guild_id, admin_discord_id, now=None):
    """Create an expiring admin-bound session once its 24-hour window opens."""
    if renewal_sessions_collection is None:
        raise RuntimeError("Renewal database is unavailable.")
    now = time.time() if now is None else float(now)
    guild_id = str(guild_id)
    admin_discord_id = str(admin_discord_id)
    logger.info("create_or_get_renewal_session guild=%s admin=%s", guild_id, admin_discord_id)
    with _mongo_deadline(6):
        return _create_or_get_renewal_session_inner(guild_id, admin_discord_id, now)


def _create_or_get_renewal_session_inner(guild_id, admin_discord_id, now):
    status = get_renewal_status(guild_id, now)
    logger.info(
        "renewal status configured=%s available=%s state=%s",
        status.get("configured"),
        status.get("renewal_available"),
        status.get("state"),
    )
    if is_renewal_exempt(guild_id):
        raise ValueError("This server has permanent access and does not need renewal.")
    if not status.get("configured"):
        raise ValueError("Configure service renewal before starting checkpoints.")
    if not status.get("renewal_available"):
        raise ValueError(
            "Renewal opens 24 hours before the current due time."
        )

    existing = renewal_sessions_collection.find_one(
        {
            "guild_id": guild_id,
            "admin_discord_id": admin_discord_id,
            "completed": False,
            "expires_at": {"$gt": now},
        },
        sort=[("created_at", -1)],
    )
    if existing:
        return existing["_id"]

    token = secrets.token_urlsafe(32)
    step_tokens = {
        str(step): secrets.token_urlsafe(24)
        for step in range(1, CHECKPOINT_COUNT + 1)
    }
    expires_at = now + RENEWAL_SESSION_SECONDS
    renewal_sessions_collection.insert_one(
        {
            "_id": token,
            "guild_id": guild_id,
            "admin_discord_id": admin_discord_id,
            "cycle": int(status.get("cycle", 1)),
            "current_step": 1,
            "completed_steps": [],
            "checkpoint_links": {},
            "checkpoint_started_at": {},
            "step_tokens": step_tokens,
            "ip": None,
            "completed": False,
            "created_at": now,
            "expires_at": expires_at,
            "expires_at_ttl": datetime.fromtimestamp(expires_at, timezone.utc),
        }
    )
    return token


def insert_renewal_session(guild_id, admin_discord_id, token, now=None):
    """Persist a bot-generated session token. Never invent a different id."""
    if renewal_sessions_collection is None:
        raise RuntimeError("Renewal database is unavailable.")
    if is_renewal_exempt(guild_id):
        raise ValueError("This server has permanent access and does not need renewal.")
    now = time.time() if now is None else float(now)
    guild_id = str(guild_id)
    admin_discord_id = str(admin_discord_id)
    token = str(token or "").strip()
    if not token:
        raise ValueError("Missing renewal session token.")
    status = get_renewal_status(guild_id, now)
    if not status.get("configured"):
        raise ValueError("Configure service renewal before starting checkpoints.")
    if not status.get("renewal_available"):
        raise ValueError("Renewal opens 24 hours before the current due time.")

    existing = renewal_sessions_collection.find_one({"_id": token})
    if existing:
        return token
    expires_at = now + RENEWAL_SESSION_SECONDS
    try:
        renewal_sessions_collection.insert_one(
            {
                "_id": token,
                "guild_id": guild_id,
                "admin_discord_id": admin_discord_id,
                "cycle": int(status.get("cycle", 1)),
                "current_step": 1,
                "completed_steps": [],
                "checkpoint_links": {},
                "checkpoint_started_at": {},
                "step_tokens": {
                    str(step): secrets.token_urlsafe(24)
                    for step in range(1, CHECKPOINT_COUNT + 1)
                },
                "ip": None,
                "completed": False,
                "created_at": now,
                "expires_at": expires_at,
                "expires_at_ttl": datetime.fromtimestamp(expires_at, timezone.utc),
            }
        )
    except DuplicateKeyError:
        pass
    return token


def get_renewal_session(session_token, now=None):
    if renewal_sessions_collection is None:
        return None
    now = time.time() if now is None else float(now)
    try:
        session = renewal_sessions_collection.find_one({"_id": session_token})
        if not session:
            return None
        if now >= float(session.get("expires_at", 0)):
            renewal_sessions_collection.delete_one({"_id": session_token})
            return None
        return session
    except Exception as exc:
        logger.error("Failed to read renewal session: %s", exc)
        return None


def static_renewal_links():
    """Four dashboard lockers. Preferred over the create-link API."""
    links = []
    for step in range(1, CHECKPOINT_COUNT + 1):
        url = (os.environ.get(f"LOOTLABS_RENEWAL_LINK_{step}") or "").strip()
        links.append(url)
    if all(url.startswith(("http://", "https://")) for url in links):
        return links
    blob = (os.environ.get("LOOTLABS_RENEWAL_LINKS") or "").strip()
    if blob:
        parts = [part.strip() for part in re.split(r"[\s,]+", blob) if part.strip()]
        http_parts = [part for part in parts if part.startswith(("http://", "https://"))]
        if len(http_parts) >= CHECKPOINT_COUNT:
            return http_parts[:CHECKPOINT_COUNT]
    return None


def find_open_renewal_session_by_ip(client_ip, now=None):
    if renewal_sessions_collection is None or not client_ip:
        return None
    now = time.time() if now is None else float(now)
    return renewal_sessions_collection.find_one(
        {
            "ip": client_ip,
            "completed": False,
            "expires_at": {"$gt": now},
        },
        sort=[("created_at", -1)],
    )


def complete_static_renewal_step(step, client_ip, session_token=None, now=None):
    """Finish a step after a static locker, using the cookie session or IP."""
    now = time.time() if now is None else float(now)
    session = get_renewal_session(session_token, now) if session_token else None
    if not session:
        session = find_open_renewal_session_by_ip(client_ip, now)
    if not session:
        raise ValueError("No renewal session found. Open the Discord link first.")
    token = (session.get("step_tokens") or {}).get(str(int(step)), "")
    result = complete_renewal_checkpoint(session["_id"], step, token, client_ip, now)
    result["session_token"] = session["_id"]
    return result


def _lootlabs_settings():
    token = (os.environ.get("LOOTLABS_API_TOKEN") or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise RuntimeError("LootLabs renewal is not configured on the website.")
    try:
        tier_id = int(os.environ.get("LOOTLABS_RENEWAL_TIER_ID", "3"))
        theme = int(os.environ.get("LOOTLABS_RENEWAL_THEME", "5"))
    except ValueError:
        raise RuntimeError("LootLabs tier/theme environment variables must be numbers.")
    # Docs list tiers 1-3 as valid; theme 1-5.
    tier_id = min(max(tier_id, 1), 3)
    theme = min(max(theme, 1), 5)
    return token, tier_id, theme


def _extract_loot_url(data):
    """Pull a locker URL out of the several shapes LootLabs returns."""
    if data is None:
        return None
    if isinstance(data, str):
        text = data.strip()
        if text.startswith(("http://", "https://")):
            return text
        if text and " " not in text and 2 < len(text) < 80:
            return f"https://loot-link.com/s?{text}"
        return None
    if not isinstance(data, dict):
        return None

    for key in ("loot_url", "lootUrl", "locker_url", "short_url"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value

    short = data.get("short") or data.get("slug")
    if isinstance(short, str) and short.strip():
        short = short.strip()
        if short.startswith(("http://", "https://")):
            return short
        return f"https://loot-link.com/s?{short}"

    for key in ("message", "data", "result"):
        nested = data.get(key)
        if nested is not None and nested is not data:
            found = _extract_loot_url(nested)
            if found:
                return found
    return None


def _lootlabs_error_text(data, status_code, body_text=""):
    if isinstance(data, dict):
        if data.get("type") == "error" or data.get("error"):
            message = data.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()[:220]
            if isinstance(message, dict):
                nested = message.get("error") or message.get("message") or ""
                if nested:
                    return str(nested)[:220]
            if data.get("error") and data.get("error") is not True:
                return str(data.get("error"))[:220]
        snippet = (body_text or "")[:220].strip()
        if snippet:
            return snippet
    elif body_text:
        return body_text[:220].strip()
    if status_code:
        return f"HTTP {status_code}"
    return "Unexpected LootLabs response"


def _lootlabs_parse(response):
    body_text = (response.text or "")[:800]
    try:
        data = response.json()
    except ValueError:
        data = None
    loot_url = _extract_loot_url(data)
    ok = (
        response.status_code < 400
        and bool(loot_url)
        and not (isinstance(data, dict) and data.get("type") == "error")
    )
    return ok, loot_url, data, body_text


def _raise_lootlabs_failure(status_code, data, fallback):
    if status_code == 401:
        raise RuntimeError(
            "LootLabs rejected the API token. Check LOOTLABS_API_TOKEN on the website Render service."
        )
    if status_code == 429:
        raise RuntimeError("LootLabs rate-limited the request. Wait a minute and try again.")
    hint = _lootlabs_error_text(data, status_code, fallback) or fallback
    lowered = str(hint).lower()
    if "creator" in lowered or ("mandatory" in lowered and "detail" in lowered):
        raise RuntimeError(
            "LootLabs needs your creator profile filled in (name + avatar image) before it can create links."
        )
    raise RuntimeError(f"LootLabs could not create the checkpoint link. {hint}")


def _create_lootlabs_link(api_token, payload):
    """Create a locker link via POST, then GET if POST cannot reach LootLabs."""
    last_error = "No response from LootLabs."
    try:
        response = requests.post(
            LOOTLABS_API_URL,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=25,
        )
        ok, loot_url, data, body_text = _lootlabs_parse(response)
        logger.info("LootLabs POST status=%s body=%s", response.status_code, body_text)
        if ok:
            return loot_url, data
        last_error = _lootlabs_error_text(data, response.status_code, body_text)
        if response.status_code:
            _raise_lootlabs_failure(response.status_code, data, last_error or body_text)
    except RuntimeError:
        raise
    except requests.RequestException as exc:
        last_error = f"{type(exc).__name__}: {exc}"
        logger.error("LootLabs POST failed: %s", last_error)

    params = {
        "api_token": api_token,
        "title": payload["title"],
        "url": payload["url"],
        "tier_id": payload["tier_id"],
        "number_of_tasks": payload["number_of_tasks"],
        "theme": payload.get("theme", 1),
    }
    if payload.get("thumbnail"):
        params["thumbnail"] = payload["thumbnail"]
    try:
        response = requests.get(LOOTLABS_API_URL, params=params, timeout=25)
        ok, loot_url, data, body_text = _lootlabs_parse(response)
        logger.info("LootLabs GET status=%s body=%s", response.status_code, body_text)
        if ok:
            return loot_url, data
        _raise_lootlabs_failure(response.status_code, data, last_error)
    except RuntimeError:
        raise
    except requests.RequestException as exc:
        detail = f"{type(exc).__name__}: {exc}"
        logger.error("LootLabs GET failed: %s", detail)
        raise RuntimeError(
            f"LootLabs could not create the checkpoint link. {detail}"
        ) from exc


def start_renewal_checkpoint(session_token, client_ip, base_url=None, now=None):
    """Generate/cache the one-task LootLabs link for the expected next step."""
    if renewal_sessions_collection is None:
        raise RuntimeError("Renewal database is unavailable.")
    now = time.time() if now is None else float(now)
    session = get_renewal_session(session_token, now)
    if not session:
        raise ValueError("This renewal session expired. Start again from Discord.")
    if session.get("completed"):
        raise ValueError("This renewal is already complete.")
    entitlement = get_renewal_entitlement(session["guild_id"])
    if not entitlement or not entitlement.get("enabled"):
        raise ValueError("This server's renewal configuration is no longer active.")
    if int(session.get("cycle", 0)) != int(entitlement.get("cycle", 1)):
        raise ValueError("This renewal cycle is no longer current. Start again from Discord.")

    stored_ip = session.get("ip")
    if stored_ip and stored_ip != client_ip:
        raise ValueError("This renewal session is already active on another connection.")
    step = int(session.get("current_step", 1))
    if step < 1 or step > CHECKPOINT_COUNT:
        raise ValueError("This renewal has no remaining checkpoints.")
    step_key = str(step)
    cached = (session.get("checkpoint_links") or {}).get(step_key)
    if cached:
        return cached, step

    static_links = static_renewal_links()
    if static_links:
        loot_url = static_links[step - 1]
        link_field = f"checkpoint_links.{step_key}"
        started_field = f"checkpoint_started_at.{step_key}"
        result = renewal_sessions_collection.update_one(
            {
                "_id": session_token,
                "current_step": step,
                "completed": False,
                link_field: {"$exists": False},
                "$or": [{"ip": None}, {"ip": client_ip}],
            },
            {
                "$set": {
                    link_field: loot_url,
                    started_field: now,
                    "ip": client_ip,
                    "updated_at": now,
                    "link_mode": "static",
                }
            },
        )
        if result.modified_count:
            logger.info("Using static LootLabs locker for step=%s", step)
            return loot_url, step
        session = get_renewal_session(session_token, now)
        winner = ((session or {}).get("checkpoint_links") or {}).get(step_key)
        if winner and (not session.get("ip") or session.get("ip") == client_ip):
            return winner, step
        raise ValueError("Checkpoint state changed. Reload the renewal page.")

    api_token, tier_id, theme = _lootlabs_settings()
    completion_token = (session.get("step_tokens") or {}).get(step_key)
    if not completion_token:
        raise RuntimeError("Renewal checkpoint token is missing.")
    public_base = (base_url or SERVER_BASE_URL or "").strip().rstrip("/")
    if public_base.startswith("http://"):
        public_base = "https://" + public_base[len("http://"):]
    if not public_base.startswith("https://"):
        public_base = "https://" + public_base.lstrip("/")
    completion_url = (
        f"{public_base}/ks/renew/complete/{session_token}/{step}/{completion_token}"
    )
    payload = {
        "title": f"Checkpoint {step} of {CHECKPOINT_COUNT}"[:30],
        "url": completion_url,
        "tier_id": tier_id,
        "number_of_tasks": 1,
        "theme": theme,
    }
    thumbnail = (os.environ.get("LOOTLABS_RENEWAL_THUMBNAIL") or "").strip()
    if thumbnail.startswith(("http://", "https://")):
        payload["thumbnail"] = thumbnail
    logger.info(
        "Creating LootLabs locker title=%r dest=%s tier=%s theme=%s",
        payload["title"],
        completion_url,
        tier_id,
        theme,
    )

    loot_url, _data = _create_lootlabs_link(api_token, payload)

    link_field = f"checkpoint_links.{step_key}"
    started_field = f"checkpoint_started_at.{step_key}"
    result = renewal_sessions_collection.update_one(
        {
            "_id": session_token,
            "current_step": step,
            "completed": False,
            link_field: {"$exists": False},
            "$or": [{"ip": None}, {"ip": client_ip}],
        },
        {
            "$set": {
                link_field: loot_url,
                started_field: now,
                "ip": client_ip,
                "updated_at": now,
            }
        },
    )
    if result.modified_count:
        return loot_url, step

    # A parallel click may have won the race. Return only the stored winner.
    session = get_renewal_session(session_token, now)
    winner = ((session or {}).get("checkpoint_links") or {}).get(step_key)
    if winner and (not session.get("ip") or session.get("ip") == client_ip):
        return winner, step
    raise ValueError("Checkpoint state changed. Reload the renewal page.")


def _apply_completed_renewal(guild_id, session_token, now):
    entitlement = get_renewal_entitlement(guild_id)
    if not entitlement or not entitlement.get("enabled"):
        raise RuntimeError("Renewal entitlement no longer exists.")
    if entitlement.get("last_completed_session") == session_token:
        return entitlement

    new_due = compute_renewed_due(entitlement, now)
    renewal_entitlements_collection.update_one(
        {
            "_id": str(guild_id),
            "last_completed_session": {"$ne": session_token},
        },
        {
            "$set": {
                "due_at": new_due,
                "grace_ends_at": new_due + GRACE_PERIOD_SECONDS,
                "last_completed_at": now,
                "last_completed_session": session_token,
                "updated_at": now,
            },
            "$inc": {"cycle": 1},
        },
    )
    updated = get_renewal_entitlement(guild_id)
    if not updated or updated.get("last_completed_session") != session_token:
        raise RuntimeError("Could not apply the completed renewal.")
    return updated


def complete_renewal_checkpoint(
    session_token, step, completion_token, client_ip, now=None
):
    """Atomically complete only the expected step and renew after step four."""
    if renewal_sessions_collection is None:
        raise RuntimeError("Renewal database is unavailable.")
    now = time.time() if now is None else float(now)
    step = int(step)
    session = get_renewal_session(session_token, now)
    if not session:
        raise ValueError("This renewal session expired. Start again from Discord.")
    if step < 1 or step > CHECKPOINT_COUNT:
        raise ValueError("Invalid renewal checkpoint.")

    expected_token = (session.get("step_tokens") or {}).get(str(step), "")
    if not expected_token or not secrets.compare_digest(expected_token, completion_token):
        raise ValueError("Invalid renewal checkpoint token.")
    if session.get("ip") and session.get("ip") != client_ip:
        raise ValueError("Renewal connection mismatch. Return to the original browser.")

    entitlement = get_renewal_entitlement(session["guild_id"])
    if not entitlement or not entitlement.get("enabled"):
        raise ValueError("This server's renewal configuration is no longer active.")
    session_cycle = int(session.get("cycle", 0))
    entitlement_cycle = int(entitlement.get("cycle", 1))
    if session_cycle != entitlement_cycle:
        if (
            session.get("completed")
            and step == CHECKPOINT_COUNT
            and entitlement.get("last_completed_session") == session_token
        ):
            return {
                "completed": True,
                "step": step,
                "due_at": entitlement["due_at"],
            }
        raise ValueError("This renewal cycle is no longer current. Start again from Discord.")

    if session.get("completed"):
        if step == CHECKPOINT_COUNT:
            entitlement = _apply_completed_renewal(
                session["guild_id"], session_token, now
            )
            return {
                "completed": True,
                "step": step,
                "due_at": entitlement["due_at"],
            }
        raise ValueError("This renewal is already complete.")

    current_step = int(session.get("current_step", 1))
    if current_step != step:
        raise ValueError(f"Checkpoint {current_step} must be completed next.")
    started_at = (session.get("checkpoint_started_at") or {}).get(str(step))
    if not started_at:
        raise ValueError("Start this checkpoint from the renewal page first.")
    elapsed = now - float(started_at)
    if elapsed < MIN_CHECKPOINT_SECONDS:
        raise ValueError(
            f"Checkpoint completed too quickly. Wait {MIN_CHECKPOINT_SECONDS} seconds and use the LootLabs link."
        )

    update = {
        "$push": {"completed_steps": step},
        "$set": {"updated_at": now},
    }
    if step == CHECKPOINT_COUNT:
        update["$set"].update(
            {"current_step": CHECKPOINT_COUNT + 1, "completed": True, "completed_at": now}
        )
    else:
        update["$set"]["current_step"] = step + 1

    result = renewal_sessions_collection.update_one(
        {
            "_id": session_token,
            "current_step": step,
            "completed": False,
            f"step_tokens.{step}": completion_token,
        },
        update,
    )
    if not result.modified_count:
        latest = get_renewal_session(session_token, now)
        if latest and step in latest.get("completed_steps", []):
            if step == CHECKPOINT_COUNT:
                entitlement = _apply_completed_renewal(
                    latest["guild_id"], session_token, now
                )
                return {
                    "completed": True,
                    "step": step,
                    "due_at": entitlement["due_at"],
                }
            return {"completed": False, "step": step, "next_step": step + 1}
        raise ValueError("Checkpoint state changed. Reload the renewal page.")

    if step == CHECKPOINT_COUNT:
        entitlement = _apply_completed_renewal(session["guild_id"], session_token, now)
        return {"completed": True, "step": step, "due_at": entitlement["due_at"]}
    return {"completed": False, "step": step, "next_step": step + 1}


def format_renewal_timestamp(timestamp, timezone_name):
    if not timestamp:
        return "not set"
    return datetime.fromtimestamp(float(timestamp), ZoneInfo(timezone_name)).strftime(
        "%Y-%m-%d %H:%M %Z"
    )


def spoiler_email(email):
    email = (email or "").strip()
    if not email:
        return "Not set"
    return f"||{email}||"


def _is_freemail_address(address):
    address = (address or "").strip().lower()
    if "@" not in address:
        return False
    return address.rsplit("@", 1)[-1] in _FREEMAIL_DOMAINS


def sender_is_freemail():
    settings = _email_settings()
    if not settings:
        return False
    return _is_freemail_address(settings.get("from_address"))


def _brevo_settings():
    """Return HTTPS email settings when the Render-safe Brevo path is enabled."""
    api_key = (os.environ.get("BREVO_API_KEY") or "").strip()
    from_address = (os.environ.get("BREVO_FROM_EMAIL") or "").strip()
    if not api_key or not from_address:
        return None
    return {
        "provider": "brevo",
        "api_key": api_key,
        "from_address": from_address,
        "from_name": (
            os.environ.get("BREVO_FROM_NAME") or "Vadrifts Key System"
        ).strip(),
    }


def _smtp_settings():
    host = (os.environ.get("SMTP_HOST") or "").strip()
    from_address = (
        os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USERNAME") or ""
    ).strip()
    if not host or not from_address:
        return None
    return {
        "provider": "smtp",
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "username": (os.environ.get("SMTP_USERNAME") or "").strip(),
        "password": os.environ.get("SMTP_PASSWORD") or "",
        "from_address": from_address,
        "from_name": (os.environ.get("SMTP_FROM_NAME") or "Vadrifts Key System").strip(),
        "use_tls": os.environ.get("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"},
        "use_ssl": os.environ.get("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes", "on"},
    }


def _email_settings():
    # HTTPS is preferred when configured because free Render services block the
    # standard SMTP ports. SMTP remains as a backwards-compatible fallback.
    return _brevo_settings() or _smtp_settings()


def _html_email(title, paragraphs, code=None):
    blocks = [
        "<div style=\"font-family:Inter,Segoe UI,Arial,sans-serif;max-width:560px;"
        "margin:0 auto;padding:28px 24px;background:#0b0b0f;color:#f5f5f7;"
        "border-radius:18px\">",
        f"<h2 style=\"margin:0 0 16px;font-size:22px;color:#c4b5fd\">{html.escape(title)}</h2>",
    ]
    for paragraph in paragraphs:
        blocks.append(
            f"<p style=\"margin:0 0 12px;line-height:1.65;color:#d4d4d8\">{html.escape(paragraph)}</p>"
        )
    if code:
        blocks.append(
            "<p style=\"margin:20px 0;padding:16px 18px;background:#1b1030;"
            "border:1px solid #5b21b6;border-radius:12px;font-size:28px;"
            f"letter-spacing:8px;font-weight:800;color:#fff;text-align:center\">{html.escape(code)}</p>"
        )
    blocks.append(
        "<p style=\"margin:22px 0 0;font-size:12px;color:#71717a\">Vadrifts Key System</p></div>"
    )
    return "".join(blocks)


def _send_email(to_address, subject, body, html_body=None):
    settings = _email_settings()
    if not settings:
        raise RuntimeError("Email delivery is not configured.")

    if _is_freemail_address(settings["from_address"]) and not getattr(_send_email, "_freemail_warned", False):
        logger.warning(
            "BREVO_FROM_EMAIL/SMTP sender %s is a freemail address; Gmail/Yahoo will "
            "usually drop or spam these. Authenticate a real domain in Brevo.",
            settings["from_address"],
        )
        _send_email._freemail_warned = True

    if settings["provider"] == "brevo":
        payload = {
            "sender": {
                "name": settings["from_name"],
                "email": settings["from_address"],
            },
            "to": [{"email": to_address}],
            "subject": subject,
            "textContent": body,
        }
        if html_body:
            payload["htmlContent"] = html_body
        try:
            response = requests.post(
                BREVO_EMAIL_API_URL,
                headers={
                    "accept": "application/json",
                    "api-key": settings["api_key"],
                    "content-type": "application/json",
                },
                json=payload,
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = ""
            resp = getattr(exc, "response", None)
            if resp is not None:
                detail = (resp.text or "")[:300]
            suffix = f" (HTTP {status})" if status else ""
            logger.error("Brevo send failed%s: %s", suffix, detail or exc)
            raise RuntimeError(f"Brevo email API request failed{suffix}") from exc
        message_id = None
        try:
            message_id = (response.json() or {}).get("messageId")
        except ValueError:
            message_id = None
        logger.info(
            "Brevo accepted email to %s subject=%r messageId=%s",
            to_address,
            subject,
            message_id or "unknown",
        )
        return

    message = EmailMessage()
    message["From"] = formataddr((settings["from_name"], settings["from_address"]))
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    smtp_class = smtplib.SMTP_SSL if settings["use_ssl"] else smtplib.SMTP
    with smtp_class(settings["host"], settings["port"], timeout=15) as smtp:
        if settings["use_tls"] and not settings["use_ssl"]:
            smtp.starttls()
        if settings["username"]:
            smtp.login(settings["username"], settings["password"])
        smtp.send_message(message)
    logger.info("SMTP accepted email to %s subject=%r", to_address, subject)


def _reminder_event(entitlement, now):
    due_at = float(entitlement["due_at"])
    grace_ends_at = float(
        entitlement.get("grace_ends_at", due_at + GRACE_PERIOD_SECONDS)
    )
    if now >= grace_ends_at:
        return "blocked"
    if now >= due_at:
        return "grace"
    if now >= due_at - 60 * 60:
        return "one_hour"
    if now >= due_at - 24 * 60 * 60:
        return "one_day"
    return None


def _reminder_copy(entitlement, event):
    timezone_name = entitlement.get("timezone", "UTC")
    due_text = format_renewal_timestamp(entitlement["due_at"], timezone_name)
    grace_text = format_renewal_timestamp(
        entitlement.get("grace_ends_at"), timezone_name
    )
    labels = {
        "one_day": "Renewal is due within 24 hours",
        "one_hour": "Renewal is due within one hour",
        "grace": (
            f"Renewal is now in the {GRACE_PERIOD_SECONDS // 60}-minute grace period"
        ),
        "blocked": "Service access is blocked until renewal",
    }
    guild_name = entitlement.get("guild_name", "your Discord server")
    subject = f"[{guild_name}] {labels[event]}"
    body = (
        f"{labels[event]} for {guild_name}.\n\n"
        f"Due: {due_text}\n"
        f"Grace ends: {grace_text}\n\n"
        "Open that Discord server, run /ks setup, choose Service Renewal, "
        "and complete all four LootLabs checkpoints. Existing customer keys "
        "remain stored while access is blocked.\n"
    )
    return labels[event], subject, body, due_text, grace_text


def mark_discord_reminder_sent(notification_id):
    if renewal_notifications_collection is None:
        return
    renewal_notifications_collection.update_one(
        {"_id": notification_id},
        {"$set": {"discord_sent_at": time.time()}},
    )


def process_due_email_reminders(now=None):
    """Queue Discord DMs and optionally send email for the current reminder window."""
    if renewal_entitlements_collection is None or renewal_notifications_collection is None:
        return {"configured": False, "sent": 0, "failed": 0, "discord": []}

    now = time.time() if now is None else float(now)
    sent = 0
    failed = 0
    discord = []
    cursor = renewal_entitlements_collection.find({"enabled": True})
    for entitlement in cursor:
        event = _reminder_event(entitlement, now)
        if not event:
            continue
        guild_id = str(entitlement["_id"])
        if is_renewal_exempt(guild_id):
            continue
        cycle = int(entitlement.get("cycle", 1))
        notification_id = f"{guild_id}:{cycle}:{event}"
        try:
            renewal_notifications_collection.insert_one(
                {
                    "_id": notification_id,
                    "guild_id": guild_id,
                    "cycle": cycle,
                    "event": event,
                    "created_at": now,
                    "expires_at_ttl": datetime.fromtimestamp(
                        now + 120 * 24 * 60 * 60, timezone.utc
                    ),
                }
            )
        except DuplicateKeyError:
            pass
        existing = renewal_notifications_collection.find_one({"_id": notification_id}) or {}

        label, subject, body, due_text, grace_text = _reminder_copy(entitlement, event)
        email = (entitlement.get("email") or "").strip()
        email_ok = bool(email) and entitlement.get("email_verified") is not False
        if email_ok and not existing.get("sent_at") and _email_settings():
            html_body = _html_email(
                label,
                [
                    f"This is a reminder for {entitlement.get('guild_name', 'your Discord server')}.",
                    f"Due: {due_text}",
                    f"Grace ends: {grace_text}",
                    "Open Discord, run /ks setup, choose Service Renewal, and complete all four LootLabs checkpoints.",
                ],
            )
            try:
                _send_email(email, subject, body, html_body=html_body)
                renewal_notifications_collection.update_one(
                    {"_id": notification_id}, {"$set": {"sent_at": time.time()}}
                )
                sent += 1
            except Exception as exc:
                failed += 1
                logger.error(
                    "Renewal email failed for guild %s (%s): %s", guild_id, event, exc
                )

        discord_id = str(entitlement.get("owner_discord_id") or "").strip()
        if discord_id and not existing.get("discord_sent_at"):
            discord.append(
                {
                    "notification_id": notification_id,
                    "discord_id": discord_id,
                    "guild_id": guild_id,
                    "guild_name": entitlement.get("guild_name", "Discord server"),
                    "event": event,
                    "subject": subject,
                    "body": body,
                    "due_text": due_text,
                    "grace_text": grace_text,
                }
            )
    return {"configured": True, "sent": sent, "failed": failed, "discord": discord}
