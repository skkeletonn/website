import os
import logging
import psycopg2
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")


def _get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def _init_db():
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set")
        return
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS execution_logs (
                id        SERIAL PRIMARY KEY,
                hwid      TEXT NOT NULL,
                script    TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_logs_timestamp
            ON execution_logs (timestamp)
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Analytics DB ready")
    except Exception as e:
        logger.error(f"Failed to initialize analytics DB: {e}")


_init_db()


def log_execution(hwid, script):
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO execution_logs (hwid, script, timestamp) VALUES (%s, %s, %s)",
            (hwid, script, datetime.utcnow())
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Failed to log execution: {e}")
    finally:
        if conn:
            conn.close()


def _cleanup_old_rows(conn):
    try:
        cutoff = datetime.utcnow() - timedelta(days=60)
        cur = conn.cursor()
        cur.execute("DELETE FROM execution_logs WHERE timestamp < %s", (cutoff,))
        conn.commit()
        cur.close()
    except Exception:
        pass


def get_analytics():
    empty = {
        "total_executions": 0,
        "week_executions": 0,
        "month_executions": 0,
        "prev_week_executions": 0,
        "prev_month_executions": 0,
        "unique_users_week": 0,
        "unique_users_month": 0,
        "script_breakdown": {},
        "chart_labels": [],
        "chart_data": [],
        "script_daily_data": {},
    }

    if not DATABASE_URL:
        return empty

    conn = None
    try:
        conn = _get_conn()
        _cleanup_old_rows(conn)

        cur = conn.cursor()
        cutoff = datetime.utcnow() - timedelta(days=60)
        cur.execute(
            "SELECT hwid, script, timestamp FROM execution_logs WHERE timestamp >= %s",
            (cutoff,)
        )
        rows = cur.fetchall()
        cur.close()

        if not rows:
            return empty

        now = datetime.utcnow()
        week_ago = now - timedelta(days=7)
        two_weeks_ago = now - timedelta(days=14)
        month_ago = now - timedelta(days=30)

        week_count = 0
        prev_week_count = 0
        month_count = 0
        prev_month_count = 0

        script_counts = defaultdict(int)
        unique_week = set()
        unique_month = set()
        daily_data = defaultdict(int)
        script_daily = defaultdict(lambda: defaultdict(int))

        for hwid, script, ts in rows:
            script = script or "Unknown"
            hwid = hwid or ""

            script_counts[script] += 1
            day_key = ts.strftime("%Y-%m-%d")
            daily_data[day_key] += 1
            script_daily[script][day_key] += 1

            if ts >= week_ago:
                week_count += 1
                unique_week.add(hwid)
            elif ts >= two_weeks_ago:
                prev_week_count += 1

            if ts >= month_ago:
                month_count += 1
                unique_month.add(hwid)
            else:
                prev_month_count += 1

        last_30 = [
            (now - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(29, -1, -1)
        ]
        chart_data = [daily_data.get(d, 0) for d in last_30]

        script_chart = {
            s: [script_daily[s].get(d, 0) for d in last_30]
            for s in script_counts
        }

        return {
            "total_executions": len(rows),
            "week_executions": week_count,
            "month_executions": month_count,
            "prev_week_executions": prev_week_count,
            "prev_month_executions": prev_month_count,
            "unique_users_week": len(unique_week),
            "unique_users_month": len(unique_month),
            "script_breakdown": dict(script_counts),
            "chart_labels": last_30,
            "chart_data": chart_data,
            "script_daily_data": script_chart,
        }

    except Exception as e:
        logger.error(f"Failed to get analytics: {e}")
        return empty
    finally:
        if conn:
            conn.close()
