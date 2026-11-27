"""Pure-Python live SQLCipher/SQLite realtime reader for WeChat 4.x databases.

Replaces the removed closed-source ``wechatdb_client.dll`` / ``wechatdb_broker.exe``
with an in-process incremental page-level SQLCipher + WAL mirror backed by Python's
standard ``sqlite3`` and ``cryptography``.

Key properties:
- Zero external binaries, zero 45-day expiration, zero network calls or telemetry.
- Never modifies or locks WeChat's ``db_storage`` files (reads ``.db`` / ``.db-wal``
  snapshots in shared read-only mode).
- Sub-millisecond incremental updates when WeChat appends new messages to ``.db-wal``:
  only newly changed 4 KiB WAL pages are AES-decrypted and patched in-place, bumping
  SQLite's ``file_change_counter`` so open ``sqlite3`` connections immediately observe
  new rows and tables without closing/reopening or triggering ``WinError 32`` on Windows.
"""

from __future__ import annotations

import atexit
import hashlib
import hmac
import itertools
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .app_paths import get_output_databases_dir
from .logging_config import get_logger
from .sqlite_wal import _checksum as _wal_checksum
from .wechat_decrypt import (
    HMAC_SIZE,
    IV_SIZE,
    PAGE_SIZE,
    RESERVE_SIZE,
    SALT_SIZE,
    SQLITE_HEADER,
    _compute_page_hmac,
    _resolve_page1_key_material,
)

logger = get_logger(__name__)

_HANDLE_START = 1 << 60
_CURSOR_START = 1 << 59
_READ_PREFIXES = {"SELECT", "PRAGMA", "EXPLAIN", "WITH"}
_KEY_MATERIAL_CACHE_LIMIT = 512
_STABLE_READ_ATTEMPTS = 5
_STABLE_READ_BACKOFF_S = 0.006


class NativeCoreRealtimeError(RuntimeError):
    pass


def _smart_text_factory(raw: bytes) -> str | bytes:
    """Decode UTF-8 TEXT columns as str while preserving binary zstd blobs stored in TEXT."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return bytes(raw)


_key_cache_lock = threading.Lock()
_key_material_cache: OrderedDict[tuple[bytes, bytes], tuple[bytes, bytes, str]] = OrderedDict()


def _resolve_cached_key_material(
    key_bytes: bytes, page1: bytes
) -> tuple[bytes, bytes, str] | None:
    if len(page1) < PAGE_SIZE:
        return None
    cache_key = (hashlib.sha256(key_bytes).digest(), bytes(page1[:SALT_SIZE]))
    with _key_cache_lock:
        cached = _key_material_cache.get(cache_key)
        if cached is not None:
            enc_key, mac_key, mode = cached
            stored_hmac = page1[PAGE_SIZE - HMAC_SIZE : PAGE_SIZE]
            if hmac.compare_digest(stored_hmac, _compute_page_hmac(mac_key, page1, 1)):
                _key_material_cache.move_to_end(cache_key)
                return cached
            _key_material_cache.pop(cache_key, None)

    resolved = _resolve_page1_key_material(key_bytes, page1)
    if resolved is None:
        return None
    with _key_cache_lock:
        _key_material_cache[cache_key] = resolved
        while len(_key_material_cache) > _KEY_MATERIAL_CACHE_LIMIT:
            _key_material_cache.popitem(last=False)
    return resolved


def _decrypt_cipher_page(aes_algo: algorithms.AES, page: bytes | memoryview, page_num: int) -> bytes:
    iv = bytes(page[PAGE_SIZE - RESERVE_SIZE : PAGE_SIZE - RESERVE_SIZE + IV_SIZE])
    offset = SALT_SIZE if page_num == 1 else 0
    encrypted_slice = page[offset : PAGE_SIZE - RESERVE_SIZE]
    decryptor = Cipher(aes_algo, modes.CBC(iv), backend=default_backend()).decryptor()
    plain = decryptor.update(encrypted_slice) + decryptor.finalize()
    if page_num == 1:
        return SQLITE_HEADER + plain + (b"\x00" * RESERVE_SIZE)
    return plain + (b"\x00" * RESERVE_SIZE)


def _page_tag(raw_page: bytes | memoryview, *, encrypted: bool) -> bytes:
    if encrypted:
        return bytes(raw_page[-16:])
    return hashlib.blake2b(raw_page, digest_size=16).digest()


def _stat_sig(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except FileNotFoundError:
        return (0, 0)
    except OSError:
        return (0, 0)
    mtime_ns = int(getattr(st, "st_mtime_ns", 0) or int(st.st_mtime * 1_000_000_000))
    return (mtime_ns, int(st.st_size))


def _read_optional_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def _parse_committed_wal_pages(
    wal: bytes,
    base_pages: int,
    page_size: int,
    *,
    verify_page: Callable[[bytes, int], bool] | None = None,
) -> tuple[dict[int, bytes], int]:
    """Extract latest committed pages ``{pgno: raw_page}`` and authoritative ``db_size``.

    Because SQLite WAL frame checksums chain cumulatively from byte 0, stopping at the
    first salt mismatch or incomplete/uncommitted tail frame while WeChat is actively
    appending yields the exact ACID-consistent state as of the latest committed frame.
    """
    if not wal or len(wal) < 32:
        return {}, base_pages
    magic, version, wal_page_size = struct.unpack(">III", wal[:12])
    if magic not in (0x377F0682, 0x377F0683) or version != 3007000:
        return {}, base_pages
    if wal_page_size != page_size:
        return {}, base_pages
    endian = "<" if magic == 0x377F0682 else ">"
    state = _wal_checksum(wal[:24], endian)
    if state != struct.unpack(">II", wal[24:32]):
        return {}, base_pages

    salt = wal[16:24]
    frame_size = 24 + page_size
    pending: dict[int, bytes] = {}
    committed: dict[int, bytes] = {}
    effective_base = base_pages
    committed_size = base_pages

    for offset in range(32, len(wal) - frame_size + 1, frame_size):
        header = wal[offset : offset + 24]
        if header[8:16] != salt:
            break
        pgno, db_size = struct.unpack(">II", header[:8])
        if pgno == 0 or pgno > 0xFFFFFFFE:
            break
        page = wal[offset + 24 : offset + frame_size]
        state = _wal_checksum(page, endian, _wal_checksum(header[:8], endian, state))
        if state != struct.unpack(">II", header[16:24]):
            break
        pending[pgno] = page
        if db_size:
            candidate = dict(committed)
            candidate.update(pending)
            pending.clear()
            candidate = {n: p for n, p in candidate.items() if n <= db_size}
            next_base = min(effective_base, db_size)
            extension = sum(n > next_base for n in candidate)
            if db_size - next_base != extension:
                break
            committed = candidate
            effective_base = next_base
            committed_size = db_size

    if not committed:
        return {}, base_pages

    if verify_page is not None:
        for pgno, page in committed.items():
            if not verify_page(page, pgno):
                raise NativeCoreRealtimeError(f"Live WAL page {pgno} failed HMAC authentication.")

    return committed, committed_size


_cache_root_lock = threading.Lock()
_process_cache_root: Path | None = None


def _sweep_dead_process_caches(parent_dir: Path) -> None:
    try:
        import psutil
    except Exception:
        psutil = None  # type: ignore[assignment]
    if psutil is None:
        return
    try:
        entries = list(parent_dir.iterdir())
    except OSError:
        return
    current_pid = os.getpid()
    for entry in entries:
        name = entry.name
        if not name.startswith("p") or not name[1:].isdigit():
            continue
        pid = int(name[1:])
        if pid == current_pid:
            continue
        try:
            if not psutil.pid_exists(pid):
                shutil.rmtree(entry, ignore_errors=True)
        except Exception:
            continue


def _get_process_cache_root() -> Path:
    global _process_cache_root
    with _cache_root_lock:
        if _process_cache_root is not None and _process_cache_root.is_dir():
            return _process_cache_root
        candidates = [
            get_output_databases_dir() / ".realtime_cache",
            Path(tempfile.gettempdir()) / "wechat-data-analysis-realtime",
        ]
        for base in candidates:
            try:
                base.mkdir(parents=True, exist_ok=True)
                _sweep_dead_process_caches(base)
                proc_dir = base / f"p{os.getpid()}"
                proc_dir.mkdir(parents=True, exist_ok=True)
                _process_cache_root = proc_dir
                return proc_dir
            except OSError:
                continue
        fallback = Path(tempfile.mkdtemp(prefix="wda-rt-"))
        _process_cache_root = fallback
        return fallback


class _LiveSQLiteMirror:
    """Maintains an incrementally patched plain SQLite mirror of a live WeChat DB."""

    def __init__(self, source_path: Path, mirror_path: Path, key_bytes: bytes) -> None:
        self.source_path = source_path
        self.wal_path = Path(str(source_path) + "-wal")
        self.mirror_path = mirror_path
        self._key_bytes = bytes(key_bytes)
        self._lock = threading.RLock()

        self._fp: Any = None
        self._conn: sqlite3.Connection | None = None
        self._closed = False

        self._encrypted = True
        self._page_size = PAGE_SIZE
        self._salt = b""
        self._enc_key = b""
        self._mac_key = b""
        self._aes_algo: algorithms.AES | None = None

        self._base_sig: tuple[int, int] | None = None
        self._wal_sig: tuple[int, int] | None = None
        self._base_pages_count = 0
        self._mirror_pages_count = 0
        self._change_counter = 1

        self._base_page1: bytes = b""
        self._base_page_tags = bytearray()
        self._wal_overridden_base_pages: dict[int, bytes] = {}
        self._wal_page_tags: dict[int, bytes] = {}
        self._wal_page1_plain: bytes | None = None

    def _decrypt_one(self, raw_page: bytes | memoryview, page_num: int) -> bytes:
        if not self._encrypted:
            return bytes(raw_page)
        assert self._aes_algo is not None
        return _decrypt_cipher_page(self._aes_algo, raw_page, page_num)

    def _verify_wal_page(self, raw_page: bytes, page_num: int) -> bool:
        if not self._encrypted:
            return True
        return hmac.compare_digest(
            raw_page[PAGE_SIZE - HMAC_SIZE : PAGE_SIZE],
            _compute_page_hmac(self._mac_key, raw_page, page_num),
        )

    def _read_stable_wal(self, expected_base_sig: tuple[int, int]) -> tuple[bytes, tuple[int, int]] | None:
        """Read WAL bytes only if the main DB file signature stayed unchanged."""
        for attempt in range(_STABLE_READ_ATTEMPTS):
            wal_before = _stat_sig(self.wal_path)
            raw_wal = _read_optional_bytes(self.wal_path) if wal_before[1] > 0 else b""
            base_after = _stat_sig(self.source_path)
            if base_after != expected_base_sig:
                return None
            wal_after = _stat_sig(self.wal_path)
            if wal_before == wal_after and len(raw_wal) == wal_after[1]:
                return raw_wal, wal_after
            if attempt + 1 < _STABLE_READ_ATTEMPTS:
                time.sleep(_STABLE_READ_BACKOFF_S * (attempt + 1))
        wal_final = _stat_sig(self.wal_path)
        if _stat_sig(self.source_path) != expected_base_sig:
            return None
        return _read_optional_bytes(self.wal_path), wal_final

    def _read_stable_db_and_wal(self) -> tuple[bytes, bytes, tuple[int, int], tuple[int, int]]:
        last_db = b""
        last_wal = b""
        db_sig = (0, 0)
        wal_sig = (0, 0)
        for attempt in range(_STABLE_READ_ATTEMPTS):
            db_before = _stat_sig(self.source_path)
            wal_before = _stat_sig(self.wal_path)
            try:
                last_db = self.source_path.read_bytes()
            except OSError as exc:
                raise NativeCoreRealtimeError(
                    f"Cannot read database {self.source_path.name}: {exc}"
                ) from exc
            last_wal = _read_optional_bytes(self.wal_path) if wal_before[1] > 0 else b""
            db_after = _stat_sig(self.source_path)
            wal_after = _stat_sig(self.wal_path)
            if (
                db_before == db_after
                and wal_before == wal_after
                and len(last_db) == db_after[1]
                and len(last_wal) == wal_after[1]
            ):
                return last_db, last_wal, db_after, wal_after
            db_sig, wal_sig = db_after, wal_after
            if attempt + 1 < _STABLE_READ_ATTEMPTS:
                time.sleep(_STABLE_READ_BACKOFF_S * (attempt + 1))
        return last_db, last_wal, db_sig, wal_sig

    def _configure_format_from_page1(self, raw_db: bytes) -> tuple[bool, int]:
        """Inspect page 1 and return ``(format_reset_required, page_size)``."""
        if len(raw_db) < 512:
            raise NativeCoreRealtimeError(f"Database file too small: {self.source_path.name}")

        if raw_db.startswith(SQLITE_HEADER):
            if len(raw_db) < 100:
                raise NativeCoreRealtimeError(f"Invalid SQLite header: {self.source_path.name}")
            raw_ps = int.from_bytes(raw_db[16:18], "big")
            page_size = 65536 if raw_ps == 1 else raw_ps
            if page_size < 512 or page_size > 65536 or (page_size & (page_size - 1)) != 0:
                raise NativeCoreRealtimeError(f"Invalid SQLite page size: {page_size}")
            if len(raw_db) < page_size or (len(raw_db) % page_size) != 0:
                raise NativeCoreRealtimeError(
                    f"Unaligned SQLite database size for {self.source_path.name}"
                )
            reset = (self._base_sig is None) or self._encrypted or (self._page_size != page_size)
            self._encrypted = False
            self._page_size = page_size
            self._salt = b""
            self._enc_key = b""
            self._mac_key = b""
            self._aes_algo = None
            return reset, page_size

        if len(raw_db) < PAGE_SIZE:
            raise NativeCoreRealtimeError(f"Encrypted database too small: {self.source_path.name}")

        page1 = raw_db[:PAGE_SIZE]
        salt = page1[:SALT_SIZE]
        resolved = _resolve_cached_key_material(self._key_bytes, page1)
        if resolved is None:
            raise NativeCoreRealtimeError(
                f"Database key authentication failed for {self.source_path.name}"
            )
        enc_key, mac_key, _mode = resolved
        reset = (
            (self._base_sig is None)
            or (not self._encrypted)
            or (self._page_size != PAGE_SIZE)
            or (self._salt != salt)
            or (self._enc_key != enc_key)
        )
        self._encrypted = True
        self._page_size = PAGE_SIZE
        self._salt = salt
        self._enc_key = enc_key
        self._mac_key = mac_key
        self._aes_algo = algorithms.AES(enc_key)
        return reset, PAGE_SIZE

    def _ensure_handles_open(self) -> None:
        if self._fp is None:
            self.mirror_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.mirror_path.exists():
                self.mirror_path.write_bytes(b"")
            self._fp = self.mirror_path.open("r+b")
        if self._conn is None:
            conn = sqlite3.connect(
                str(self.mirror_path),
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.text_factory = _smart_text_factory
            self._conn = conn

    def _write_effective_page1(self, committed_db_size: int) -> None:
        source_p1 = self._wal_page1_plain if self._wal_page1_plain is not None else self._base_page1
        p1 = bytearray(source_p1)
        self._change_counter = ((self._change_counter + 1) & 0xFFFFFFFF) or 1
        p1[18:20] = b"\x01\x01"
        p1[24:28] = self._change_counter.to_bytes(4, "big")
        p1[28:32] = int(committed_db_size).to_bytes(4, "big")
        self._fp.seek(0)
        self._fp.write(p1)

    def _apply_wal_overlay(
        self,
        raw_wal: bytes,
        *,
        raw_base_db: bytes | None = None,
    ) -> int:
        page_size = self._page_size
        committed_pages, committed_db_size = _parse_committed_wal_pages(
            raw_wal,
            self._base_pages_count,
            page_size,
            verify_page=self._verify_wal_page if self._encrypted else None,
        )

        # Restore any base pages previously overridden by WAL that are no longer overridden.
        for pgno in list(self._wal_overridden_base_pages.keys()):
            if pgno in committed_pages:
                continue
            base_plain = self._wal_overridden_base_pages.pop(pgno)
            if pgno <= committed_db_size:
                self._fp.seek((pgno - 1) * page_size)
                self._fp.write(base_plain)

        # If a previous WAL truncated the mirror below base_pages_count and now grew back,
        # refill any uncovered base pages between mirror_pages_count+1 .. min(base_pages_count, committed_db_size).
        uncovered_limit = min(self._base_pages_count, committed_db_size)
        if self._mirror_pages_count < uncovered_limit:
            source_bytes = raw_base_db if raw_base_db is not None else self.source_path.read_bytes()
            mv = memoryview(source_bytes)
            for pgno in range(max(2, self._mirror_pages_count + 1), uncovered_limit + 1):
                if pgno in committed_pages:
                    continue
                start = (pgno - 1) * page_size
                raw_page = mv[start : start + page_size]
                if len(raw_page) < page_size:
                    raw_page = bytes(raw_page) + (b"\x00" * (page_size - len(raw_page)))
                plain = self._decrypt_one(raw_page, pgno)
                self._fp.seek(start)
                self._fp.write(plain)

        next_wal_tags: dict[int, bytes] = {}
        if 1 not in committed_pages:
            self._wal_page1_plain = None

        for pgno, raw_wal_page in committed_pages.items():
            tag = _page_tag(raw_wal_page, encrypted=self._encrypted)
            next_wal_tags[pgno] = tag
            if self._wal_page_tags.get(pgno) == tag and (
                pgno != 1 or self._wal_page1_plain is not None
            ):
                continue

            plain_wal = self._decrypt_one(raw_wal_page, pgno)
            if pgno == 1:
                self._wal_page1_plain = plain_wal
                continue

            offset = (pgno - 1) * page_size
            if pgno <= self._base_pages_count and pgno not in self._wal_overridden_base_pages:
                self._fp.seek(offset)
                existing_base = self._fp.read(page_size)
                if len(existing_base) == page_size:
                    self._wal_overridden_base_pages[pgno] = existing_base

            self._fp.seek(offset)
            self._fp.write(plain_wal)

        self._wal_page_tags = next_wal_tags
        target_bytes = committed_db_size * page_size
        if committed_db_size != self._mirror_pages_count:
            self._fp.truncate(target_bytes)
        self._write_effective_page1(committed_db_size)
        self._fp.flush()
        self._mirror_pages_count = committed_db_size
        return committed_db_size

    def _refresh_locked(self) -> None:
        cur_base_sig = _stat_sig(self.source_path)
        if cur_base_sig == (0, 0):
            raise NativeCoreRealtimeError(f"Database does not exist: {self.source_path}")
        cur_wal_sig = _stat_sig(self.wal_path)

        # Fast path: neither main DB nor WAL changed.
        if (
            self._base_sig is not None
            and cur_base_sig == self._base_sig
            and cur_wal_sig == self._wal_sig
            and self._conn is not None
        ):
            return

        # Incremental WAL-only path: main DB untouched, only -wal changed.
        if (
            self._base_sig is not None
            and cur_base_sig == self._base_sig
            and self._fp is not None
        ):
            stable_wal = self._read_stable_wal(cur_base_sig)
            if stable_wal is not None:
                raw_wal, wal_sig = stable_wal
                self._apply_wal_overlay(raw_wal)
                self._wal_sig = wal_sig
                self._ensure_handles_open()
                return

        # Main DB is new or checkpointed.
        raw_db, raw_wal, base_sig, wal_sig = self._read_stable_db_and_wal()
        full_reset, page_size = self._configure_format_from_page1(raw_db)
        new_base_pages = (len(raw_db) + page_size - 1) // page_size
        mv = memoryview(raw_db)

        if self._fp is None:
            self.mirror_path.parent.mkdir(parents=True, exist_ok=True)
            self._fp = self.mirror_path.open("w+b")

        if full_reset:
            self._wal_overridden_base_pages.clear()
            self._wal_page_tags.clear()
            self._wal_page1_plain = None

            plain_buf = bytearray(new_base_pages * page_size)
            plain_mv = memoryview(plain_buf)
            new_tags = bytearray(new_base_pages * 16)

            for idx in range(new_base_pages):
                pgno = idx + 1
                start = idx * page_size
                raw_page = mv[start : start + page_size]
                if len(raw_page) < page_size:
                    raw_page = bytes(raw_page) + (b"\x00" * (page_size - len(raw_page)))
                new_tags[idx * 16 : (idx + 1) * 16] = _page_tag(
                    raw_page, encrypted=self._encrypted
                )
                plain_page = self._decrypt_one(raw_page, pgno)
                if pgno == 1:
                    self._base_page1 = plain_page
                plain_mv[start : start + page_size] = plain_page

            self._fp.seek(0)
            self._fp.write(plain_buf)
            self._fp.truncate(len(plain_buf))
            self._base_pages_count = new_base_pages
            self._mirror_pages_count = new_base_pages
            self._base_page_tags = new_tags
        else:
            # Restore prior WAL overrides before diffing base pages against tags.
            for pgno, base_plain in self._wal_overridden_base_pages.items():
                if pgno <= self._mirror_pages_count:
                    self._fp.seek((pgno - 1) * page_size)
                    self._fp.write(base_plain)
            self._wal_overridden_base_pages.clear()
            self._wal_page_tags.clear()
            self._wal_page1_plain = None

            old_pages = self._base_pages_count
            old_tags = self._base_page_tags
            new_tags = bytearray(new_base_pages * 16)

            for idx in range(new_base_pages):
                pgno = idx + 1
                start = idx * page_size
                raw_page = mv[start : start + page_size]
                if len(raw_page) < page_size:
                    raw_page = bytes(raw_page) + (b"\x00" * (page_size - len(raw_page)))
                tag = _page_tag(raw_page, encrypted=self._encrypted)
                new_tags[idx * 16 : (idx + 1) * 16] = tag

                tag_same = (
                    idx < old_pages
                    and pgno <= self._mirror_pages_count
                    and old_tags[idx * 16 : (idx + 1) * 16] == tag
                )
                if tag_same:
                    continue

                plain_page = self._decrypt_one(raw_page, pgno)
                if pgno == 1:
                    self._base_page1 = plain_page
                else:
                    self._fp.seek(start)
                    self._fp.write(plain_page)

            self._base_pages_count = new_base_pages
            self._base_page_tags = new_tags
            if new_base_pages != self._mirror_pages_count:
                self._fp.truncate(new_base_pages * page_size)
                self._mirror_pages_count = new_base_pages

        self._apply_wal_overlay(raw_wal, raw_base_db=raw_db)
        self._base_sig = base_sig
        self._wal_sig = wal_sig
        self._ensure_handles_open()

    def query(self, sql: str) -> list[dict[str, Any]]:
        with self._lock:
            if self._closed:
                raise NativeCoreRealtimeError("Live mirror database is closed.")
            self._refresh_locked()
            assert self._conn is not None
            cur = self._conn.execute(sql)
            try:
                rows = [dict(row) for row in cur.fetchall()]
            finally:
                cur.close()
            return rows

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            conn, self._conn = self._conn, None
            fp, self._fp = self._fp, None
            self._wal_overridden_base_pages.clear()
            self._wal_page_tags.clear()
            self._base_page_tags = bytearray()
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if fp is not None:
                try:
                    fp.close()
                except Exception:
                    pass
            try:
                self.mirror_path.unlink(missing_ok=True)
            except OSError:
                pass


@dataclass
class _AccountContext:
    handle: int
    account: str
    native_wxid: str
    db_storage_dir: Path
    session_db_path: Path
    key: bytearray = field(repr=False)
    cache_dir: Path = field(default_factory=Path)
    mirrors: dict[str, _LiveSQLiteMirror] = field(default_factory=dict, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    closed: bool = False

    def get_mirror(self, database_path: Path) -> _LiveSQLiteMirror:
        norm_key = os.path.normcase(os.fspath(database_path))
        with self.lock:
            if self.closed:
                raise NativeCoreRealtimeError("Realtime account handle is closed.")
            existing = self.mirrors.get(norm_key)
            if existing is not None:
                return existing
            digest = hashlib.sha256(norm_key.encode("utf-8")).hexdigest()[:12]
            mirror_path = self.cache_dir / f"{digest}_{database_path.name}"
            mirror = _LiveSQLiteMirror(database_path, mirror_path, bytes(self.key))
            self.mirrors[norm_key] = mirror
            return mirror

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            mirrors = list(self.mirrors.values())
            self.mirrors.clear()
            self.key[:] = b"\0" * len(self.key)
        for mirror in mirrors:
            mirror.close()
        if self.cache_dir and str(self.cache_dir) != ".":
            shutil.rmtree(self.cache_dir, ignore_errors=True)


@dataclass
class _MessageCursor:
    cursor: int
    account_handle: int
    username: str
    batch_size: int
    ascending: bool
    begin_timestamp: int
    end_timestamp: int
    tables: tuple[tuple[Path, str], ...]
    last_by_path: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    exhausted: bool = False


_registry_lock = threading.RLock()
_handles = itertools.count(_HANDLE_START)
_cursors = itertools.count(_CURSOR_START)
_accounts: dict[int, _AccountContext] = {}
_message_cursors: dict[int, _MessageCursor] = {}


def _context(handle: int) -> _AccountContext:
    try:
        value = int(handle)
    except Exception as exc:
        raise NativeCoreRealtimeError(f"Invalid realtime handle: {handle!r}") from exc
    with _registry_lock:
        ctx = _accounts.get(value)
    if ctx is None or ctx.closed:
        raise NativeCoreRealtimeError(f"Realtime account handle {value} is not open.")
    return ctx


def is_native_core_handle(handle: int) -> bool:
    try:
        value = int(handle)
    except Exception:
        return False
    with _registry_lock:
        ctx = _accounts.get(value)
        return ctx is not None and not ctx.closed


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return "X'" + bytes(value).hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def _quote_identifier(value: str) -> str:
    return '"' + str(value or "").replace('"', '""') + '"'


def _first_sql_keyword(sql: str) -> str:
    remaining = str(sql or "")
    while True:
        remaining = remaining.lstrip()
        if remaining.startswith("--"):
            newline = remaining.find("\n")
            remaining = "" if newline < 0 else remaining[newline + 1 :]
            continue
        if remaining.startswith("/*"):
            end = remaining.find("*/", 2)
            remaining = "" if end < 0 else remaining[end + 2 :]
            continue
        break
    match = re.match(r"[A-Za-z]+", remaining)
    return match.group(0).upper() if match else ""


def _canonical_database_path(context: _AccountContext, value: Path | str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise NativeCoreRealtimeError(f"Realtime database does not exist: {value}") from exc
    root = context.db_storage_dir
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise NativeCoreRealtimeError("Realtime database path escaped db_storage.") from exc
    if not relative.parts or not path.is_file():
        raise NativeCoreRealtimeError("Realtime database path is not a file under db_storage.")
    return path


def _default_database_path(context: _AccountContext, kind: str) -> Path:
    normalized = str(kind or "").strip().lower()
    candidates = {
        "contact": ("contact/contact.db", "contact.db", "MicroMsg.db"),
        "session": ("session/session.db", "session.db", "Session.db", "MicroMsg.db"),
        "media": ("sns/sns.db", "sns.db"),
        "sns": ("sns/sns.db", "sns.db"),
        "favorite": ("favorite/favorite.db", "favorite.db"),
        "general": ("general/general.db", "general.db"),
        "hardlink": ("hardlink/hardlink.db", "hardlink.db"),
    }.get(normalized, ())
    for relative in candidates:
        candidate = context.db_storage_dir / relative
        try:
            if candidate.is_file():
                return _canonical_database_path(context, candidate)
        except OSError:
            continue
    raise NativeCoreRealtimeError(
        f"An explicit database path is required for realtime kind {normalized!r}."
    )


def _query(context: _AccountContext, database_path: Path, sql: str) -> list[dict[str, Any]]:
    mirror = context.get_mirror(database_path)
    try:
        return mirror.query(sql)
    except NativeCoreRealtimeError:
        raise
    except Exception as exc:
        raise NativeCoreRealtimeError(
            f"SQLite query failed on {database_path.name}: {exc}"
        ) from exc


def open_account(
    *,
    account: str,
    native_wxid: str,
    db_storage_dir: Path,
    session_db_path: Path,
    key_hex: str,
) -> int:
    encoded = str(key_hex or "").strip()
    if not re.fullmatch(r"[0-9A-Fa-f]{64}", encoded):
        raise NativeCoreRealtimeError("A 32-byte database key is required.")
    try:
        root = Path(db_storage_dir).expanduser().resolve(strict=True)
        session_path = Path(session_db_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise NativeCoreRealtimeError(f"Invalid account database root: {exc}") from exc

    if not root.is_dir() or root not in session_path.parents or not session_path.is_file():
        raise NativeCoreRealtimeError("Invalid account database root.")

    handle = next(_handles)
    root_digest = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:12]
    cache_dir = _get_process_cache_root() / f"h{handle}_{root_digest}"
    context = _AccountContext(
        handle=handle,
        account=str(account or "").strip(),
        native_wxid=str(native_wxid or "").strip(),
        db_storage_dir=root,
        session_db_path=session_path,
        key=bytearray.fromhex(encoded),
        cache_dir=cache_dir,
    )
    try:
        probe = _query(context, session_path, "SELECT 1 AS realtime_probe")
        if not probe or int(probe[0].get("realtime_probe") or 0) != 1:
            raise NativeCoreRealtimeError("Session database probe failed.")
        with _registry_lock:
            _accounts[handle] = context
        return handle
    except BaseException:
        context.close()
        raise


def close_account(handle: int) -> None:
    try:
        value = int(handle)
    except Exception:
        return
    with _registry_lock:
        context = _accounts.pop(value, None)
        stale = [key for key, cur in _message_cursors.items() if cur.account_handle == value]
        for key in stale:
            _message_cursors.pop(key, None)
    if context is not None:
        context.close()


def close_all() -> None:
    global _process_cache_root
    with _registry_lock:
        contexts = list(_accounts.values())
        _accounts.clear()
        _message_cursors.clear()
    for context in contexts:
        context.close()
    with _cache_root_lock:
        proc_dir = _process_cache_root
        _process_cache_root = None
    if proc_dir is not None:
        shutil.rmtree(proc_dir, ignore_errors=True)


atexit.register(close_all)


def exec_query(
    handle: int,
    *,
    kind: str,
    path: str | None,
    sql: str,
) -> list[dict[str, Any]]:
    context = _context(handle)
    statement = str(sql or "").strip()
    if not statement:
        return []
    database_path = (
        _default_database_path(context, kind)
        if path is None or not str(path).strip()
        else _canonical_database_path(context, path)
    )
    keyword = _first_sql_keyword(statement)
    if keyword in _READ_PREFIXES:
        return _query(context, database_path, statement)
    raise NativeCoreRealtimeError("Realtime raw SQL is read-only.")


def get_sessions(handle: int) -> list[dict[str, Any]]:
    context = _context(handle)
    try:
        return _query(
            context,
            context.session_db_path,
            "SELECT * FROM SessionTable ORDER BY sort_timestamp DESC",
        )
    except NativeCoreRealtimeError:
        return _query(
            context,
            context.session_db_path,
            "SELECT * FROM SessionTable ORDER BY last_timestamp DESC",
        )


def _message_database_paths(context: _AccountContext, username: str) -> tuple[Path, ...]:
    candidates_dirs = [context.db_storage_dir / "message", context.db_storage_dir]
    paths: list[Path] = []
    seen_norm: set[str] = set()
    for directory in candidates_dirs:
        try:
            entries = [p for p in directory.iterdir() if p.is_file()]
        except OSError:
            continue
        for entry in entries:
            norm = os.path.normcase(os.fspath(entry))
            if norm not in seen_norm:
                seen_norm.add(norm)
                paths.append(entry)

    normal = sorted(
        (p for p in paths if re.fullmatch(r"message(?:_\d+)?\.db", p.name, re.I)),
        key=lambda v: v.name.lower(),
    )
    business = sorted(
        (p for p in paths if re.fullmatch(r"biz_message(?:_\d+)?\.db", p.name, re.I)),
        key=lambda v: v.name.lower(),
    )
    other = sorted(
        (
            p
            for p in paths
            if p.suffix.lower() == ".db"
            and "message" in p.name.lower()
            and p not in normal
            and p not in business
            and p.name.lower() not in {"message_fts.db", "message_resource.db"}
        ),
        key=lambda v: v.name.lower(),
    )
    ordered = (
        business + normal + other
        if str(username).startswith("gh_")
        else normal + business + other
    )
    return tuple(_canonical_database_path(context, p) for p in ordered)


def _message_tables(context: _AccountContext, username: str) -> tuple[tuple[Path, str], ...]:
    expected = "Msg_" + hashlib.md5(str(username).encode("utf-8")).hexdigest()
    lookup = (
        "SELECT name FROM sqlite_master WHERE type='table' AND lower(name)=lower("
        + _sql_literal(expected)
        + ") LIMIT 1"
    )
    found: list[tuple[Path, str]] = []
    for path in _message_database_paths(context, username):
        try:
            rows = _query(context, path, lookup)
        except Exception:
            continue
        if rows and str(rows[0].get("name") or "").strip():
            found.append((path, str(rows[0]["name"])))
    return tuple(found)


def _message_select_sql(
    table: str,
    *,
    ascending: bool,
    limit: int,
    begin_timestamp: int = 0,
    end_timestamp: int = 0,
    last: tuple[int, int, int] | None = None,
) -> str:
    direction = "ASC" if ascending else "DESC"
    where: list[str] = []
    if begin_timestamp > 0:
        where.append(f"m.create_time >= {int(begin_timestamp)}")
    if end_timestamp > 0:
        where.append(f"m.create_time <= {int(end_timestamp)}")
    if last is not None:
        create_time, sort_seq, local_id = (int(v) for v in last)
        comparator = ">" if ascending else "<"
        where.append(
            "(m.create_time {cmp} {ct} OR "
            "(m.create_time = {ct} AND COALESCE(m.sort_seq, 0) {cmp} {ss}) OR "
            "(m.create_time = {ct} AND COALESCE(m.sort_seq, 0) = {ss} "
            "AND m.local_id {cmp} {lid}))".format(
                cmp=comparator, ct=create_time, ss=sort_seq, lid=local_id
            )
        )
    suffix = " WHERE " + " AND ".join(where) if where else ""
    quoted = _quote_identifier(table)
    return (
        "SELECT m.local_id, m.server_id, m.local_type, m.sort_seq, "
        "m.real_sender_id, m.create_time, m.message_content, m.compress_content, "
        "m.packed_info_data AS packed_info_data, m.source AS msg_source, "
        "n.user_name AS sender_username "
        f"FROM {quoted} m LEFT JOIN Name2Id n ON m.real_sender_id = n.rowid"
        f"{suffix} ORDER BY m.sort_seq {direction}, m.local_id {direction} "
        f"LIMIT {max(1, int(limit))}"
    )


def _message_rows_for_table(
    context: _AccountContext,
    path: Path,
    table: str,
    **options: Any,
) -> list[dict[str, Any]]:
    statements = [_message_select_sql(table, **options)]
    statements.append(statements[0].replace("m.source AS msg_source", "NULL AS msg_source"))
    statements.append(
        statements[-1].replace(
            "m.packed_info_data AS packed_info_data", "NULL AS packed_info_data"
        )
    )
    statements.append(
        statements[-1].replace(
            "LEFT JOIN Name2Id n ON m.real_sender_id = n.rowid", ""
        ).replace("n.user_name AS sender_username", "NULL AS sender_username")
    )
    last_error: Exception | None = None
    for statement in statements:
        try:
            rows = _query(context, path, statement)
            for row in rows:
                row["_db_path"] = str(path)
                row["db_name"] = path.name
                row["table_name"] = table
            return rows
        except Exception as exc:
            last_error = exc
    raise NativeCoreRealtimeError(
        f"Cannot query realtime message table {path.name}/{table}: {last_error}"
    )


def open_message_cursor(
    handle: int,
    username: str,
    *,
    batch_size: int,
    ascending: bool = False,
    begin_timestamp: int = 0,
    end_timestamp: int = 0,
) -> int:
    context = _context(handle)
    user = str(username or "").strip()
    if not user:
        return 0
    tables = _message_tables(context, user)
    cursor_id = next(_cursors)
    state = _MessageCursor(
        cursor=cursor_id,
        account_handle=context.handle,
        username=user,
        batch_size=max(1, min(int(batch_size or 1), 4096)),
        ascending=bool(ascending),
        begin_timestamp=max(0, int(begin_timestamp or 0)),
        end_timestamp=max(0, int(end_timestamp or 0)),
        tables=tables,
    )
    with _registry_lock:
        _message_cursors[cursor_id] = state
    return cursor_id


def fetch_message_batch(handle: int, cursor: int) -> tuple[list[dict[str, Any]], bool]:
    context = _context(handle)
    with _registry_lock:
        state = _message_cursors.get(int(cursor or 0))
    if state is None or state.account_handle != context.handle or state.exhausted:
        return [], False

    candidates: list[dict[str, Any]] = []
    probe = state.batch_size + 1
    for path, table in state.tables:
        key = os.path.normcase(os.fspath(path))
        rows = _message_rows_for_table(
            context,
            path,
            table,
            ascending=state.ascending,
            limit=probe,
            begin_timestamp=state.begin_timestamp,
            end_timestamp=state.end_timestamp,
            last=state.last_by_path.get(key),
        )
        candidates.extend(rows)

    candidates.sort(
        key=lambda row: (
            int(row.get("create_time") or 0),
            int(row.get("sort_seq") or 0),
            int(row.get("local_id") or 0),
            os.path.normcase(str(row.get("_db_path") or "")),
        ),
        reverse=not state.ascending,
    )
    has_more = len(candidates) > state.batch_size
    selected = candidates[: state.batch_size]
    for row in selected:
        path_key = os.path.normcase(str(row.get("_db_path") or ""))
        state.last_by_path[path_key] = (
            int(row.get("create_time") or 0),
            int(row.get("sort_seq") or 0),
            int(row.get("local_id") or 0),
        )
    if not has_more:
        state.exhausted = True
    return selected, has_more


def close_message_cursor(handle: int, cursor: int) -> None:
    context = _context(handle)
    with _registry_lock:
        state = _message_cursors.get(int(cursor or 0))
        if state is not None and state.account_handle == context.handle:
            _message_cursors.pop(state.cursor, None)


def get_messages(
    handle: int,
    username: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    wanted = max(0, int(limit or 0))
    skipped = max(0, int(offset or 0))
    if wanted <= 0:
        return []
    cursor = open_message_cursor(
        handle,
        username,
        batch_size=min(4096, max(1, skipped + wanted)),
    )
    collected: list[dict[str, Any]] = []
    try:
        while len(collected) < skipped + wanted:
            rows, has_more = fetch_message_batch(handle, cursor)
            collected.extend(rows)
            if not has_more:
                break
        return collected[skipped : skipped + wanted]
    finally:
        close_message_cursor(handle, cursor)


def get_message_count(handle: int, username: str) -> int:
    context = _context(handle)
    total = 0
    for path, table in _message_tables(context, str(username or "").strip()):
        rows = _query(
            context,
            path,
            f"SELECT COUNT(*) AS message_count FROM {_quote_identifier(table)}",
        )
        if rows:
            total += int(rows[0].get("message_count") or 0)
    return total


def _contact_rows(context: _AccountContext, usernames: list[str] | None) -> list[dict[str, Any]]:
    database_path = _default_database_path(context, "contact")
    targets = list(
        dict.fromkeys(
            str(value or "").strip()
            for value in (usernames or [])
            if str(value or "").strip()
        )
    )
    where = ""
    if targets:
        where = " WHERE username IN (" + ",".join(_sql_literal(v) for v in targets) + ")"
    result: dict[str, dict[str, Any]] = {}
    for table in (("contact", "stranger") if targets else ("contact",)):
        try:
            rows = _query(context, database_path, f"SELECT * FROM {table}{where}")
        except Exception:
            if not targets:
                raise
            continue
        for row in rows:
            username = str(row.get("username") or row.get("user_name") or "").strip()
            if username and username not in result:
                result[username] = row
    return list(result.values())


def get_contact(handle: int, username: str) -> dict[str, Any]:
    context = _context(handle)
    rows = _contact_rows(context, [username])
    return rows[0] if rows else {}


def get_contacts_compact(
    handle: int, usernames: list[str] | None = None
) -> list[dict[str, Any]]:
    return _contact_rows(_context(handle), usernames)


def _display_name(row: dict[str, Any], fallback: str) -> str:
    for key in ("remark", "nick_name", "nickname", "alias"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return fallback


def get_display_names(handle: int, usernames: list[str]) -> dict[str, str]:
    context = _context(handle)
    try:
        rows = _contact_rows(context, usernames)
    except Exception:
        return {}
    result: dict[str, str] = {}
    for row in rows:
        username = str(row.get("username") or row.get("user_name") or "").strip()
        if username:
            result[username] = _display_name(row, username)
    return result


def get_avatar_urls(handle: int, usernames: list[str]) -> dict[str, str]:
    context = _context(handle)
    try:
        rows = _contact_rows(context, usernames)
    except Exception:
        return {}
    result: dict[str, str] = {}
    for row in rows:
        username = str(row.get("username") or row.get("user_name") or "").strip()
        url = str(row.get("big_head_url") or row.get("small_head_url") or "").strip()
        if username and url:
            result[username] = url
    return result


def get_group_members(handle: int, chatroom_id: str) -> list[dict[str, Any]]:
    context = _context(handle)
    room = str(chatroom_id or "").strip()
    if not room:
        return []
    try:
        database_path = _default_database_path(context, "contact")
    except Exception:
        return []
    room_literal = _sql_literal(room)
    statements = (
        "SELECT nm.username AS username FROM chatroom_member cm "
        "JOIN name2id nm ON nm.rowid=cm.member_id "
        "JOIN chat_room cr ON cr.id=cm.room_id "
        f"WHERE cr.username={room_literal} ORDER BY cm.rowid",
        "SELECT nm.user_name AS username FROM chatroom_member cm "
        "JOIN name2id nm ON nm.rowid=cm.member_id "
        "JOIN chat_room cr ON cr.id=cm.room_id "
        f"WHERE cr.username={room_literal} ORDER BY cm.rowid",
    )
    for statement in statements:
        try:
            rows = _query(context, database_path, statement)
        except Exception:
            continue
        members = [
            {"username": str(row.get("username") or "").strip()}
            for row in rows
            if str(row.get("username") or "").strip()
        ]
        if members:
            return members
    return []


def get_group_nicknames(handle: int, chatroom_id: str) -> dict[str, str]:
    members = get_group_members(handle, chatroom_id)
    usernames = [str(item.get("username") or "") for item in members]
    return get_display_names(handle, usernames)


def get_sns_timeline(
    handle: int,
    *,
    limit: int = 20,
    offset: int = 0,
    usernames: list[str] | None = None,
    keyword: str | None = None,
    start_time: int = 0,
    end_time: int = 0,
) -> list[dict[str, Any]]:
    context = _context(handle)
    wanted = max(0, min(int(limit or 0), 5000))
    skipped = max(0, int(offset or 0))
    if wanted <= 0:
        return []
    sns_db = _default_database_path(context, "sns")
    filters = ["content IS NOT NULL", "content != ''"]
    users = list(
        dict.fromkeys(
            str(value or "").strip()
            for value in (usernames or [])
            if str(value or "").strip()
        )
    )
    if users:
        filters.append("user_name IN (" + ",".join(_sql_literal(v) for v in users) + ")")
    if str(keyword or "").strip():
        escaped = str(keyword).replace("'", "''")
        filters.append(f"content LIKE '%{escaped}%'")
    where = " WHERE " + " AND ".join(filters)
    probe = min(20_000, skipped + wanted + 2048)
    base = (
        "SELECT tid, user_name, content, pack_info_buf FROM SnsTimeLine"
        f"{where} ORDER BY tid DESC LIMIT {probe}"
    )
    try:
        rows = _query(context, sns_db, base)
    except Exception:
        rows = _query(context, sns_db, base.replace(", pack_info_buf", ""))

    from .routers.sns import _decode_sns_text_blob, _parse_timeline_xml

    parsed_rows: list[dict[str, Any]] = []
    for row in rows:
        try:
            tid = int(row.get("tid") or 0)
        except Exception:
            continue
        username = str(row.get("user_name") or "").strip()
        raw_xml = _decode_sns_text_blob(row.get("content"))
        parsed = _parse_timeline_xml(raw_xml, username)
        created = int(parsed.get("createTime") or 0)
        if start_time and created < int(start_time):
            continue
        if end_time and created > int(end_time):
            continue
        parsed_rows.append(
            {
                "id": str(tid & ((1 << 64) - 1)),
                "tid": str(tid & ((1 << 64) - 1)),
                "username": str(parsed.get("username") or username),
                "nickname": "",
                "createTime": created,
                "contentDesc": str(parsed.get("contentDesc") or ""),
                "location": str(parsed.get("location") or ""),
                "sourceName": str(parsed.get("sourceName") or ""),
                "media": parsed.get("media") or [],
                "likes": parsed.get("likes") or [],
                "comments": parsed.get("comments") or [],
                "type": int(parsed.get("type") or 1),
                "title": str(parsed.get("title") or ""),
                "contentUrl": str(parsed.get("contentUrl") or ""),
                "finderFeed": parsed.get("finderFeed") or {},
                "rawXml": raw_xml,
            }
        )
    names = get_display_names(handle, [str(r.get("username") or "") for r in parsed_rows])
    for row in parsed_rows:
        username = str(row.get("username") or "")
        row["nickname"] = names.get(username, username)
    return parsed_rows[skipped : skipped + wanted]


def decrypt_sns_image(encrypted_data: bytes, key: str) -> bytes:
    from .sns_media import weflow_decrypt_sns_image_bytes

    return weflow_decrypt_sns_image_bytes(bytes(encrypted_data or b""), str(key or ""))


def account_snapshot(handle: int) -> dict[str, Any]:
    context = _context(handle)
    return {
        "account": context.account,
        "native_wxid": context.native_wxid,
        "db_storage_dir": str(context.db_storage_dir),
        "session_db_path": str(context.session_db_path),
        "connected": not context.closed,
        "connected_at": time.time(),
    }


__all__ = [
    "NativeCoreRealtimeError",
    "account_snapshot",
    "close_account",
    "close_all",
    "close_message_cursor",
    "decrypt_sns_image",
    "exec_query",
    "fetch_message_batch",
    "get_avatar_urls",
    "get_contact",
    "get_contacts_compact",
    "get_display_names",
    "get_group_members",
    "get_group_nicknames",
    "get_message_count",
    "get_messages",
    "get_sessions",
    "get_sns_timeline",
    "is_native_core_handle",
    "open_account",
    "open_message_cursor",
]
