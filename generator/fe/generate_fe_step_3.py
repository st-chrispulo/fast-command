from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import secrets
import signal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse, urlunparse

import socketio
from sqlalchemy import and_, desc

try:
    from logger import logger as _app_logger
    logger = _app_logger.getChild("components.live_preview.watch_and_push_db")
except Exception:
    import logging
    logger = logging.getLogger(__name__)

from auth.db import SessionLocalExternal
from app.core.settings import settings
from models.socket.tbl_socket_rooms import SocketRoom
from models.socket.tbl_socket_servers import SocketServer


WORKDIR = os.getenv("WORKDIR", "FE_Workbench")

SOCKET_JOIN_EVENT = os.getenv("SOLITUD_PREVIEW_SOCKET_JOIN_EVENT", "join")
SOCKET_UPDATE_EVENT = os.getenv("SOLITUD_PREVIEW_SOCKET_UPDATE_EVENT", "file_changed")

POLL_SECONDS = float(os.getenv("SOLITUD_PREVIEW_POLL_SECONDS", "0.75"))
MAX_FILE_BYTES = int(os.getenv("SOLITUD_PREVIEW_WATCH_MAX_FILE_BYTES", str(2 * 1024 * 1024)))

SOCKET_PUBLIC_BASE_URL = (os.getenv("SOLITUD_SOCKET_SERVER_INTERNAL_HOST", "http://ws:8001") or "").strip()
PREVIEW_ORIGIN = (os.getenv("SOLITUD_PREVIEW_ORIGIN", "https://app-local.solitud.dev") or "").strip()

DEFAULT_REQUESTED_BY = (os.getenv("REQUESTED_BY") or "").strip() or None
DEFAULT_PREFERRED_REGION = (os.getenv("PREFERRED_REGION") or "").strip() or None


@dataclass(slots=True)
class PreviewSocketConn:
    base_url: str
    namespace: str
    room_key: str
    join_token: str


@dataclass(slots=True)
class FunnelWatchState:
    funnel_id: str
    out_dir: Path
    state_path: Path
    mtimes: Dict[str, int]
    conn: PreviewSocketConn
    sio: socketio.AsyncClient


def _strip_port_if_cloudflare(url: str) -> str:
    s = (url or "").strip().rstrip("/")
    if not s:
        return s
    try:
        u = urlparse(s)
        host = (u.hostname or "").lower()
        port = u.port
        if host and host not in ("127.0.0.1", "localhost") and port in (8001, 80, 443):
            netloc = host
            if u.username:
                if u.password:
                    netloc = f"{u.username}:{u.password}@{host}"
                else:
                    netloc = f"{u.username}@{host}"
            return urlunparse((u.scheme, netloc, u.path, u.params, u.query, u.fragment)).rstrip("/")
    except Exception:
        return s
    return s


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _resolve_out_dir(workdir: Path, funnel_id: str) -> Path:
    funnel_dir = workdir / str(funnel_id)
    meta_path = funnel_dir / "in" / "metadata.json"
    meta = _load_json(meta_path) if meta_path.exists() else None

    if isinstance(meta, dict) and meta.get("out_dir"):
        return Path(str(meta["out_dir"]))

    if (funnel_dir / "output").exists():
        return funnel_dir / "output"

    return funnel_dir


def _read_text(path: Path) -> str:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_FILE_BYTES:
            return ""
        try:
            return raw.decode("utf-8")
        except Exception:
            return raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _virtual_path(rel_posix: str) -> str:
    s = rel_posix.lstrip("/")

    if s.startswith("components/layouts/"):
        return "layouts/" + s[len("components/layouts/") :]
    if s.startswith("components/contents/"):
        return "contents/" + s[len("components/contents/") :]
    if s.startswith("components/authentications/"):
        return "authentications/" + s[len("components/authentications/") :]
    if s.startswith("components/pages/"):
        return "pages/" + s[len("components/pages/") :]
    if s.startswith("components/navigations/"):
        return "navigations/" + s[len("components/navigations/") :]

    return s


def _list_files(root: Path, state_path: Path) -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        state_resolved = state_path.resolve()
    except Exception:
        state_resolved = None

    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.name.lower() == "metadata.json":
            continue
        try:
            if state_resolved and p.resolve() == state_resolved:
                continue
        except Exception:
            pass
        try:
            st = p.stat()
            rel = p.relative_to(root).as_posix()
            out[rel] = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
        except Exception:
            continue
    return out


def _room_key() -> str:
    return f"preview_{secrets.token_urlsafe(18)}"


def _join_token() -> str:
    return secrets.token_urlsafe(32)


def _join_token_hash(token: str) -> str:
    secret = (getattr(settings, "socket_room_join_token_secret", "") or "").strip()
    if not secret:
        raise RuntimeError("socket_room_join_token_secret not configured")
    mac = hmac.new(secret.encode("utf-8"), token.encode("utf-8"), digestmod="sha256")
    return mac.hexdigest()


def _server_is_stale(server: SocketServer) -> bool:
    last_seen = getattr(server, "last_seen_at", None)
    if not last_seen:
        return True
    stale_seconds = int(getattr(settings, "socket_server_stale_seconds", 60) or 60)
    return last_seen < (_utcnow() - timedelta(seconds=stale_seconds))


def _server_has_capacity(server: SocketServer) -> bool:
    max_rooms = int(getattr(server, "max_rooms", 0) or 0)
    if max_rooms <= 0:
        return True
    rooms_used = int(getattr(server, "rooms_used", 0) or 0)
    return rooms_used < max_rooms


def _server_score(server: SocketServer) -> int:
    return int(getattr(server, "rooms_used", 0) or 0)


def _pick_server(db, preferred_region: Optional[str]) -> SocketServer:
    rows = db.query(SocketServer).filter(SocketServer.is_active.is_(True)).all()
    rows = [s for s in rows if not _server_is_stale(s) and _server_has_capacity(s)]
    if not rows:
        raise RuntimeError("No available socket servers")

    if preferred_region:
        region_rows = [s for s in rows if (getattr(s, "region", None) or "").strip() == preferred_region]
        if region_rows:
            return sorted(region_rows, key=_server_score)[0]

    return sorted(rows, key=_server_score)[0]


def _socket_base_url(server: SocketServer) -> str:
    if SOCKET_PUBLIC_BASE_URL:
        return _strip_port_if_cloudflare(SOCKET_PUBLIC_BASE_URL).rstrip("/")

    host = (getattr(server, "host", None) or "").strip()
    if not host:
        raise RuntimeError("Socket server host is missing")

    if host.startswith("http://") or host.startswith("https://"):
        base = host.rstrip("/")
    else:
        base = f"http://{host}".rstrip("/")

    return _strip_port_if_cloudflare(base).rstrip("/")


def _extract_scope_value(room: SocketRoom, key: str) -> Optional[str]:
    scope = getattr(room, "scope_json", None) or {}
    v = scope.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _find_existing_preview_room(db, funnel_id: str, requested_by: Optional[str]) -> Optional[SocketRoom]:
    q = (
        db.query(SocketRoom)
        .filter(
            and_(
                SocketRoom.is_active.is_(True),
                SocketRoom.room_type == "preview",
            )
        )
        .order_by(desc(getattr(SocketRoom, "created_at", SocketRoom.id)))
    )

    rooms = q.all()
    for r in rooms:
        if _extract_scope_value(r, "funnel_id") != funnel_id:
            continue
        if requested_by and _extract_scope_value(r, "requested_by") != str(requested_by):
            continue
        return r
    return None


def _get_server_by_id(db, server_id: Any) -> Optional[SocketServer]:
    if not server_id:
        return None
    return db.query(SocketServer).filter(SocketServer.id == server_id).first()


def _get_conn_from_db(
    *,
    db,
    funnel_id: str,
    requested_by: Optional[str],
    preferred_region: Optional[str],
) -> PreviewSocketConn:
    existing = _find_existing_preview_room(db, funnel_id, requested_by)
    if existing:
        server = _get_server_by_id(db, getattr(existing, "server_id", None))
        if server and getattr(server, "is_active", False) and (not _server_is_stale(server)):
            jt = _join_token()
            existing.join_token_hash = _join_token_hash(jt)
            if not getattr(existing, "room_key", None):
                existing.room_key = _room_key()
            db.commit()
            db.refresh(existing)

            return PreviewSocketConn(
                base_url=_socket_base_url(server),
                namespace="/",
                room_key=str(getattr(existing, "room_key") or "").strip(),
                join_token=jt,
            )

        existing.is_active = False
        db.commit()

    server = _pick_server(db, preferred_region)

    rk = _room_key()
    jt = _join_token()

    room = SocketRoom(
        server_id=server.id,
        room_key=rk,
        room_type="preview",
        join_token_hash=_join_token_hash(jt),
        scope_json={
            "purpose": "comp_connections_preview",
            "funnel_id": str(funnel_id),
            "requested_by": str(requested_by) if requested_by else None,
        },
        is_active=True,
    )

    db.add(room)

    if hasattr(server, "rooms_used"):
        server.rooms_used = int(getattr(server, "rooms_used", 0) or 0) + 1

    db.commit()
    db.refresh(room)

    return PreviewSocketConn(
        base_url=_socket_base_url(server),
        namespace="/",
        room_key=rk,
        join_token=jt,
    )


def _discover_funnel_ids(workdir: Path) -> list[str]:
    if not workdir.exists() or not workdir.is_dir():
        return []
    out: list[str] = []
    for p in workdir.iterdir():
        if not p.is_dir():
            continue
        name = (p.name or "").strip()
        if not name or name.startswith("."):
            continue
        out.append(name)
    return sorted(set(out))


async def _connect_socket(*, funnel_id: str, conn: PreviewSocketConn) -> socketio.AsyncClient:
    sio = socketio.AsyncClient(
        reconnection=True,
        reconnection_attempts=0,
        reconnection_delay=1,
        logger=False,
        engineio_logger=False,
    )

    @sio.event(namespace=conn.namespace)
    async def connect_error(data):
        logger.error("socket_connect_error", extra={"funnel_id": funnel_id, "data": data})

    @sio.event(namespace=conn.namespace)
    async def disconnect():
        logger.warning("socket_disconnected", extra={"funnel_id": funnel_id})

    headers = {
        "Origin": PREVIEW_ORIGIN,
        "User-Agent": "Mozilla/5.0",
    }

    await sio.connect(
        conn.base_url,
        transports=["polling"],
        socketio_path="socket.io",
        auth={"room_key": conn.room_key, "join_token": conn.join_token},
        headers=headers,
        wait_timeout=30,
        namespaces=[conn.namespace],
    )

    await sio.emit(
        SOCKET_JOIN_EVENT,
        {"room_key": conn.room_key, "join_token": conn.join_token},
        namespace=conn.namespace,
    )

    logger.info(
        "socket_connected",
        extra={"funnel_id": funnel_id, "room_key": conn.room_key, "base_url": conn.base_url},
    )
    return sio


def _load_mtimes(state_path: Path) -> Dict[str, int]:
    state = _load_json(state_path) or {}
    mtimes = state.get("mtimes")
    if not isinstance(mtimes, dict):
        return {}
    out: Dict[str, int] = {}
    for k, v in mtimes.items():
        ks = str(k).strip()
        if not ks:
            continue
        try:
            out[ks] = int(v)
        except Exception:
            continue
    return out


async def _build_funnel_state(
    *,
    workdir: Path,
    funnel_id: str,
    requested_by: Optional[str],
    preferred_region: Optional[str],
) -> FunnelWatchState:
    out_dir = _resolve_out_dir(workdir, funnel_id)
    state_path = workdir / str(funnel_id) / ".watch_state.json"

    db = SessionLocalExternal()
    try:
        conn = _get_conn_from_db(
            db=db,
            funnel_id=str(funnel_id),
            requested_by=str(requested_by) if requested_by else None,
            preferred_region=preferred_region,
        )
    finally:
        db.close()

    sio = await _connect_socket(funnel_id=funnel_id, conn=conn)
    mtimes = _load_mtimes(state_path)

    logger.info(
        "preview_watch_ready",
        extra={
            "funnel_id": funnel_id,
            "out_dir": str(out_dir),
            "room_key": conn.room_key,
        },
    )

    return FunnelWatchState(
        funnel_id=funnel_id,
        out_dir=out_dir,
        state_path=state_path,
        mtimes=mtimes,
        conn=conn,
        sio=sio,
    )


async def _refresh_funnel_socket(
    *,
    st: FunnelWatchState,
    requested_by: Optional[str],
    preferred_region: Optional[str],
) -> FunnelWatchState:
    try:
        await st.sio.disconnect()
    except Exception:
        pass

    db = SessionLocalExternal()
    try:
        conn = _get_conn_from_db(
            db=db,
            funnel_id=str(st.funnel_id),
            requested_by=str(requested_by) if requested_by else None,
            preferred_region=preferred_region,
        )
    finally:
        db.close()

    sio = await _connect_socket(funnel_id=st.funnel_id, conn=conn)

    return FunnelWatchState(
        funnel_id=st.funnel_id,
        out_dir=st.out_dir,
        state_path=st.state_path,
        mtimes=st.mtimes,
        conn=conn,
        sio=sio,
    )


async def _push_changes_one_funnel(st: FunnelWatchState) -> bool:
    if not st.out_dir.exists() or not st.out_dir.is_dir():
        return False

    current = _list_files(st.out_dir, st.state_path)

    changed: list[str] = []
    for rel, mtime_ns in current.items():
        prev = st.mtimes.get(rel)
        if prev is None or int(prev) != int(mtime_ns):
            changed.append(rel)

    if not changed:
        return False

    pushed_any = False
    for rel in sorted(changed):
        p = st.out_dir / rel
        code = _read_text(p)
        st.mtimes[rel] = current.get(rel, st.mtimes.get(rel, 0))

        if not code:
            continue

        await st.sio.emit(
            SOCKET_UPDATE_EVENT,
            {"path": _virtual_path(rel), "code": code},
            namespace=st.conn.namespace,
        )
        pushed_any = True

    _save_json(st.state_path, {"mtimes": st.mtimes})
    if pushed_any:
        logger.info("pushed_changes", extra={"funnel_id": st.funnel_id, "count": len(changed)})
    return pushed_any


class WatchAllFunnels:
    def __init__(
        self,
        *,
        workdir: Path,
        requested_by: Optional[str],
        preferred_region: Optional[str],
        poll_seconds: float,
    ) -> None:
        self._workdir = workdir
        self._requested_by = requested_by
        self._preferred_region = preferred_region
        self._poll_seconds = max(0.1, float(poll_seconds))
        self._running = True
        self._states: Dict[str, FunnelWatchState] = {}

    def stop(self, *_args: Any) -> None:
        self._running = False

    async def _ensure_funnel(self, funnel_id: str) -> None:
        if funnel_id in self._states:
            return
        try:
            st = await _build_funnel_state(
                workdir=self._workdir,
                funnel_id=funnel_id,
                requested_by=self._requested_by,
                preferred_region=self._preferred_region,
            )
            self._states[funnel_id] = st
        except Exception:
            logger.exception("ensure_funnel_failed", extra={"funnel_id": funnel_id})

    async def _cleanup_removed(self, active: set[str]) -> None:
        removed = [fid for fid in self._states.keys() if fid not in active]
        for fid in removed:
            st = self._states.pop(fid, None)
            if not st:
                continue
            try:
                await st.sio.disconnect()
            except Exception:
                pass
            logger.info("funnel_removed", extra={"funnel_id": fid})

    async def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        logger.info(
            "watch_all_start",
            extra={
                "workdir": str(self._workdir),
                "requested_by": self._requested_by,
                "preferred_region": self._preferred_region,
                "poll_seconds": self._poll_seconds,
            },
        )

        try:
            while self._running:
                funnel_ids = _discover_funnel_ids(self._workdir)
                active = set(funnel_ids)

                for fid in funnel_ids:
                    await self._ensure_funnel(fid)

                await self._cleanup_removed(active)

                for fid in funnel_ids:
                    st = self._states.get(fid)
                    if not st:
                        continue

                    try:
                        if not st.sio.connected:
                            self._states[fid] = await _refresh_funnel_socket(
                                st=st,
                                requested_by=self._requested_by,
                                preferred_region=self._preferred_region,
                            )
                            st = self._states[fid]

                        await _push_changes_one_funnel(st)
                    except Exception:
                        logger.exception("funnel_poll_failed", extra={"funnel_id": fid})
                        try:
                            self._states[fid] = await _refresh_funnel_socket(
                                st=st,
                                requested_by=self._requested_by,
                                preferred_region=self._preferred_region,
                            )
                        except Exception:
                            logger.exception("funnel_reconnect_failed", extra={"funnel_id": fid})

                await asyncio.sleep(self._poll_seconds)
        finally:
            for fid, st in list(self._states.items()):
                try:
                    await st.sio.disconnect()
                except Exception:
                    pass
                logger.info("watch_all_stopped_funnel", extra={"funnel_id": fid})
            self._states.clear()
            logger.info("watch_all_stopped")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=False, default=WORKDIR)
    ap.add_argument("--requested-by", required=False, default=DEFAULT_REQUESTED_BY or "")
    ap.add_argument("--preferred-region", required=False, default=DEFAULT_PREFERRED_REGION or "")
    ap.add_argument("--poll-seconds", required=False, type=float, default=POLL_SECONDS)
    args = ap.parse_args()

    requested_by = (args.requested_by or "").strip() or None
    preferred_region = (args.preferred_region or "").strip() or None

    runner = WatchAllFunnels(
        workdir=Path(args.workdir),
        requested_by=requested_by,
        preferred_region=preferred_region,
        poll_seconds=float(args.poll_seconds),
    )
    asyncio.run(runner.run())


if __name__ == "__main__":
    main()