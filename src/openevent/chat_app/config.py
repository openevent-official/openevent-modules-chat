"""Portable configuration and append-only session registration."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from filelock import FileLock, Timeout

MAX_UINT64 = (1 << 64) - 1
ULID_RE = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}\Z")
DECIMAL_RE = re.compile(r"[1-9][0-9]*\Z")


class ConfigurationError(ValueError):
    """Invalid or ambiguous persistent configuration (no secret values)."""


def strict_json(data: bytes | str) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("non-finite JSON number")

    result = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(result, dict):
        raise ValueError("JSON object required")
    return result


def uint64_string(value) -> int:
    if not isinstance(value, str) or not DECIMAL_RE.fullmatch(value):
        raise ValueError("canonical positive uint64 string required")
    number = int(value)
    if number > MAX_UINT64:
        raise ValueError("uint64 overflow")
    return number


def valid_ulid(value) -> str:
    if not isinstance(value, str) or not ULID_RE.fullmatch(value):
        raise ValueError("canonical ULID required")
    return value


def new_ulid() -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    number = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    return "".join(alphabet[(number >> (5 * i)) & 31] for i in range(25, -1, -1))


def regular_file(path: Path):
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise ConfigurationError("configuration entry is not a regular file")


def write_json(path: Path, value: dict, *, exclusive: bool):
    if not exclusive:
        regular_file(path)
    with path.open("x" if exclusive else "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    # Closing flushes Python's buffers. Power-loss durability is not promised.


@dataclass(frozen=True)
class ServerConfig:
    openevent_target: str
    channels_dir: Path
    web_token: str
    user_principal: int
    user_openevent_token: str
    agent_principal: int
    rpc_timeout_ms: float
    max_retries: int

    @classmethod
    def load(cls, path: str | Path) -> "ServerConfig":
        path = Path(path)
        try:
            regular_file(path)
            data = strict_json(path.read_bytes().decode("utf-8", errors="strict"))
            if set(data) != set(cls.__dataclass_fields__):
                raise ValueError("invalid server configuration fields")
            for name in ("openevent_target", "channels_dir", "web_token", "user_openevent_token"):
                if not isinstance(data[name], str) or not data[name]:
                    raise ValueError("non-empty configuration string required")
                data[name].encode("utf-8", errors="strict")
            for name in ("user_principal", "agent_principal"):
                data[name] = uint64_string(data[name])
            if data["user_principal"] == data["agent_principal"]:
                raise ValueError("user and agent must be distinct")
            timeout = data["rpc_timeout_ms"]
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("positive finite rpc_timeout_ms required")
            retries = data["max_retries"]
            if type(retries) is not int or retries < 0:
                raise ValueError("nonnegative integer max_retries required")
            directory = Path(data["channels_dir"])
            data["channels_dir"] = directory if directory.is_absolute() else path.absolute().parent / directory
            return cls(**data)
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            raise ConfigurationError("invalid server configuration") from exc


@dataclass(frozen=True)
class SessionConfig:
    session_id: str
    create_request_id: str
    channel_id: int
    scan_start_seq: int

    def as_json(self):
        return {"format_version": 1, "session_id": self.session_id,
                "create_request_id": self.create_request_id, "channel_id": str(self.channel_id),
                "scan_start_seq": str(self.scan_start_seq)}

    @classmethod
    def parse(cls, data, filename):
        if set(data) != {"format_version", "session_id", "create_request_id", "channel_id", "scan_start_seq"}:
            raise ValueError("invalid session fields")
        if type(data["format_version"]) is not int or data["format_version"] != 1:
            raise ValueError("unsupported session format")
        sid = valid_ulid(data["session_id"])
        if filename != sid + ".json":
            raise ValueError("session filename mismatch")
        return cls(sid, valid_ulid(data["create_request_id"]), uint64_string(data["channel_id"]),
                   uint64_string(data["scan_start_seq"]))


class ConfigStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.pending = self.directory / ".pending"
        self.lock = threading.RLock()
        self.sessions = {}
        self.requests = {}
        self.channels = set()
        self.recoverable = {}
        self._directory_lock = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._check_directory(self.directory)
            lock_path = self.directory / ".lock"
            if lock_path.exists() or lock_path.is_symlink():
                regular_file(lock_path)
            # Startup acquires the lock; a request thread may close the service.
            self._directory_lock = FileLock(lock_path, timeout=0, thread_local=False)
            self._directory_lock.acquire()
            self.pending.mkdir(exist_ok=True)
            self._check_directory(self.pending)
            self._load()
        except Timeout as exc:
            self.close()
            raise ConfigurationError("configuration directory is already in use") from exc
        except Exception:
            self.close()
            raise

    @staticmethod
    def _check_directory(path):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ConfigurationError("configuration directory must be a directory, not a symbolic link")

    def _load(self):
        try:
            for path in sorted(self.directory.iterdir()):
                if path.name in (".lock", ".pending"):
                    continue
                regular_file(path)
                self._register(SessionConfig.parse(strict_json(path.read_bytes().decode("utf-8")), path.name))
            seen_sessions = set(self.sessions)
            seen_requests = set(self.requests)
            seen_channels = set(self.channels)
            for path in sorted(self.pending.iterdir()):
                regular_file(path)
                if path.suffix != ".json":
                    raise ValueError("unexpected pending file")
                sid = valid_ulid(path.stem)
                data = strict_json(path.read_bytes().decode("utf-8"))
                required = {"format_version", "create_request_id", "session_id", "scan_start_seq"}
                if set(data) not in (required, required | {"channel_id"}):
                    raise ValueError("invalid pending fields")
                if type(data["format_version"]) is not int or data["format_version"] != 1 or data["session_id"] != sid:
                    raise ValueError("invalid pending identity")
                request = valid_ulid(data["create_request_id"])
                uint64_string(data["scan_start_seq"])
                if "channel_id" not in data:
                    raise ConfigurationError(f"Channel creation result is uncertain; pending session {sid}")
                record = SessionConfig.parse(data, path.name)
                if sid in seen_sessions or request in seen_requests or record.channel_id in seen_channels:
                    raise ConfigurationError("duplicate session, channel, or creation request")
                seen_sessions.add(sid)
                seen_requests.add(request)
                seen_channels.add(record.channel_id)
                self.recoverable[sid] = record
        except (OSError, ValueError, TypeError) as exc:
            if isinstance(exc, ConfigurationError):
                raise
            entry = path.name if "path" in locals() else self.directory.name
            raise ConfigurationError(f"invalid session configuration entry {entry}") from exc

    def _check_unique(self, session):
        if session.session_id in self.sessions or session.channel_id in self.channels or session.create_request_id in self.requests:
            raise ConfigurationError("duplicate session, channel, or creation request")

    def _register(self, session):
        self._check_unique(session)
        self.sessions[session.session_id] = session
        self.requests[session.create_request_id] = session.session_id
        self.channels.add(session.channel_id)

    def begin(self, session_id, request_id, scan_start_seq):
        data = {"format_version": 1, "session_id": session_id, "create_request_id": request_id,
                "scan_start_seq": str(scan_start_seq)}
        with self.lock:
            write_json(self.pending / (session_id + ".json"), data, exclusive=True)
        return data

    def record_channel(self, pending, channel_id):
        pending = dict(pending, channel_id=str(channel_id))
        with self.lock:
            write_json(self.pending / (pending["session_id"] + ".json"), pending, exclusive=False)
        return pending

    def commit(self, pending):
        with self.lock:
            source = self.pending / (pending["session_id"] + ".json")
            regular_file(source)
            data = strict_json(source.read_bytes().decode("utf-8"))
            if data != pending:
                raise ConfigurationError("pending configuration mismatch")
            session = SessionConfig.parse(data, source.name)
            self._check_unique(session)
            final = self.directory / (session.session_id + ".json")
            if final.exists() or final.is_symlink():
                raise ConfigurationError("session configuration already exists")
            os.replace(source, final)
            regular_file(final)
            confirmed = SessionConfig.parse(strict_json(final.read_bytes().decode("utf-8")), final.name)
            if confirmed != session:
                raise ConfigurationError("committed configuration mismatch")
            self._register(confirmed)
            self.recoverable.pop(session.session_id, None)
        return session

    def close(self):
        if self._directory_lock is not None:
            self._directory_lock.release()
            self._directory_lock = None
