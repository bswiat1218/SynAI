from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerificationError, VerifyMismatchError

from synai.web.database import MetadataDatabase


class AuthenticationError(Exception):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.public_message = message


@dataclass(frozen=True)
class AuthenticatedSession:
    token_hash: str
    expires_at: int


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_token: str
    expires_at: int


class AuthenticationService:
    RATE_WINDOW_SECONDS = 300
    RATE_MAX_ATTEMPTS = 5
    RATE_BLOCK_SECONDS = 300
    MAX_TRACKED_PEERS = 4096
    MAX_ACTIVE_SESSIONS = 64

    def __init__(self, database: MetadataDatabase, lifetime_seconds: int) -> None:
        self.database = database
        self.lifetime_seconds = lifetime_seconds
        self.hasher = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=2, type=Type.ID)

    def initialize(self, initial_password: str | None) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM credentials WHERE singleton = 1",
            ).fetchone()
            if row is not None:
                return
            if initial_password is None:
                raise ValueError(
                    "SYNAI_INITIAL_PASSWORD is required once to initialize browser authentication",
                )
            encoded = initial_password.encode("utf-8")
            if not 12 <= len(encoded) <= 1024:
                raise ValueError("Initial credential must be 12 to 1024 UTF-8 bytes")
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT 1 FROM credentials WHERE singleton = 1",
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO credentials(singleton, password_hash) VALUES (1, ?)",
                        (self.hasher.hash(initial_password),),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def login(self, password: str, peer: str, now: int | None = None) -> IssuedSession:
        timestamp = int(time.time()) if now is None else now
        peer_hash = hashlib.sha256(peer.encode("utf-8", errors="replace")).hexdigest()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM login_limits WHERE window_start < ? AND blocked_until < ?",
                    (timestamp - self.RATE_WINDOW_SECONDS, timestamp),
                )
                limit = connection.execute(
                    "SELECT window_start, attempts, blocked_until FROM login_limits WHERE peer_hash = ?",
                    (peer_hash,),
                ).fetchone()
                if limit is not None and limit["blocked_until"] > timestamp:
                    connection.commit()
                    raise AuthenticationError(
                        "rate_limited", 429, "Too many login attempts. Try again later.",
                    )
                if limit is None or timestamp - limit["window_start"] >= self.RATE_WINDOW_SECONDS:
                    attempts = 0
                    window_start = timestamp
                else:
                    attempts = limit["attempts"]
                    window_start = limit["window_start"]
                if limit is None:
                    peer_count = connection.execute(
                        "SELECT count(*) AS count FROM login_limits",
                    ).fetchone()["count"]
                    if peer_count >= self.MAX_TRACKED_PEERS:
                        connection.commit()
                        raise AuthenticationError(
                            "rate_limit_capacity", 503, "Login service is temporarily unavailable.",
                        )
                if attempts >= self.RATE_MAX_ATTEMPTS:
                    connection.execute(
                        "UPDATE login_limits SET blocked_until = ? WHERE peer_hash = ?",
                        (timestamp + self.RATE_BLOCK_SECONDS, peer_hash),
                    )
                    connection.commit()
                    raise AuthenticationError(
                        "rate_limited", 429, "Too many login attempts. Try again later.",
                    )
                attempts += 1
                blocked_until = timestamp + self.RATE_BLOCK_SECONDS if attempts >= self.RATE_MAX_ATTEMPTS else 0
                connection.execute(
                    "INSERT INTO login_limits(peer_hash, window_start, attempts, blocked_until) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(peer_hash) DO UPDATE SET "
                    "window_start=excluded.window_start, attempts=excluded.attempts, "
                    "blocked_until=excluded.blocked_until",
                    (peer_hash, window_start, attempts, blocked_until),
                )
                credential = connection.execute(
                    "SELECT password_hash FROM credentials WHERE singleton = 1",
                ).fetchone()
                connection.commit()
            except AuthenticationError:
                raise
            except BaseException:
                connection.rollback()
                raise
        valid = False
        if credential is not None:
            try:
                valid = self.hasher.verify(credential["password_hash"], password)
            except (VerifyMismatchError, VerificationError):
                valid = False
        if not valid:
            raise AuthenticationError("unauthenticated", 401, "Invalid credentials.")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM login_limits WHERE peer_hash = ?", (peer_hash,))
                connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (timestamp,))
                active_count = connection.execute(
                    "SELECT count(*) AS count FROM sessions WHERE expires_at > ?",
                    (timestamp,),
                ).fetchone()["count"]
                if active_count >= self.MAX_ACTIVE_SESSIONS:
                    connection.commit()
                    raise AuthenticationError(
                        "session_capacity", 429, "The maximum number of active sessions was reached.",
                    )
                raw_token = secrets.token_urlsafe(32)
                raw_csrf = secrets.token_urlsafe(32)
                expires_at = timestamp + self.lifetime_seconds
                connection.execute(
                    "INSERT INTO sessions(token_hash, csrf_hash, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?)",
                    (_digest(raw_token), _digest(raw_csrf), timestamp, expires_at),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return IssuedSession(raw_token, raw_csrf, expires_at)

    def session(self, raw_token: str | None, now: int | None = None) -> AuthenticatedSession:
        if not isinstance(raw_token, str) or len(raw_token) > 128:
            raise AuthenticationError("unauthenticated", 401, "Authentication required.")
        timestamp = int(time.time()) if now is None else now
        token_hash = _digest(raw_token)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None or row["expires_at"] <= timestamp:
                if row is not None:
                    connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                raise AuthenticationError("unauthenticated", 401, "Authentication required.")
        return AuthenticatedSession(token_hash, row["expires_at"])

    def rotate_csrf(self, session: AuthenticatedSession) -> str:
        csrf_token = secrets.token_urlsafe(32)
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET csrf_hash = ? WHERE token_hash = ? AND expires_at > ?",
                (_digest(csrf_token), session.token_hash, int(time.time())),
            )
            if cursor.rowcount != 1:
                raise AuthenticationError("unauthenticated", 401, "Authentication required.")
        return csrf_token

    def verify_csrf(self, session: AuthenticatedSession, csrf_token: str | None) -> bool:
        if not isinstance(csrf_token, str) or len(csrf_token) > 128:
            return False
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT csrf_hash FROM sessions WHERE token_hash = ? AND expires_at > ?",
                (session.token_hash, int(time.time())),
            ).fetchone()
        return row is not None and hmac.compare_digest(row["csrf_hash"], _digest(csrf_token))

    def logout(self, session: AuthenticatedSession) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (session.token_hash,))

    def change_password(
        self, session: AuthenticatedSession, current_password: str, new_password: str,
    ) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM credentials WHERE singleton = 1",
            ).fetchone()
        try:
            valid = row is not None and self.hasher.verify(row["password_hash"], current_password)
        except (VerifyMismatchError, VerificationError):
            valid = False
        if not valid:
            raise AuthenticationError("invalid_credentials", 403, "Current credential is incorrect.")
        if not 12 <= len(new_password.encode("utf-8")) <= 1024:
            raise AuthenticationError("invalid_credential", 422, "New credential is outside supported bounds.")
        replacement = self.hasher.hash(new_password)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE credentials SET password_hash = ? WHERE singleton = 1",
                    (replacement,),
                )
                connection.execute("DELETE FROM sessions")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()
