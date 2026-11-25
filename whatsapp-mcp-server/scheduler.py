"""Persistent WhatsApp message scheduler (runs inside the MCP server process).

Design goals: never lose a job, never double-send, recover from restarts.
 - Jobs live in SQLite on the persistent volume (store/scheduler.db).
 - A job is marked 'sending' BEFORE it is sent. If the server dies mid-send the job
   becomes 'unknown' on restart instead of being sent twice.
 - Failed sends are retried (3 attempts, growing delay); then marked 'failed'.
 - Jobs that are more than 6 hours late (server was off) are marked 'missed', not sent.
 - Sends are spaced 2-6 s apart to look human and reduce the risk of WhatsApp flags.
"""
import os
import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

STORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "whatsapp-bridge", "store")
DB_PATH = os.path.join(STORE_DIR, "scheduler.db")
POLL_SECONDS = 15
GRACE_SECONDS = 6 * 3600
MAX_ATTEMPTS = 3
MEDIA_KEEP_DAYS = 14
REPEATS = {"none": None, "daily": timedelta(days=1), "weekly": timedelta(days=7)}

_started = False
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    os.makedirs(STORE_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def init() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                media_message_id TEXT NOT NULL DEFAULT '',
                media_chat_jid TEXT NOT NULL DEFAULT '',
                send_at TEXT NOT NULL,
                repeat TEXT NOT NULL DEFAULT 'none',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, send_at)")
        # a job left in 'sending' means the server died mid-send: never auto-resend it
        c.execute(
            "UPDATE jobs SET status='unknown', last_error='Interrupted while sending - check the chat before re-scheduling' "
            "WHERE status='sending'"
        )


def parse_send_time(send_at: str = "", delay_minutes: float = 0) -> datetime:
    """ISO-8601 time (UTC if no offset given) or a delay in minutes from now."""
    now = _now()
    if delay_minutes and delay_minutes > 0:
        return now + timedelta(minutes=float(delay_minutes))
    if not send_at or not send_at.strip():
        raise ValueError("Provide send_at (e.g. 2026-11-26T09:00:00Z) or delay_minutes")
    try:
        dt = datetime.fromisoformat(send_at.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Could not read send_at '{send_at}'. Use ISO format like 2026-11-26T09:00:00Z")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt < now - timedelta(minutes=1):
        raise ValueError(f"send_at {_iso(dt)} is in the past (current UTC time is {_iso(now)})")
    return dt


def add_jobs(
    recipients: List[str],
    message: str,
    when: datetime,
    repeat: str = "none",
    media_message_id: str = "",
    media_chat_jid: str = "",
) -> List[int]:
    if repeat not in REPEATS:
        raise ValueError("repeat must be one of: none, daily, weekly")
    if not message and not media_message_id:
        raise ValueError("Provide a message and/or media_message_id")
    if media_message_id and not media_chat_jid:
        raise ValueError("media_chat_jid is required with media_message_id")
    if not recipients:
        raise ValueError("At least one recipient is required")
    if len(recipients) > 50:
        raise ValueError("At most 50 recipients per call")
    ids: List[int] = []
    with _conn() as c:
        for r in recipients:
            cur = c.execute(
                "INSERT INTO jobs (recipient, message, media_message_id, media_chat_jid, send_at, repeat, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (r, message, media_message_id, media_chat_jid, _iso(when), repeat, _iso(_now())),
            )
            ids.append(cur.lastrowid)
    return ids


def list_jobs(status: str = "pending", limit: int = 50) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with _conn() as c:
        if status in ("", "all"):
            rows = c.execute("SELECT * FROM jobs ORDER BY send_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        else:
            order = "ASC" if status == "pending" else "DESC"
            rows = c.execute(
                f"SELECT * FROM jobs WHERE status = ? ORDER BY send_at {order}, id {order} LIMIT ?", (status, limit)
            ).fetchall()
    return [dict(r) for r in rows]


def cancel(job_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("UPDATE jobs SET status='cancelled' WHERE id=? AND status='pending'", (int(job_id),))
        return cur.rowcount > 0


def _schedule_next(c: sqlite3.Connection, job: sqlite3.Row, now: datetime) -> None:
    step = REPEATS.get(job["repeat"])
    if not step:
        return
    nxt = datetime.fromisoformat(job["send_at"])
    while nxt <= now:
        nxt += step
    c.execute(
        "INSERT INTO jobs (recipient, message, media_message_id, media_chat_jid, send_at, repeat, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job["recipient"], job["message"], job["media_message_id"], job["media_chat_jid"],
         _iso(nxt), job["repeat"], _iso(now)),
    )


def run_due(send_fn: Callable[[Dict[str, Any]], Tuple[bool, str]]) -> int:
    """Send every job that is due. Returns the number of jobs handled."""
    now = _now()
    with _conn() as c:
        due = c.execute(
            "SELECT * FROM jobs WHERE status='pending' AND send_at <= ? ORDER BY send_at, id LIMIT 20",
            (_iso(now),),
        ).fetchall()
    handled = 0
    for job in due:
        with _conn() as c:
            late = (now - datetime.fromisoformat(job["send_at"])).total_seconds()
            if late > GRACE_SECONDS:
                c.execute(
                    "UPDATE jobs SET status='missed', last_error=? WHERE id=? AND status='pending'",
                    (f"Missed: {int(late // 60)} minutes late (server was offline?)", job["id"]),
                )
                _schedule_next(c, job, now)
                handled += 1
                continue
            claimed = c.execute(
                "UPDATE jobs SET status='sending', attempts=attempts+1 WHERE id=? AND status='pending'", (job["id"],)
            ).rowcount
        if not claimed:
            continue
        try:
            ok, msg = send_fn(dict(job))
        except Exception as e:  # never let one bad job stop the loop
            ok, msg = False, f"{type(e).__name__}: {e}"
        attempts = job["attempts"] + 1
        with _conn() as c:
            if ok:
                c.execute("UPDATE jobs SET status='sent', sent_at=?, last_error=NULL WHERE id=?", (_iso(_now()), job["id"]))
                _schedule_next(c, job, _now())
            elif attempts >= MAX_ATTEMPTS:
                c.execute("UPDATE jobs SET status='failed', last_error=? WHERE id=?", (str(msg)[:500], job["id"]))
                _schedule_next(c, job, _now())
            else:
                retry_at = _now() + timedelta(seconds=60 * attempts)
                c.execute(
                    "UPDATE jobs SET status='pending', send_at=?, last_error=? WHERE id=?",
                    (_iso(retry_at), f"Attempt {attempts} failed: {str(msg)[:400]}", job["id"]),
                )
        handled += 1
        time.sleep(random.uniform(2, 6))
    return handled


def cleanup_media(keep_days: int = MEDIA_KEEP_DAYS) -> int:
    """Delete downloaded media older than keep_days so the volume never fills up."""
    removed = 0
    cutoff = time.time() - keep_days * 86400
    try:
        for entry in os.scandir(STORE_DIR):
            if not entry.is_dir():
                continue
            for f in os.scandir(entry.path):
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        os.remove(f.path)
                        removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


def start(send_fn: Callable[[Dict[str, Any]], Tuple[bool, str]]) -> None:
    """Start the background scheduler thread (once)."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    init()

    def loop() -> None:
        last_cleanup = 0.0
        while True:
            try:
                run_due(send_fn)
                if time.time() - last_cleanup > 6 * 3600:
                    cleanup_media()
                    last_cleanup = time.time()
            except Exception as e:
                print(f"[scheduler] error: {e}", flush=True)
            time.sleep(POLL_SECONDS)

    threading.Thread(target=loop, name="wa-scheduler", daemon=True).start()
    print("[scheduler] started", flush=True)
