from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import secrets
from typing import Any


SESSION_COOKIE_NAME = "wnba_admin_session"
SESSION_TTL_DAYS = int(os.getenv("ADMIN_SESSION_TTL_DAYS", "14"))
PBKDF2_ITERATIONS = int(os.getenv("ADMIN_PASSWORD_PBKDF2_ITERATIONS", "260000"))


@dataclass
class SessionUser:
    user_id: int
    username: str
    is_admin: bool
    csrf_token: str


def ensure_auth_schema(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_sessions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            session_token TEXT NOT NULL UNIQUE,
            csrf_token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES auth_users(id)
        )
        """
    )
    session_columns = {row["name"] for row in conn.execute("PRAGMA table_info(auth_sessions)").fetchall()}
    if "csrf_token" not in session_columns:
        conn.execute("ALTER TABLE auth_sessions ADD COLUMN csrf_token TEXT")
        conn.execute("UPDATE auth_sessions SET csrf_token = '' WHERE csrf_token IS NULL")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
        ON auth_sessions(user_id)
        """
    )
    prune_expired_sessions(conn)
    ensure_bootstrap_admin(conn)


def ensure_bootstrap_admin(conn: Any) -> None:
    username = os.getenv("ADMIN_USERNAME", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not username or not password:
        return
    now = _utcnow()
    password_hash = hash_password(password)
    existing = conn.execute(
        "SELECT id, username, password_hash, is_admin, is_active FROM auth_users WHERE lower(username) = lower(?)",
        (username,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO auth_users (username, password_hash, is_admin, is_active, created_at, updated_at)
            VALUES (?, ?, 1, 1, ?, ?)
            """,
            (username, password_hash, now, now),
        )
        conn.commit()
        return
    should_update = (
        not verify_password(password, str(existing["password_hash"]))
        or int(existing["is_admin"] or 0) != 1
        or int(existing["is_active"] or 0) != 1
        or str(existing["username"]) != username
    )
    if should_update:
        conn.execute(
            """
            UPDATE auth_users
            SET username = ?, password_hash = ?, is_admin = 1, is_active = 1, updated_at = ?
            WHERE id = ?
            """,
            (username, password_hash, now, int(existing["id"])),
        )
        conn.commit()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return hmac.compare_digest(digest.hex(), expected)


def authenticate_user(conn: Any, username: str, password: str) -> SessionUser | None:
    row = conn.execute(
        """
        SELECT id, username, password_hash, is_admin, is_active
        FROM auth_users
        WHERE lower(username) = lower(?)
        """,
        (username.strip(),),
    ).fetchone()
    if row is None or int(row["is_active"] or 0) != 1:
        return None
    if not verify_password(password, str(row["password_hash"])):
        return None
    return SessionUser(
        user_id=int(row["id"]),
        username=str(row["username"]),
        is_admin=bool(row["is_admin"]),
        csrf_token="",
    )


def create_session(conn: Any, user: SessionUser) -> tuple[str, str]:
    prune_expired_sessions(conn)
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=SESSION_TTL_DAYS)
    conn.execute(
        """
        INSERT INTO auth_sessions (user_id, session_token, csrf_token, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user.user_id, token, csrf_token, now.isoformat(), expires_at.isoformat()),
    )
    conn.commit()
    return token, csrf_token


def delete_session(conn: Any, token: str | None) -> None:
    if not token:
        return
    conn.execute("DELETE FROM auth_sessions WHERE session_token = ?", (token,))
    conn.commit()


def get_session_user(conn: Any, token: str | None) -> SessionUser | None:
    if not token:
        return None
    prune_expired_sessions(conn)
    row = conn.execute(
        """
        SELECT u.id, u.username, u.is_admin, s.csrf_token
        FROM auth_sessions s
        JOIN auth_users u ON u.id = s.user_id
        WHERE s.session_token = ?
          AND s.expires_at > ?
          AND u.is_active = 1
        """,
        (token, _utcnow()),
    ).fetchone()
    if row is None:
        return None
    return SessionUser(
        user_id=int(row["id"]),
        username=str(row["username"]),
        is_admin=bool(row["is_admin"]),
        csrf_token=str(row["csrf_token"] or ""),
    )


def prune_expired_sessions(conn: Any) -> None:
    conn.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (_utcnow(),))
    conn.commit()


def session_cookie_max_age() -> int:
    return SESSION_TTL_DAYS * 24 * 60 * 60


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
