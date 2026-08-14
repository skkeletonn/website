"""Durable guild-sponsored access and four-step LootLabs renewals.

This module is intentionally separate from guild_key_system.py.  The Discord bot
and website are separate deployments, but both point at the same MongoDB
collections and use this same document/state-machine contract.
"""

import logging
import os
import re
import secrets
import smtplib
import time
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
SERVER_BASE_URL = os.environ.get(
    "SERVER_BASE_URL", "https://valorium.onrender.com"
).rstrip("/")

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

if RENEWAL_TEST_CYCLE_MINUTES:
    logger.warning(
        "RENEWAL TEST TIMING ENABLED: cycle=%sm grace=%sm; remove the test variables before production",
        RENEWAL_TEST_CYCLE_MINUTES,
        GRACE_PERIOD_SECONDS // 60,
    )

LOOTLABS_API_URL = "https://creators.lootlabs.gg/api/public/content_locker"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

renewal_entitlements_collection = None
renewal_sessions_collection = None
renewal_notifications_collection = None
_smtp_missing_logged = False

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

    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        raise ValueError("Enter a valid notification email address.")
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
            "message": "Sponsored renewal is not configured; legacy access remains active.",
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
        "email": entitlement.get("email", ""),
        "guild_name": entitlement.get("guild_name", "Discord server"),
        "owner_discord_id": entitlement.get("owner_discord_id"),
    }
    if state == "active":
        result["message"] = "Sponsored access is active."
    elif state == "grace":
        grace_minutes = GRACE_PERIOD_SECONDS // 60
        result["message"] = (
            f"Sponsored access is in its {grace_minutes}-minute grace period."
        )
    else:
        result["message"] = (
            "Sponsored access expired. A server admin must complete the four renewal checkpoints."
        )
    return result


def get_renewal_entitlement(guild_id):
    if renewal_entitlements_collection is None:
        return None
    return renewal_entitlements_collection.find_one({"_id": str(guild_id)})


def get_renewal_status(guild_id, now=None):
    """Get dynamic access state. Missing records deliberately keep legacy access."""
    if renewal_entitlements_collection is None:
        if MONGODB_URI:
            return {
                "configured": True,
                "state": "unavailable",
                "allows_access": False,
                "renewal_available": False,
                "message": "Sponsored access could not be checked. Please try again shortly.",
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
            "message": "Sponsored access could not be checked. Please try again shortly.",
        }


def configure_renewal(
    guild_id,
    guild_name,
    owner_discord_id,
    email,
    timezone_name,
    local_time,
    now=None,
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


def create_or_get_renewal_session(guild_id, admin_discord_id, now=None):
    """Create an expiring admin-bound session once its 24-hour window opens."""
    if renewal_sessions_collection is None:
        raise RuntimeError("Renewal database is unavailable.")
    now = time.time() if now is None else float(now)
    guild_id = str(guild_id)
    admin_discord_id = str(admin_discord_id)
    status = get_renewal_status(guild_id, now)
    if not status.get("configured"):
        raise ValueError("Configure sponsored renewal before starting checkpoints.")
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


def _lootlabs_settings():
    token = (os.environ.get("LOOTLABS_API_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("LootLabs renewal is not configured on the website.")
    try:
        tier_id = int(os.environ.get("LOOTLABS_RENEWAL_TIER_ID", "3"))
        theme = int(os.environ.get("LOOTLABS_RENEWAL_THEME", "5"))
    except ValueError:
        raise RuntimeError("LootLabs tier/theme environment variables must be numbers.")
    return token, tier_id, theme


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

    api_token, tier_id, theme = _lootlabs_settings()
    completion_token = (session.get("step_tokens") or {}).get(step_key)
    if not completion_token:
        raise RuntimeError("Renewal checkpoint token is missing.")
    public_base = (base_url or SERVER_BASE_URL).rstrip("/")
    completion_url = (
        f"{public_base}/ks/renew/complete/{session_token}/{step}/{completion_token}"
    )
    payload = {
        "title": f"Server renewal {step} of {CHECKPOINT_COUNT}"[:30],
        "url": completion_url,
        "tier_id": tier_id,
        "number_of_tasks": 1,
        "theme": theme,
    }
    thumbnail = (os.environ.get("LOOTLABS_RENEWAL_THUMBNAIL") or "").strip()
    if thumbnail:
        payload["thumbnail"] = thumbnail

    try:
        response = requests.post(
            LOOTLABS_API_URL,
            headers={"Authorization": f"Bearer {api_token}"},
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("LootLabs link creation failed: %s", exc)
        raise RuntimeError("LootLabs could not create the checkpoint link. Try again shortly.")

    loot_url = (data.get("message") or {}).get("loot_url") if isinstance(data, dict) else None
    if not loot_url or data.get("type") == "error":
        logger.error("Unexpected LootLabs response: %r", data)
        raise RuntimeError("LootLabs rejected the checkpoint link request.")

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


def _smtp_settings():
    host = (os.environ.get("SMTP_HOST") or "").strip()
    from_address = (
        os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USERNAME") or ""
    ).strip()
    if not host or not from_address:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "username": (os.environ.get("SMTP_USERNAME") or "").strip(),
        "password": os.environ.get("SMTP_PASSWORD") or "",
        "from_address": from_address,
        "from_name": (os.environ.get("SMTP_FROM_NAME") or "Vadrifts Key System").strip(),
        "use_tls": os.environ.get("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"},
        "use_ssl": os.environ.get("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes", "on"},
    }


def _send_email(to_address, subject, body):
    settings = _smtp_settings()
    if not settings:
        raise RuntimeError("SMTP is not configured.")
    message = EmailMessage()
    message["From"] = formataddr((settings["from_name"], settings["from_address"]))
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    smtp_class = smtplib.SMTP_SSL if settings["use_ssl"] else smtplib.SMTP
    with smtp_class(settings["host"], settings["port"], timeout=15) as smtp:
        if settings["use_tls"] and not settings["use_ssl"]:
            smtp.starttls()
        if settings["username"]:
            smtp.login(settings["username"], settings["password"])
        smtp.send_message(message)


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


def process_due_email_reminders(now=None):
    """Send one idempotent reminder for the guild's current reminder window."""
    global _smtp_missing_logged
    if renewal_entitlements_collection is None or renewal_notifications_collection is None:
        return {"configured": False, "sent": 0, "failed": 0}
    if not _smtp_settings():
        if not _smtp_missing_logged:
            logger.warning(
                "SMTP_HOST/SMTP_FROM are not configured; renewal emails are being skipped"
            )
            _smtp_missing_logged = True
        return {"configured": False, "sent": 0, "failed": 0}

    now = time.time() if now is None else float(now)
    sent = 0
    failed = 0
    cursor = renewal_entitlements_collection.find(
        {"enabled": True, "email": {"$type": "string", "$ne": ""}}
    )
    for entitlement in cursor:
        event = _reminder_event(entitlement, now)
        if not event:
            continue
        guild_id = str(entitlement["_id"])
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
            continue

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
            "blocked": "Sponsored access is blocked until renewal",
        }
        subject = f"[{entitlement.get('guild_name', 'Discord server')}] {labels[event]}"
        body = (
            f"{labels[event]} for {entitlement.get('guild_name', 'your Discord server')}.\n\n"
            f"Due: {due_text}\n"
            f"Grace ends: {grace_text}\n\n"
            "Open your Discord server, run /ks setup, choose Sponsored Renewal, "
            "and complete all four LootLabs checkpoints. Existing customer keys "
            "remain stored while access is blocked.\n"
        )
        try:
            _send_email(entitlement["email"], subject, body)
            renewal_notifications_collection.update_one(
                {"_id": notification_id}, {"$set": {"sent_at": time.time()}}
            )
            sent += 1
        except Exception as exc:
            failed += 1
            logger.error(
                "Renewal email failed for guild %s (%s): %s", guild_id, event, exc
            )
            # Permit the next worker run to retry this event.
            renewal_notifications_collection.delete_one({"_id": notification_id})
    return {"configured": True, "sent": sent, "failed": failed}
