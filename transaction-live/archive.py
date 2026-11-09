"""Durable observations and reconstructed stages; standard-library-only storage.

Document bytes are immutable objects. SQLite publishes their references only after
the objects have been flushed and atomically renamed into the content store.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
import uuid
import zipfile


class ArchiveError(RuntimeError):
    pass


class QuotaExceeded(ArchiveError):
    pass


_DB = "archive.sqlite3"
_CHUNK = 1024 * 1024
_METADATA_RESERVE = 64 * 1024
_MAX_METADATA = 4 * 1024 * 1024
_MAX_ASSET = 64 * 1024 * 1024
_MAX_DIFF = 128
_SECRET_KEYS = re.compile(r"(^|_)(token|password|secret|credential|authorization|cookie)s?($|_)", re.I)
_SECRET_TEXT = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+|((?:token|password|secret|authorization)=)[^\s&#]+")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_TASK = re.compile(r"(?:capture|rebuild|export)-[A-Za-z0-9_-]+\Z")


class _Reservation:
    def __init__(self, amount, workdir):
        self.amount = amount
        self.workdir = workdir
        self.persistent = 0
        self.temporary_paths = set()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe(value):
    """Defence in depth for metadata; native asset bytes remain unmodified."""
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if _SECRET_KEYS.search(str(k)) else _safe(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub(lambda m: (m.group(1) or m.group(2)) + "[redacted]", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ArchiveError("metadata must contain JSON values")


def _json(value):
    try:
        result = json.dumps(_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ArchiveError("invalid JSON metadata") from exc
    if len(result.encode("utf-8")) > _MAX_METADATA:
        raise ArchiveError("metadata exceeds the 4 MiB bound")
    return result


def _decode(value, default=None):
    return json.loads(value) if value is not None else default


def _preview(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value if len(text) <= 1024 else {"truncated": True, "preview": text[:1024]}


def document_diff(before, after):
    """Bounded structural changes, using feature/entity IDs rather than array slots."""
    changes = []

    def emit(path, kind, old, new):
        if len(changes) < _MAX_DIFF:
            changes.append({"path": path or "$", "kind": kind, "old": _preview(old), "new": _preview(new)})

    def keyed(items, path):
        if not items or not all(isinstance(item, dict) for item in items):
            return None
        key = "id" if all("id" in item for item in items) else "name" if "param" in path and all("name" in item for item in items) else None
        if key is None:
            return None
        pairs = [(json.dumps(item[key], ensure_ascii=False, sort_keys=True), item) for item in items]
        if len({k for k, _ in pairs}) != len(pairs):
            return None
        return key, dict(pairs), [k for k, _ in pairs]

    def walk(old, new, path, depth=0):
        if len(changes) >= _MAX_DIFF or old == new:
            return
        if depth > 32:
            emit(path, "changed", old, new)
        elif isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(old.keys() | new.keys()):
                child = f"{path}.{key}" if path else str(key)
                if key not in old:
                    emit(child, "added", None, new[key])
                elif key not in new:
                    emit(child, "removed", old[key], None)
                else:
                    walk(old[key], new[key], child, depth + 1)
        elif isinstance(old, list) and isinstance(new, list):
            left, right = keyed(old, path), keyed(new, path)
            if not old and right:
                left = (right[0], {}, [])
            if not new and left:
                right = (left[0], {}, [])
            if left and right and left[0] == right[0]:
                field, a, order_a = left
                _, b, order_b = right
                for ident in sorted(a.keys() | b.keys()):
                    child = f"{path}[{field}={ident}]"
                    if ident not in a:
                        emit(child, "added", None, b[ident])
                    elif ident not in b:
                        emit(child, "removed", a[ident], None)
                    else:
                        walk(a[ident], b[ident], child, depth + 1)
                common_a = [k for k in order_a if k in b]
                common_b = [k for k in order_b if k in a]
                if common_a != common_b:
                    emit(path + ".order", "reordered", [json.loads(k) for k in order_a], [json.loads(k) for k in order_b])
            else:
                emit(path, "changed", old, new)
        else:
            emit(path, "changed", old, new)

    walk(before, after, "")
    if len(changes) == _MAX_DIFF:
        changes[-1] = {"path": "$", "kind": "truncated", "old": None, "new": "Diff limited to 128 entries; inspect immutable document assets for the full change."}
    return _safe(changes)


class Archive:
    def __init__(self, root: Path, limit_bytes=2 * 1024 ** 3, read_only=False):
        self.root = Path(root).resolve()
        self.limit_bytes = int(limit_bytes)
        if self.limit_bytes <= 0:
            raise ArchiveError("limit_bytes must be positive")
        self.read_only = bool(read_only)
        self._lock = threading.RLock()
        self._closed = False
        self._quota_hit = False
        self._reservations = {}
        self._writer_lock = None
        self._recovery = {"events": 0, "jobs": 0}
        if read_only:
            if not (self.root / _DB).is_file():
                raise ArchiveError("archive database does not exist")
            uri = (self.root / _DB).as_uri() + "?mode=ro"
            self._db = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None, timeout=10)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self._acquire_writer()
            try:
                for name in ("objects", "tmp", ".work"):
                    path = self.root / name
                    if path.exists() and (path.is_symlink() or path.resolve() != path):
                        raise ArchiveError("archive storage directories must not be symbolic links or junctions")
                (self.root / "objects").mkdir(exist_ok=True)
                (self.root / "tmp").mkdir(exist_ok=True)
                self._db = sqlite3.connect(self.root / _DB, check_same_thread=False, isolation_level=None, timeout=10)
            except Exception:
                self._release_writer()
                raise
        try:
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            if not read_only:
                self._db.execute("PRAGMA journal_mode=DELETE")
                self._db.execute("PRAGMA synchronous=FULL")
                self._create_schema()
                self._recover()
        except Exception:
            self._db.close()
            self._release_writer()
            raise

    def _acquire_writer(self):
        path = self.root / ".writer.lock"
        if path.is_symlink() or (path.exists() and path.resolve() != path):
            raise ArchiveError("archive writer lock must not be a link")
        handle = path.open("a+b")
        try:
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ArchiveError("this archive is already open by a writer; use read-only mode or another archive") from exc
        self._writer_lock = handle

    def _release_writer(self):
        handle, self._writer_lock = self._writer_lock, None
        if handle is not None:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _workdir(self, workdir):
        if workdir is None:
            return None
        path = Path(workdir).absolute()
        resolved = path.resolve()
        expected_parent = self.root / ".work"
        if (path != resolved or resolved.parent != expected_parent or not _TASK.fullmatch(resolved.name)
                or expected_parent.is_symlink() or (expected_parent.exists() and expected_parent.resolve() != expected_parent)):
            raise ArchiveError("workdir must be one owned capture/rebuild/export task directory under archive .work")
        return resolved

    @staticmethod
    def _tree_bytes(path):
        """Only called for a validated single task directory; never follow links."""
        try:
            if not path.exists() or path.is_symlink():
                return 0
            if path.is_file():
                return path.stat().st_size
        except FileNotFoundError:
            return 0
        total, examined = 0, 0
        todo = [(path, 0)]
        while todo:
            directory, depth = todo.pop()
            if depth > 8:
                raise ArchiveError("owned temporary directory nesting exceeds the bound")
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        examined += 1
                        if examined > 10000:
                            raise ArchiveError("owned temporary file count exceeds the bound")
                        try:
                            item = Path(entry.path)
                            if entry.is_symlink() or item.resolve() != item:
                                continue
                            if entry.is_file(follow_symlinks=False):
                                total += entry.stat(follow_symlinks=False).st_size
                            elif entry.is_dir(follow_symlinks=False):
                                todo.append((item, depth + 1))
                        except FileNotFoundError:
                            pass  # The owning worker may be finishing its cleanup.
            except FileNotFoundError:
                pass  # A worker can remove its whole task between discovery and scandir.
        return total

    def _remaining_reservation(self, reservation):
        consumed = reservation.persistent
        if reservation.workdir is not None:
            consumed += self._tree_bytes(reservation.workdir)
        for path in reservation.temporary_paths:
            try:
                if path.exists() and not path.is_symlink():
                    consumed += path.stat().st_size
            except FileNotFoundError:
                pass
        return max(0, reservation.amount - consumed)

    @contextlib.contextmanager
    def reserve(self, amount, workdir=None):
        """Reserve shared disk headroom, crediting this worker's actual use once.

        Enter before exports or derived-file writes. Include commit and cleanup in
        the context. Nested reservations in one calling thread are refused.
        """
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ArchiveError("reservation size must be a positive integer")
        owner = threading.get_ident()
        with self._lock:
            self._open(True)
            if owner in self._reservations:
                raise ArchiveError("nested archive reservations are not supported")
            directory = self._workdir(workdir)
            if directory is not None and any(r.workdir == directory for r in self._reservations.values()):
                raise ArchiveError("temporary task already has a reservation")
            self._check_quota(amount)
            reservation = _Reservation(amount, directory)
            self._reservations[owner] = reservation
        try:
            yield reservation
        finally:
            with self._lock:
                self._reservations.pop(owner, None)

    def _create_schema(self):
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, started_at TEXT NOT NULL, info TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
            document_id TEXT, revision INTEGER, kind TEXT NOT NULL, raw TEXT, status TEXT NOT NULL,
            message TEXT, observed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS assets(sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL CHECK(size>=0));
        CREATE TABLE IF NOT EXISTS snapshots(
            id INTEGER PRIMARY KEY REFERENCES events(id), document_id TEXT, document_hash TEXT, created_at TEXT NOT NULL,
            summary TEXT NOT NULL, assets TEXT NOT NULL, diff TEXT NOT NULL, inference TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS snapshots_document ON snapshots(document_id,id);
        CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id), position INTEGER NOT NULL,
            version TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, fields TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stages(
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id), created_at TEXT NOT NULL,
            summary TEXT NOT NULL, assets TEXT NOT NULL, diff TEXT NOT NULL, inference TEXT NOT NULL);
        PRAGMA user_version=1;
        """)
        # A staging build without the lookup column may already have been opened.
        if "document_hash" not in {row[1] for row in self._db.execute("PRAGMA table_info(snapshots)")}:
            self._db.execute("ALTER TABLE snapshots ADD COLUMN document_hash TEXT")
            for row in self._db.execute("SELECT id,summary FROM snapshots").fetchall():
                self._db.execute("UPDATE snapshots SET document_hash=? WHERE id=?", (_decode(row["summary"]).get("document_hash"), row["id"]))
        self._db.execute("CREATE INDEX IF NOT EXISTS snapshots_hash ON snapshots(document_id,document_hash,id)")

    def _recover(self):
        with self._lock:
            # Only files with this module's staging prefix are ours to clean.
            for path in (self.root / "tmp").glob(".pending-*"):
                if path.is_file():
                    path.unlink(missing_ok=True)
            self._db.execute("BEGIN IMMEDIATE")
            try:
                # A process can exit after publishing an object but before SQLite commit.
                # Only unreferenced files in our exact content-addressed layout are removed.
                for path in (self.root / "objects").glob("*/*"):
                    if (path.is_file() and _SHA.fullmatch(path.name) and path.parent.name == path.name[:2]
                            and not self._db.execute("SELECT 1 FROM assets WHERE sha256=?", (path.name,)).fetchone()):
                        path.unlink(missing_ok=True)
                cursor = self._db.execute("""UPDATE events SET status='interrupted', message=?
                    WHERE status IN ('pending','capturing','collecting') AND id NOT IN (SELECT id FROM snapshots)""",
                    ("Capture interrupted before an immutable snapshot was committed.",))
                self._recovery["events"] = cursor.rowcount
                rows = self._db.execute("SELECT id,fields FROM jobs WHERE status IN ('queued','pending','running','cancelling')").fetchall()
                for row in rows:
                    fields = _decode(row["fields"], {})
                    fields["recovery_message"] = "Reconstruction interrupted by process exit; submit a new job to retry."
                    self._db.execute("UPDATE jobs SET status='interrupted',updated_at=?,fields=? WHERE id=?", (_now(), _json(fields), row["id"]))
                self._recovery["jobs"] = len(rows)
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def _open(self, writing=False):
        if self._closed:
            raise ArchiveError("archive is closed")
        if writing and self.read_only:
            raise ArchiveError("archive is read-only")

    def _used(self):
        # Objects are immutable and their verified sizes are transactional metadata.
        # Avoid filesystem stat() for every old object on every new observation.
        objects = self._db.execute("SELECT coalesce(sum(size),0) FROM assets").fetchone()[0]
        total = objects
        for name in (_DB, _DB + "-journal", _DB + "-wal", _DB + "-shm", ".writer.lock"):
            path = self.root / name
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass
        temporary = self.root / "tmp"
        if temporary.is_dir() and not temporary.is_symlink():
            for path in temporary.iterdir():
                try:
                    if path.name.startswith(".pending-") and path.is_file() and not path.is_symlink():
                        total += path.stat().st_size
                except FileNotFoundError:
                    pass
        work = self.root / ".work"
        if work.is_dir() and not work.is_symlink():
            for task in work.iterdir():
                try:
                    if _TASK.fullmatch(task.name) and task.is_dir() and not task.is_symlink() and task.resolve() == task:
                        total += self._tree_bytes(task)
                except FileNotFoundError:
                    pass
        for reservation in self._reservations.values():
            for path in reservation.temporary_paths:
                # Export staging may be beside an explicitly caller-owned destination.
                try:
                    if not path.is_relative_to(self.root) and path.is_file() and not path.is_symlink():
                        total += path.stat().st_size
                except FileNotFoundError:
                    pass
        return total

    def _check_quota(self, extra=0, projected_database=False):
        used = self._used()
        if projected_database:
            pages = self._db.execute("PRAGMA page_count").fetchone()[0]
            page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
            used += max(0, pages * page_size - (self.root / _DB).stat().st_size)
        owner = threading.get_ident()
        reserved = {key: self._remaining_reservation(value) for key, value in self._reservations.items()}
        used += sum(value for key, value in reserved.items() if key != owner)
        used += max(extra, reserved.get(owner, 0))
        if used + _METADATA_RESERVE > self.limit_bytes:
            self._quota_hit = True
            raise QuotaExceeded("Archive size limit reached; recording must pause. Existing records were preserved.")

    @contextlib.contextmanager
    def _transaction(self, extra=0):
        with self._lock:
            self._open(True)
            self._check_quota(extra)
            old_size = (self.root / _DB).stat().st_size
            reservation = self._reservations.get(threading.get_ident())
            journal = self.root / (_DB + "-journal")
            if reservation is not None:
                reservation.temporary_paths.add(journal)
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._check_quota(projected_database=True)
                self._db.commit()
                if reservation is not None:
                    reservation.persistent += max(0, (self.root / _DB).stat().st_size - old_size)
            except sqlite3.Error as exc:
                self._db.rollback()
                raise ArchiveError("archive database operation failed") from exc
            except OSError as exc:
                self._db.rollback()
                if exc.errno == errno.ENOSPC:
                    self._quota_hit = True
                    raise QuotaExceeded("Storage is full; recording must pause. Existing records were preserved.") from exc
                raise ArchiveError("archive filesystem operation failed") from exc
            except Exception:
                self._db.rollback()
                raise
            finally:
                if reservation is not None:
                    reservation.temporary_paths.discard(journal)

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True
                self._release_writer()

    def new_session(self, info: dict) -> str:
        encoded = _json(info)
        ident = "session_" + uuid.uuid4().hex
        with self._transaction(len(encoded.encode("utf-8")) + 4096):
            self._db.execute("INSERT INTO sessions VALUES(?,?,?)", (ident, _now(), encoded))
        return ident

    def add_event(self, session_id, document_id, revision, kind, raw=None, status="pending", message=None) -> int:
        encoded = _json(raw) if raw is not None else None
        message = _safe(message)
        if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 0):
            raise ArchiveError("revision must be a nonnegative integer or null")
        with self._transaction(len((encoded or "").encode("utf-8")) + 8192):
            try:
                cur = self._db.execute("INSERT INTO events(session_id,document_id,revision,kind,raw,status,message,observed_at) VALUES(?,?,?,?,?,?,?,?)",
                    (session_id, _safe(document_id), revision, _safe(str(kind)), encoded, str(status), message, _now()))
            except sqlite3.IntegrityError as exc:
                raise ArchiveError("unknown archive session") from exc
            return cur.lastrowid

    def set_event_status(self, event_id, status, message=None):
        with self._transaction(4096):
            cur = self._db.execute("UPDATE events SET status=?,message=? WHERE id=?", (str(status), _safe(message), event_id))
            if cur.rowcount != 1:
                raise ArchiveError("event does not exist")

    def _object_path(self, sha256):
        if not isinstance(sha256, str) or not _SHA.fullmatch(sha256):
            raise ArchiveError("invalid asset digest")
        path = self.root / "objects" / sha256[:2] / sha256
        for component in (path.parent.parent, path.parent, path):
            if component.is_symlink() or (component.exists() and component.resolve() != component):
                raise ArchiveError("archive asset path must not contain links or junctions")
        return path

    def asset_path(self, sha256) -> Path:
        with self._lock:
            self._open()
            path = self._object_path(sha256)
            if not self._db.execute("SELECT 1 FROM assets WHERE sha256=?", (sha256,)).fetchone() or not path.is_file():
                raise ArchiveError("asset is unavailable")
            return path

    def read_asset(self, sha256) -> bytes:
        with self._lock:
            path = self.asset_path(sha256)
            if path.stat().st_size > _MAX_ASSET:
                raise ArchiveError("asset exceeds the 64 MiB read bound; use the streamed archive export")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != sha256:
                raise ArchiveError("asset integrity check failed")
            return data

    def _publish_asset(self, source, published):
        if isinstance(source, bytes):
            size = len(source)
            if size > _MAX_ASSET:
                raise ArchiveError("asset exceeds the 64 MiB storage bound")
            sha = hashlib.sha256(source).hexdigest()
            stream = lambda: io.BytesIO(source)
        elif isinstance(source, Path):
            if not source.is_file():
                raise ArchiveError("asset input is not a regular file")
            size = source.stat().st_size
            if size > _MAX_ASSET:
                raise ArchiveError("asset exceeds the 64 MiB storage bound")
            # Hash before copying so already stored assets need neither space nor another file.
            digest = hashlib.sha256()
            with source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(_CHUNK), b""):
                    digest.update(chunk)
            sha = digest.hexdigest()
            stream = lambda: source.open("rb")
        else:
            raise ArchiveError("asset values must be bytes or Path objects")
        target = self._object_path(sha)
        if target.exists():
            if target.stat().st_size != size:
                raise ArchiveError("stored asset size is inconsistent")
            with target.open("rb") as handle:
                digest = hashlib.sha256()
                for chunk in iter(lambda: handle.read(_CHUNK), b""):
                    digest.update(chunk)
            if digest.hexdigest() != sha:
                raise ArchiveError("stored asset integrity check failed")
        else:
            self._check_quota(size + 8192)
            target.parent.mkdir(exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=self.root / "tmp")
            temporary = Path(temporary)
            reservation = self._reservations.get(threading.get_ident())
            if reservation is not None:
                reservation.temporary_paths.add(temporary)
            try:
                copied = hashlib.sha256()
                count = 0
                with os.fdopen(fd, "wb") as output, stream() as source_stream:
                    for chunk in iter(lambda: source_stream.read(_CHUNK), b""):
                        count += len(chunk)
                        if count > size:
                            raise ArchiveError("asset changed while copying")
                        output.write(chunk)
                        copied.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if count != size or copied.hexdigest() != sha:
                    raise ArchiveError("asset changed while copying")
                os.replace(temporary, target)
                published.append(target)
                if reservation is not None:
                    reservation.persistent += size
            finally:
                temporary.unlink(missing_ok=True)
                if reservation is not None:
                    reservation.temporary_paths.discard(temporary)
        return {"sha256": sha, "size": size}

    def _write_assets(self, assets, published):
        if not isinstance(assets, dict) or len(assets) > 128:
            raise ArchiveError("assets must be a mapping with at most 128 entries")
        out = {}
        for name, source in sorted(assets.items()):
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise ArchiveError("invalid asset name")
            ref = self._publish_asset(source, published)
            out[name] = ref
            self._db.execute("INSERT OR IGNORE INTO assets VALUES(?,?)", (ref["sha256"], ref["size"]))
        return out

    def _discard_published(self, published):
        with self._lock:
            for path in published:
                if not self._db.execute("SELECT 1 FROM assets WHERE sha256=?", (path.name,)).fetchone():
                    size = path.stat().st_size if path.exists() else 0
                    path.unlink(missing_ok=True)
                    reservation = self._reservations.get(threading.get_ident())
                    if reservation is not None:
                        reservation.persistent = max(0, reservation.persistent - size)

    def _document(self, references):
        ref = references.get("document.json")
        if not ref:
            return None
        path = self._object_path(ref["sha256"])
        if ref["size"] > 64 * 1024 * 1024:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            return None

    def _inference(self, previous, summary, document_id):
        result = {"inferred": True, "kind": "unavailable", "evidence": [], "linked_snapshot_id": None,
                  "note": "This is an inference from observations, never the native undo stack or an internal ModelState."}
        if previous is None:
            result["evidence"] = ["No earlier committed observation of this document."]
            return result
        old = previous["summary"]
        result["kind"] = "observed_change"
        result["evidence"] = [{"previous_snapshot_id": previous["id"], "previous_undo": old.get("undo"), "undo": summary.get("undo"),
                               "previous_redo": old.get("redo"), "redo": summary.get("redo")}]
        digest = summary.get("document_hash")
        if not digest:
            return result
        row = self._db.execute("SELECT * FROM snapshots WHERE document_id IS ? AND document_hash=? AND id<=? ORDER BY id DESC LIMIT 1",
                               (document_id, digest, previous["id"])).fetchone()
        linked = self._record(row, "snapshot") if row else None
        undo = summary.get("undo")
        redo = summary.get("redo")
        old_undo, old_redo = old.get("undo"), old.get("redo")
        depths = all(isinstance(v, int) and not isinstance(v, bool) for v in (undo, redo, old_undo, old_redo))
        if linked and linked["id"] != previous["id"] and depths:
            if undo < old_undo and redo > old_redo:
                result["kind"] = "possible_undo"
                result["linked_snapshot_id"] = linked["id"]
            elif undo > old_undo and redo < old_redo:
                result["kind"] = "possible_redo"
                result["linked_snapshot_id"] = linked["id"]
            if result["linked_snapshot_id"] is not None:
                result["evidence"].append({"matching_document_hash": digest, "matching_snapshot_id": linked["id"],
                                           "limitation": "Unobserved edits or identical independent states can produce the same evidence."})
        return result

    def commit_snapshot(self, event_id, assets: dict, summary: dict) -> dict:
        published = []
        try:
            with self._transaction(16384):
                event = self._db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
                if event is None:
                    raise ArchiveError("event does not exist")
                if not isinstance(summary, dict):
                    raise ArchiveError("snapshot summary must be an object")
                for field in ("document_id", "revision"):
                    if field in summary and (summary[field] != event[field] or type(summary[field]) is not type(event[field])):
                        raise ArchiveError("snapshot source identity or revision does not match its observation event")
                existing = self._db.execute("SELECT * FROM snapshots WHERE id=?", (event_id,)).fetchone()
                if existing:
                    return self._record(existing, "snapshot")
                references = self._write_assets(assets, published)
                content = dict(summary)
                content["document_id"] = event["document_id"]
                content["revision"] = event["revision"]
                content.setdefault("source", "observed")
                if "document.json" in references:
                    content["document_hash"] = references["document.json"]["sha256"]
                content = _safe(content)
                previous_row = self._db.execute("SELECT * FROM snapshots WHERE document_id IS ? AND id<? ORDER BY id DESC LIMIT 1",
                                                (event["document_id"], event_id)).fetchone()
                previous = self._record(previous_row, "snapshot") if previous_row else None
                before = self._document(previous["assets"]) if previous else None
                after = self._document(references)
                diff = document_diff(before, after) if before is not None and after is not None else []
                inference = self._inference(previous, content, event["document_id"])
                self._db.execute("INSERT INTO snapshots(id,document_id,document_hash,created_at,summary,assets,diff,inference) VALUES(?,?,?,?,?,?,?,?)",
                    (event_id, event["document_id"], content.get("document_hash"), _now(), _json(content), _json(references), _json(diff), _json(inference)))
                self._db.execute("UPDATE events SET status='committed',message=NULL WHERE id=?", (event_id,))
            return self.snapshot(event_id)
        except Exception:
            # No row refers to these newly published files if the transaction failed.
            # Keep previously stored objects and every older observation intact.
            self._discard_published(published)
            raise

    def _record(self, row, kind):
        out = dict(row)
        out.pop("document_hash", None)  # Indexed lookup metadata; the public value is in summary.
        for field in ("summary", "assets", "diff", "inference"):
            out[field] = _decode(out[field])
        if kind == "snapshot":
            out["label"] = f"O{out['id']:04d}"
            out["event_id"] = out["id"]
        else:
            job = self._db.execute("SELECT snapshot_id,position FROM jobs WHERE id=?", (out["job_id"],)).fetchone()
            out["snapshot_id"], out["position"] = job["snapshot_id"], job["position"]
            out["label"] = f"O{job['snapshot_id']:04d}/S{job['position']}"
        return out

    def snapshot(self, snapshot_id) -> dict | None:
        with self._lock:
            self._open()
            row = self._db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            return self._record(row, "snapshot") if row else None

    def latest_snapshot(self, document_id=None) -> dict | None:
        with self._lock:
            self._open()
            if document_id is None:
                row = self._db.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
            else:
                row = self._db.execute("SELECT * FROM snapshots WHERE document_id=? ORDER BY id DESC LIMIT 1", (document_id,)).fetchone()
            return self._record(row, "snapshot") if row else None

    def list_events(self, after=0, limit=100, session_id=None) -> list[dict]:
        with self._lock:
            self._open()
            limit = max(1, min(int(limit), 1000))
            sql = "SELECT e.*,s.id AS snapshot_id FROM events e LEFT JOIN snapshots s ON s.id=e.id WHERE e.id>?"
            values = [int(after)]
            if session_id is not None:
                sql += " AND e.session_id=?"
                values.append(session_id)
            rows = self._db.execute(sql + " ORDER BY e.id LIMIT ?", values + [limit])
            out = []
            for row in rows:
                event = dict(row)
                event["label"] = f"O{event['id']:04d}"
                event["raw"] = _decode(event["raw"])
                out.append(event)
            return out

    def list_sessions(self) -> list[dict]:
        with self._lock:
            self._open()
            return [{"id": row["id"], "started_at": row["started_at"], "info": _decode(row["info"])}
                    for row in self._db.execute("SELECT * FROM sessions ORDER BY started_at DESC,id DESC LIMIT 1000")]

    def add_job(self, snapshot_id, position, version) -> str:
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ArchiveError("stage position must be a nonnegative prefix count")
        ident = "job_" + uuid.uuid4().hex
        timestamp = _now()
        with self._transaction(8192):
            if self.snapshot(snapshot_id) is None:
                raise ArchiveError("source snapshot does not exist")
            self._db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                             (ident, snapshot_id, position, _safe(str(version)), "queued", timestamp, timestamp, "{}"))
        return ident

    def update_job(self, job_id, status, **fields):
        with self._transaction(len(_json(fields).encode("utf-8")) + 4096):
            row = self._db.execute("SELECT fields FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ArchiveError("job does not exist")
            protected = {"id", "snapshot_id", "position", "version", "created_at", "updated_at", "status"}
            if protected.intersection(fields):
                raise ArchiveError("job identity fields cannot be overwritten")
            merged = _decode(row["fields"])
            merged.update(fields)
            self._db.execute("UPDATE jobs SET status=?,updated_at=?,fields=? WHERE id=?", (str(status), _now(), _json(merged), job_id))

    def _job_record(self, row):
        out = dict(row)
        extra = _decode(out.pop("fields"), {})
        out.update(extra)
        out["label"] = f"O{out['snapshot_id']:04d}/S{out['position']}"
        return out

    def get_job(self, job_id) -> dict | None:
        with self._lock:
            self._open()
            row = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self._job_record(row) if row else None

    def list_jobs(self) -> list[dict]:
        with self._lock:
            self._open()
            return [self._job_record(row) for row in self._db.execute("SELECT * FROM jobs ORDER BY created_at DESC,id DESC LIMIT 1000")]

    def commit_stage(self, job_id, assets, summary) -> dict:
        published = []
        try:
            with self._transaction(16384):
                existing = self._db.execute("SELECT * FROM stages WHERE job_id=?", (job_id,)).fetchone()
                if existing:
                    return self._record(existing, "stage")
                job = self.get_job(job_id)
                if job is None:
                    raise ArchiveError("job does not exist")
                source = self.snapshot(job["snapshot_id"])
                references = self._write_assets(assets, published)
                content = dict(summary)
                actual_version = content.get("cli_version") or content.get("version") or job["version"]
                content.update({"source": "rebuilt", "position": job["position"], "version": actual_version,
                                "cli_version": actual_version, "requested_version": job["version"],
                                "input_document_hash": source["summary"].get("document_hash")})
                # A reconstruction has no observed native history stacks.
                content.pop("undo", None)
                content.pop("redo", None)
                if "document.json" in references:
                    content["document_hash"] = references["document.json"]["sha256"]
                before, after = self._document(source["assets"]), self._document(references)
                diff = document_diff(before, after) if before is not None and after is not None else []
                inference = {"inferred": True, "kind": "reconstruction", "linked_snapshot_id": source["id"],
                             "evidence": ["Independent CLI reconstruction from the archived document at the requested prefix count."],
                             "note": "This is not a captured internal ModelState and has no observed undo/redo stack."}
                ident = "stage_" + uuid.uuid4().hex
                self._db.execute("INSERT INTO stages VALUES(?,?,?,?,?,?,?)", (ident, job_id, _now(), _json(content), _json(references), _json(diff), _json(inference)))
                fields = self._db.execute("SELECT fields FROM jobs WHERE id=?", (job_id,)).fetchone()
                fields = _decode(fields["fields"])
                fields["stage_id"] = ident
                self._db.execute("UPDATE jobs SET status='completed',updated_at=?,fields=? WHERE id=?", (_now(), _json(fields), job_id))
            return self.stage(ident)
        except Exception:
            self._discard_published(published)
            raise

    def commit_job_partial(self, job_id, assets, diagnostic, status) -> dict:
        """Retain genuine failed-job evidence without publishing a reconstructed stage."""
        if status not in {"failed", "cancelled", "timed_out"}:
            raise ArchiveError("partial diagnostics require failed, cancelled, or timed_out status")
        if not isinstance(assets, dict):
            raise ArchiveError("partial assets must be a mapping")
        total = 0
        for value in assets.values():
            if isinstance(value, bytes):
                total += len(value)
            elif isinstance(value, Path) and value.is_file():
                total += value.stat().st_size
            else:
                raise ArchiveError("partial asset values must be bytes or file Paths")
        if total > 128 * 1024 * 1024:
            raise ArchiveError("partial job evidence exceeds the 128 MiB bundle bound")
        published = []
        try:
            with self._transaction(16384):
                row = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise ArchiveError("job does not exist")
                fields = _decode(row["fields"], {})
                if "partial_assets" in fields:
                    return self._job_record(row)
                if self._db.execute("SELECT 1 FROM stages WHERE job_id=?", (job_id,)).fetchone():
                    raise ArchiveError("a completed reconstructed stage cannot be replaced by failure evidence")
                references = self._write_assets(assets, published)
                fields.update({"partial_assets": references, "diagnostic": _safe(diagnostic),
                               "message": "Incomplete CLI evidence only; no reconstructed Stage or internal ModelState was published."})
                self._db.execute("UPDATE jobs SET status=?,updated_at=?,fields=? WHERE id=?", (status, _now(), _json(fields), job_id))
            return self.get_job(job_id)
        except Exception:
            self._discard_published(published)
            raise

    def stage(self, stage_id) -> dict | None:
        with self._lock:
            self._open()
            row = self._db.execute("SELECT * FROM stages WHERE id=?", (stage_id,)).fetchone()
            return self._record(row, "stage") if row else None

    def status(self) -> dict:
        with self._lock:
            self._open()
            used = self._used()
            counts = {table: self._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("sessions", "events", "snapshots", "jobs", "stages", "assets")}
            reserved = sum(self._remaining_reservation(value) for value in self._reservations.values())
            budget_used = used + reserved
            return {"root": str(self.root), "read_only": self.read_only, "limit_bytes": self.limit_bytes,
                    "used_bytes": used, "reserved_bytes": reserved, "budget_used_bytes": budget_used,
                    "remaining_bytes": max(0, self.limit_bytes - budget_used),
                    "quota_exceeded": self._quota_hit or budget_used + _METADATA_RESERVE > self.limit_bytes,
                    "recording_allowed": not self.read_only and not self._quota_hit and budget_used + _METADATA_RESERVE <= self.limit_bytes,
                    "counts": counts, "recovery": dict(self._recovery)}

    def export_zip(self, destination: Path) -> Path:
        destination = Path(destination).resolve()
        with self._lock:
            self._open(True)
            if destination == self.root / _DB or self.root / "objects" in destination.parents or self.root / "tmp" in destination.parents:
                raise ArchiveError("export destination conflicts with archive storage")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.parent.parent == self.root / ".work":
                workdir = self._workdir(destination.parent)
                own_workdir = False
            else:
                workdir = self.root / ".work" / ("export-" + uuid.uuid4().hex)
                own_workdir = True
            database_size = (self.root / _DB).stat().st_size
            object_size = self._db.execute("SELECT coalesce(sum(size),0) FROM assets").fetchone()[0]
            asset_count = self._db.execute("SELECT count(*) FROM assets").fetchone()[0]
            estimate = object_size + 2 * database_size + (object_size + database_size) // 100 + max(2 * 1024 * 1024, asset_count * 512)
        with self.reserve(estimate, workdir=workdir) as reservation:
            workdir.mkdir(parents=True, exist_ok=True)
            try:
                return self._export_zip(destination, workdir, reservation)
            finally:
                if own_workdir:
                    # All files we created have their own finally-cleanup below.
                    workdir.rmdir()

    def _export_zip(self, destination, workdir, reservation):
        # Back up a consistent SQLite view under the connection lock. Compression
        # and content hashing then use only the private backup and immutable CAS,
        # so native event persistence is not blocked for the whole download.
        with tempfile.TemporaryDirectory(prefix=".backup-", dir=workdir) as directory:
            database = Path(directory) / _DB
            backup = sqlite3.connect(database)
            try:
                with self._lock:
                    self._open(True)
                    database_size = (self.root / _DB).stat().st_size
                    object_size = self._db.execute("SELECT coalesce(sum(size),0) FROM assets").fetchone()[0]
                    asset_count = self._db.execute("SELECT count(*) FROM assets").fetchone()[0]
                    needed = object_size + 2 * database_size + (object_size + database_size) // 100 + max(2 * 1024 * 1024, asset_count * 512)
                    if needed > reservation.amount:
                        extra = needed - reservation.amount
                        self._check_quota(self._remaining_reservation(reservation) + extra)
                        reservation.amount = needed
                    self._db.backup(backup)
                    shas = [row[0] for row in backup.execute("SELECT sha256 FROM assets ORDER BY sha256")]
            finally:
                backup.close()
            fd, name = tempfile.mkstemp(prefix=".archive-", suffix=".zip", dir=destination.parent)
            os.close(fd)
            temporary = Path(name)
            if not temporary.is_relative_to(workdir):
                with self._lock:
                    reservation.temporary_paths.add(temporary)
            try:
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
                    output.write(database, _DB)
                    output.writestr("manifest.json", _json({"format": "solidcraft-observation-archive", "version": 1,
                                                           "exported_at": _now(), "asset_count": len(shas)}))
                    for sha in shas:
                        path = self._object_path(sha)
                        if not path.is_file():
                            raise ArchiveError("cannot export an archive with a missing asset")
                        digest = hashlib.sha256()
                        with path.open("rb") as handle:
                            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                                digest.update(chunk)
                        if digest.hexdigest() != sha:
                            raise ArchiveError("cannot export an archive with a corrupt asset")
                        output.write(path, f"objects/{sha[:2]}/{sha}")
                with temporary.open("rb+") as handle:
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
                with self._lock:
                    reservation.temporary_paths.discard(temporary)
        return destination
