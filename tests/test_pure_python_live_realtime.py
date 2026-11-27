"""End-to-end tests for pure-Python live SQLCipher/WAL incremental message updates."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import struct
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import zstandard as zstd
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechat_decrypt_tool import (
    account_source_policy,
    chat_realtime_autosync,
    native_core_realtime,
    wcdb_realtime,
)
from wechat_decrypt_tool.routers import chat as chat_router
from wechat_decrypt_tool.routers import chat_realtime_sse
from wechat_decrypt_tool.sqlite_wal import _checksum as _wal_checksum
from wechat_decrypt_tool.wechat_decrypt import (
    PAGE_SIZE,
    RESERVE_SIZE,
    SALT_SIZE,
    WeChatDatabaseDecryptor,
    _derive_mac_key,
    _derive_sqlcipher_enc_key,
)


def _init_reserved_sqlite_db(path: Path) -> sqlite3.Connection:
    """Create a 4096-byte SQLite DB with 80-byte reserve space matching WeChat 4.x WCDB."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.execute("PRAGMA page_size = 4096")
    c.execute("PRAGMA journal_mode = DELETE")
    c.execute("CREATE TABLE _boot (x INT)")
    c.execute("DROP TABLE _boot")
    c.commit()
    c.close()

    raw = bytearray(path.read_bytes())
    raw[20] = RESERVE_SIZE
    raw[24:28] = (2).to_bytes(4, "big")
    raw[105:107] = (PAGE_SIZE - RESERVE_SIZE).to_bytes(2, "big")
    path.write_bytes(raw)

    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _encrypt_sqlcipher_page(
    enc_key: bytes,
    mac_key: bytes,
    plain_page: bytes,
    page_num: int,
    salt: bytes,
) -> bytes:
    iv = hashlib.sha256(f"iv:{page_num}:{plain_page[:64].hex()}".encode("ascii")).digest()[:16]
    if page_num == 1:
        payload = plain_page[SALT_SIZE : PAGE_SIZE - RESERVE_SIZE]
        prefix = salt
    else:
        payload = plain_page[: PAGE_SIZE - RESERVE_SIZE]
        prefix = b""

    encryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv), backend=default_backend()).encryptor()
    ct = encryptor.update(payload) + encryptor.finalize()
    body = prefix + ct + iv
    mac = hmac.new(mac_key, digestmod=hashlib.sha512)
    mac.update(body[SALT_SIZE if page_num == 1 else 0 :])
    mac.update(page_num.to_bytes(4, "little"))
    return body + mac.digest()


def _export_plain_db_and_wal_to_sqlcipher(
    plain_db_path: Path,
    enc_db_path: Path,
    *,
    passphrase_key: bytes,
    salt: bytes,
) -> None:
    """Encrypt both a plain SQLite main file and its live -wal file into SQLCipher 4.0 format."""
    enc_key = _derive_sqlcipher_enc_key(passphrase_key, salt)
    mac_key = _derive_mac_key(enc_key, salt)

    enc_db_path.parent.mkdir(parents=True, exist_ok=True)
    raw_db = plain_db_path.read_bytes()
    total_pages = len(raw_db) // PAGE_SIZE
    out_db = bytearray()
    for idx in range(total_pages):
        pgno = idx + 1
        plain_page = raw_db[idx * PAGE_SIZE : (idx + 1) * PAGE_SIZE]
        out_db.extend(_encrypt_sqlcipher_page(enc_key, mac_key, plain_page, pgno, salt))
    enc_db_path.write_bytes(out_db)

    plain_wal_path = Path(str(plain_db_path) + "-wal")
    enc_wal_path = Path(str(enc_db_path) + "-wal")
    if not plain_wal_path.exists() or plain_wal_path.stat().st_size < 32:
        enc_wal_path.unlink(missing_ok=True)
        return

    raw_wal = plain_wal_path.read_bytes()
    magic, version, wal_ps, ckpt_seq = struct.unpack(">IIII", raw_wal[:16])
    wal_salt = raw_wal[16:24]
    endian = "<" if magic == 0x377F0682 else ">"

    out_wal = bytearray(32)
    out_wal[:16] = struct.pack(">IIII", magic, version, wal_ps, ckpt_seq)
    out_wal[16:24] = wal_salt
    state = _wal_checksum(bytes(out_wal[:24]), endian)
    out_wal[24:32] = struct.pack(">II", *state)

    frame_size = 24 + PAGE_SIZE
    for offset in range(32, len(raw_wal) - frame_size + 1, frame_size):
        hdr = raw_wal[offset : offset + 24]
        if hdr[8:16] != wal_salt:
            break
        pgno, db_size = struct.unpack(">II", hdr[:8])
        plain_page = raw_wal[offset + 24 : offset + frame_size]
        enc_page = _encrypt_sqlcipher_page(enc_key, mac_key, plain_page, pgno, salt)
        frame_hdr_prefix = struct.pack(">II", pgno, db_size) + wal_salt
        state = _wal_checksum(enc_page, endian, _wal_checksum(frame_hdr_prefix[:8], endian, state))
        out_wal.extend(frame_hdr_prefix)
        out_wal.extend(struct.pack(">II", *state))
        out_wal.extend(enc_page)

    enc_wal_path.write_bytes(out_wal)


def test_encrypted_sqlcipher_wal_live_messages_and_sse_and_autosync(tmp_path: Path, monkeypatch):
    """Verify live encrypted WAL updates appear immediately via status, sessions, messages, SSE, and autosync."""
    account = "wxid_live_owner"
    friend = "wxid_alice"
    new_friend = "wxid_bob_new"
    key_bytes = bytes.fromhex("9f5dd0d3b6d0477ea5045c9e380ee272e53927993eb548dd98a022e842d5f7bd")
    key_hex = key_bytes.hex()

    session_salt = bytes.fromhex("11f4090ef6897e146f94109f13743e34")
    contact_salt = bytes.fromhex("22f4090ef6897e146f94109f13743e34")
    message_salt = bytes.fromhex("33f4090ef6897e146f94109f13743e34")

    from wechat_decrypt_tool import key_store

    data_dir = tmp_path / "app_data"
    monkeypatch.setenv("WECHAT_TOOL_DATA_DIR", str(data_dir))
    monkeypatch.setattr(key_store, "_KEY_STORE_PATH", data_dir / "output" / "account_keys.json")

    plain_stage = tmp_path / "plain_stage"
    db_storage = tmp_path / account / "db_storage"
    output_databases = data_dir / "output" / "databases"
    account_dir = output_databases / account
    account_dir.mkdir(parents=True)
    key_store.upsert_account_keys_in_store(
        account,
        db_key=key_hex,
        db_key_source_db_storage_path=str(db_storage),
        db_key_source_wxid_dir=str(db_storage.parent),
    )

    # 1. Build live plain staging DBs in WAL mode, then encrypt DB + WAL into db_storage.
    sess_conn = _init_reserved_sqlite_db(plain_stage / "session.db")
    sess_conn.execute(
        """
        CREATE TABLE SessionTable (
            username TEXT PRIMARY KEY,
            type INTEGER DEFAULT 0,
            unread_count INTEGER DEFAULT 0,
            summary TEXT DEFAULT '',
            draft TEXT DEFAULT '',
            last_msg_type INTEGER DEFAULT 1,
            last_msg_sub_type INTEGER DEFAULT 0,
            last_msg_sender TEXT DEFAULT '',
            last_sender_display_name TEXT DEFAULT '',
            last_msg_locald_id INTEGER DEFAULT 0,
            last_timestamp INTEGER DEFAULT 0,
            sort_timestamp INTEGER DEFAULT 0,
            is_hidden INTEGER DEFAULT 0
        )
        """
    )
    sess_conn.execute(
        """
        INSERT INTO SessionTable(username, unread_count, summary, last_msg_type, last_msg_locald_id, last_timestamp, sort_timestamp)
        VALUES (?, 0, '旧消息', 1, 1, 1700000100, 1700000100)
        """,
        (friend,),
    )
    sess_conn.commit()
    sess_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    cont_conn = _init_reserved_sqlite_db(plain_stage / "contact.db")
    cont_conn.execute(
        "CREATE TABLE contact (username TEXT PRIMARY KEY, alias TEXT, remark TEXT, nick_name TEXT, local_type INTEGER DEFAULT 1, verify_flag INTEGER DEFAULT 0, big_head_url TEXT, small_head_url TEXT)"
    )
    cont_conn.execute(
        "INSERT INTO contact(username, alias, remark, nick_name) VALUES (?, 'alice_id', 'Alice备注', 'Alice')",
        (friend,),
    )
    cont_conn.execute(
        "INSERT INTO contact(username, alias, remark, nick_name) VALUES (?, 'bob_id', '', 'Bob新好友')",
        (new_friend,),
    )
    cont_conn.commit()
    cont_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    friend_table = "Msg_" + hashlib.md5(friend.encode("utf-8")).hexdigest()
    msg_conn = _init_reserved_sqlite_db(plain_stage / "message_0.db")
    msg_conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
    msg_conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, ?)", (account,))
    msg_conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (2, ?)", (friend,))
    msg_conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (3, ?)", (new_friend,))
    msg_conn.execute(
        f"""
        CREATE TABLE {friend_table} (
            local_id INTEGER PRIMARY KEY,
            server_id INTEGER,
            local_type INTEGER,
            sort_seq INTEGER,
            real_sender_id INTEGER,
            create_time INTEGER,
            message_content TEXT,
            compress_content BLOB,
            packed_info_data BLOB,
            source TEXT
        )
        """
    )
    msg_conn.execute(
        f"INSERT INTO {friend_table}(local_id, server_id, local_type, sort_seq, real_sender_id, create_time, message_content) VALUES (1, 1001, 1, 100, 2, 1700000100, '旧消息')"
    )
    msg_conn.commit()
    msg_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    enc_session_db = db_storage / "session" / "session.db"
    enc_contact_db = db_storage / "contact" / "contact.db"
    enc_message_db = db_storage / "message" / "message_0.db"

    _export_plain_db_and_wal_to_sqlcipher(
        plain_stage / "session.db", enc_session_db, passphrase_key=key_bytes, salt=session_salt
    )
    _export_plain_db_and_wal_to_sqlcipher(
        plain_stage / "contact.db", enc_contact_db, passphrase_key=key_bytes, salt=contact_salt
    )
    _export_plain_db_and_wal_to_sqlcipher(
        plain_stage / "message_0.db", enc_message_db, passphrase_key=key_bytes, salt=message_salt
    )

    # 2. Materialize initial static decrypted snapshot in account_dir (just like initial manual decrypt).
    decryptor = WeChatDatabaseDecryptor(key_hex)
    assert decryptor.decrypt_database(str(enc_session_db), str(account_dir / "session.db"))
    assert decryptor.decrypt_database(str(enc_contact_db), str(account_dir / "contact.db"))
    assert decryptor.decrypt_database(str(enc_message_db), str(account_dir / "message_0.db"))
    (account_dir / "_source.json").write_text(
        json.dumps({"db_storage_path": str(db_storage), "wxid_dir": str(db_storage.parent)}),
        encoding="utf-8",
    )

    # Local decrypted account should NOT force static snapshot mode.
    assert account_source_policy.account_prefers_decrypted_snapshot(account_dir) is False

    fake_req = SimpleNamespace(
        base_url="http://testserver/",
        headers={},
        state=SimpleNamespace(),
    )

    if True:
        # 3. Verify /api/chat/realtime/status reports available=True with pure-Python mode.
        status_resp = asyncio.run(chat_router.get_chat_realtime_status(account))
        assert status_resp["available"] is True
        assert status_resp["realtime"]["native_core_mode"] == "python"
        assert status_resp["realtime"]["connected"] is True

        # 4. Open SSE stream BEFORE new messages arrive, verify initial 'ready' event.
        disconnected = False

        async def _drive_sse() -> list[dict]:
            nonlocal disconnected

            class _SSETestReq:
                base_url = "http://testserver/"
                headers: dict[str, str] = {}
                state = SimpleNamespace()

                async def is_disconnected(self) -> bool:
                    return disconnected

            sse_resp = await chat_realtime_sse.stream_chat_realtime_messages(
                _SSETestReq(),  # type: ignore[arg-type]
                account=account,
                interval_ms=10,
                include_sent=True,
                scope="chat",
            )
            events: list[dict] = []
            agen = sse_resp.body_iterator

            first_chunk = await asyncio.wait_for(agen.__anext__(), timeout=3.0)
            first_text = first_chunk.decode("utf-8") if isinstance(first_chunk, bytes) else str(first_chunk)
            for line in first_text.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
            assert events and events[0]["type"] == "ready"

            # 5. Simulate WeChat receiving a new zstd-compressed message + sending a reply + new friend session ONLY in -wal!
            main_msg_mtime_before = enc_message_db.stat().st_mtime_ns
            now_ts = int(time.time())
            zstd_text = zstd.ZstdCompressor().compress("新收到的实时消息（WAL增量）".encode("utf-8"))
            msg_conn.execute(
                f"INSERT INTO {friend_table}(local_id, server_id, local_type, sort_seq, real_sender_id, create_time, message_content, compress_content) VALUES (?, ?, 1, 200, 2, ?, ?, ?)",
                (2, 1002, now_ts, zstd_text, zstd_text),
            )
            msg_conn.execute(
                f"INSERT INTO {friend_table}(local_id, server_id, local_type, sort_seq, real_sender_id, create_time, message_content) VALUES (?, ?, 1, 201, 1, ?, '我刚刚回复的消息')",
                (3, 1003, now_ts + 1),
            )
            bob_table = "Msg_" + hashlib.md5(new_friend.encode("utf-8")).hexdigest()
            msg_conn.execute(
                f"""
                CREATE TABLE {bob_table} (
                    local_id INTEGER PRIMARY KEY,
                    server_id INTEGER,
                    local_type INTEGER,
                    sort_seq INTEGER,
                    real_sender_id INTEGER,
                    create_time INTEGER,
                    message_content TEXT,
                    compress_content BLOB,
                    packed_info_data BLOB,
                    source TEXT
                )
                """
            )
            msg_conn.execute(
                f"INSERT INTO {bob_table}(local_id, server_id, local_type, sort_seq, real_sender_id, create_time, message_content) VALUES (1, 2001, 1, 300, 3, ?, '来自新好友Bob的第一条消息')",
                (now_ts + 2,),
            )
            msg_conn.commit()

            sess_conn.execute(
                "UPDATE SessionTable SET summary = '我刚刚回复的消息', last_msg_locald_id = 3, last_timestamp = ?, sort_timestamp = ?, unread_count = 1 WHERE username = ?",
                (now_ts + 1, now_ts + 1, friend),
            )
            sess_conn.execute(
                "INSERT INTO SessionTable(username, unread_count, summary, last_msg_type, last_msg_locald_id, last_timestamp, sort_timestamp) VALUES (?, 1, '来自新好友Bob的第一条消息', 1, 1, ?, ?)",
                (new_friend, now_ts + 2, now_ts + 2),
            )
            sess_conn.commit()

            # Export ONLY the updated -wal files (keep main enc_message_db untouched to prove WAL-only incremental decrypt!).
            saved_main_msg = enc_message_db.read_bytes()
            _export_plain_db_and_wal_to_sqlcipher(
                plain_stage / "message_0.db", enc_message_db, passphrase_key=key_bytes, salt=message_salt
            )
            assert enc_message_db.read_bytes() == saved_main_msg
            os.utime(enc_message_db, ns=(main_msg_mtime_before, main_msg_mtime_before))

            saved_main_sess = enc_session_db.read_bytes()
            main_sess_mtime_before = enc_session_db.stat().st_mtime_ns
            _export_plain_db_and_wal_to_sqlcipher(
                plain_stage / "session.db", enc_session_db, passphrase_key=key_bytes, salt=session_salt
            )
            assert enc_session_db.read_bytes() == saved_main_sess
            os.utime(enc_session_db, ns=(main_sess_mtime_before, main_sess_mtime_before))

            # Read the 3 live message events from SSE!
            while len([e for e in events if e.get("type") == "message"]) < 3:
                chunk = await asyncio.wait_for(agen.__anext__(), timeout=3.0)
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
                for line in text.splitlines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))

            disconnected = True
            await agen.aclose()
            return events

        sse_events = asyncio.run(_drive_sse())
        msg_events = [e for e in sse_events if e.get("type") == "message"]
        assert len(msg_events) == 3

        alice_events = [e for e in msg_events if e["username"] == friend]
        bob_events = [e for e in msg_events if e["username"] == new_friend]
        assert [e["message"]["content"] for e in alice_events] == [
            "新收到的实时消息（WAL增量）",
            "我刚刚回复的消息",
        ]
        assert [e["received"] for e in alice_events] == [True, False]
        assert [e["message"]["content"] for e in bob_events] == ["来自新好友Bob的第一条消息"]

        # 6. Verify list_chat_messages(source="auto") and list_chat_sessions(source="auto") see all live messages!
        msgs_resp = chat_router.list_chat_messages(
            fake_req,  # type: ignore[arg-type]
            username=friend,
            account=account,
            limit=50,
            offset=0,
            order="asc",
            source="auto",
        )
        assert msgs_resp["source"] == "realtime"
        assert [m["content"] for m in msgs_resp["messages"]] == [
            "旧消息",
            "新收到的实时消息（WAL增量）",
            "我刚刚回复的消息",
        ]

        sessions_resp = chat_router.list_chat_sessions(
            fake_req,  # type: ignore[arg-type]
            account=account,
            limit=50,
            include_hidden=False,
            include_official=False,
            source="auto",
        )
        assert sessions_resp["source"] == "realtime"
        session_usernames = [s["username"] for s in sessions_resp["sessions"]]
        assert session_usernames[:2] == [new_friend, friend]

        # 7. Verify background ChatRealtimeAutoSyncService incrementally persists live messages into decrypted SQLite!
        autosync = chat_realtime_autosync.ChatRealtimeAutoSyncService()
        sync_res = autosync._sync_account(account)
        assert sync_res.get("status") == "success"
        assert int(sync_res.get("inserted_total") or sync_res.get("insertedTotal") or 0) >= 3

        decrypted_msgs = chat_router.list_chat_messages(
            fake_req,  # type: ignore[arg-type]
            username=new_friend,
            account=account,
            limit=50,
            offset=0,
            order="asc",
            source="decrypted",
        )
        assert decrypted_msgs["source"] == "decrypted"
        assert [m["content"] for m in decrypted_msgs["messages"]] == ["来自新好友Bob的第一条消息"]

    sess_conn.close()
    cont_conn.close()
    msg_conn.close()
    wcdb_realtime.shutdown()
    native_core_realtime.close_all()


def test_imported_snapshot_still_prefers_decrypted(tmp_path: Path):
    account_dir = tmp_path / "wxid_imported"
    account_dir.mkdir(parents=True)
    (account_dir / "session.db").write_bytes(b"sqlite")
    (account_dir / "contact.db").write_bytes(b"sqlite")
    (account_dir / "_source.json").write_text(
        json.dumps({"import_mode": "manual_import"}),
        encoding="utf-8",
    )
    assert account_source_policy.account_prefers_decrypted_snapshot(account_dir) is True
