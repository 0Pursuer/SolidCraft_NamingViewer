"""Run a bounded, source-faithful recompute experiment in an owned CLI.

The live archive is read-only.  These are fresh CLI evaluations of copied
Documents, never telemetry from the application's original ModelState/cache.
Only timeline.edit, edit.undo and timeline.compute_all are used for the demo.
"""
from __future__ import annotations

from collections import deque
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from rebuild import (OfflineClient, RebuildError, JobCancelled, JobTimedOut,
                     TERMINAL, _check, _parse_document, _partial_assets,
                     _safe_error, _same_version, derive_document)


MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 32 * 1024 * 1024
MAX_PHASE_BYTES = 48 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_PENDING = 2
MAX_JOBS = 512
MAX_FEATURES = 64
DEFAULT_LIMIT_BYTES = 256 * 1024 * 1024
WORK_RESERVE_BYTES = 96 * 1024 * 1024
ASSET_NAMES = frozenset({"document.json", "inspect.json", "sketches.json",
    "topology.json", "geometry.json", "model.3mf", "derived.json",
    "native-reads.json", "partial-manifest.json"})
JOB_ID = re.compile(r"[0-9a-f]{32}")
LIMITATIONS = [
    "独立 CLI 从采样 Document 重新计算；不是原进程的重算遥测或原始 ModelState。",
    "逐特征前缀复用、cache/kept/rolled 命中路径未由公开命令返回；不按耗时猜测。",
    "timeline.edit 只返回本次原生总计 recomputed/restored；Undo 未返回重算计数。",
    "应用容量预算包含本工具档案与临时文件；原生导出返回前不构成操作系统硬盘配额。",
]


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


class RecomputeLab:
    """One worker, bounded queue, immutable assets, durable job manifests."""

    def __init__(self, archive, cli_path, root: Path, version, timeout=120,
                 limit_bytes=DEFAULT_LIMIT_BYTES):
        if not math.isfinite(float(timeout)) or not 0 < float(timeout) <= 600:
            raise ValueError("重算时限必须为 0–600 秒内的正数。")
        if type(limit_bytes) is not int or limit_bytes < MAX_MANIFEST_BYTES:
            raise ValueError("重算容量预算无效。")
        self.archive = archive
        self.cli_path = Path(cli_path).resolve()
        self.root = Path(root).resolve()
        self.version = str(version)[:200]
        self.timeout = float(timeout)
        self.limit_bytes = limit_bytes
        self._condition = threading.Condition(threading.RLock())
        self._pending = deque()
        self._jobs = {}
        self._cancel = {}
        self._active_client = None
        self._stopped = False
        self.last_error = None
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs_root = self.root / "jobs"
        self._work_root = self.root / ".work"
        self._owned(self._jobs_root).mkdir(exist_ok=True)
        self._owned(self._work_root).mkdir(exist_ok=True)
        # A second server must not reinterpret a running job as crash recovery.
        self._lock_stream = self._owned(self.root / ".lab.lock").open("a+b")
        self._lock_root()
        try:
            self._recover()
        except BaseException:
            self._unlock_root()
            raise
        self._thread = threading.Thread(target=self._work, name="recompute-lab", daemon=True)
        self._thread.start()

    def _owned(self, path):
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        if not path.is_relative_to(self.root):
            raise RebuildError("重算路径越过本工具的独立目录。")
        for part in (path, *path.parents):
            if part == self.root.parent:
                break
            if part.is_symlink() or (part.exists() and part.resolve() != part):
                raise RebuildError("重算目录不能包含链接或 junction。")
        return path

    def _lock_root(self):
        try:
            if os.name == "nt":
                import msvcrt
                self._lock_stream.seek(0)
                if not self._lock_stream.read(1):
                    self._lock_stream.write(b"0")
                    self._lock_stream.flush()
                self._lock_stream.seek(0)
                msvcrt.locking(self._lock_stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            self._lock_stream.close()
            raise RebuildError("该重算目录已由另一个查看器使用。") from exc

    def _unlock_root(self):
        try:
            if os.name == "nt":
                import msvcrt
                self._lock_stream.seek(0)
                msvcrt.locking(self._lock_stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_stream.close()

    def _usage(self):
        total, entries = 0, 0
        for parent, dirs, files in os.walk(self.root, followlinks=False):
            for name in dirs + files:
                self._owned(Path(parent) / name)
                entries += 1
                if entries > 30_000:
                    raise RebuildError("重算目录文件数量超过上限。")
            for name in files:
                total += (Path(parent) / name).stat().st_size
        return total

    def _room(self, extra):
        if self._usage() + extra > self.limit_bytes:
            raise RebuildError("重算独立容量预算不足；已有记录保留。")

    def _atomic(self, path, raw):
        path = self._owned(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Keep a margin for a terminal failure manifest even after quota errors.
        self._room(len(raw) + 64 * 1024)
        temporary = self._owned(path.with_name(path.name + ".tmp"))
        try:
            with temporary.open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _persist(self, job):
        raw = _json(job)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise RebuildError("重算任务清单超过 4 MiB。")
        self._atomic(self._job_folder(job["id"]) / "result.json", raw)

    def _job_folder(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise RebuildError("无效重算任务编号。")
        return self._owned(self._jobs_root / ("job_" + job_id))

    def _recover(self):
        # A killed native exporter can leave more than its reservation. Remove
        # only our locked, validated temporary directories before publishing
        # recovery metadata, so quota exhaustion cannot prevent recovery.
        for path in self._work_root.iterdir():
            self._owned(path)
            if not path.is_dir() or not re.fullmatch(r"lab-[0-9a-f]{32}", path.name):
                raise RebuildError("重算临时目录包含未知文件。")
            shutil.rmtree(path)
        folders = sorted(self._jobs_root.iterdir())
        if len(folders) > MAX_JOBS:
            raise RebuildError("重算任务超过保留数量上限。")
        for folder in folders:
            self._owned(folder)
            if not folder.is_dir() or not re.fullmatch(r"job_[0-9a-f]{32}", folder.name):
                raise RebuildError("重算任务目录包含未知文件。")
            path = self._owned(folder / "result.json")
            if not path.is_file():
                self.last_error = "发现未发布清单的重算目录；保留其文件但不作为完成任务展示。"
                continue
            if path.stat().st_size > MAX_MANIFEST_BYTES:
                raise RebuildError("重算任务清单超过上限。")
            job = json.loads(path.read_bytes())
            if (not isinstance(job, dict) or job.get("id") != folder.name[4:]
                    or not isinstance(job.get("phases"), list) or len(job["phases"]) > 4):
                raise RebuildError("重算任务清单无效。")
            self._jobs[job["id"]] = job
            if job.get("status") not in TERMINAL:
                job.update(status="interrupted", updated_at=_now(),
                           message="上次查看器中断；隔离任务未确认完成，不自动重放。")
                self._persist(job)

    def _snapshot_document(self, snapshot_id):
        if type(snapshot_id) is not int or snapshot_id < 1:
            raise RebuildError("请选择有效观察记录。")
        snapshot = self.archive.snapshot(snapshot_id)
        if not snapshot:
            raise RebuildError("观察记录不存在。")
        asset = snapshot.get("assets", {}).get("document.json", {})
        sha = asset.get("sha256")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise RebuildError("该记录没有完整原生 Document。")
        path = self.archive.asset_path(sha)
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise RebuildError("重算演示输入超过 2 MiB。")
        with path.open("rb") as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES or hashlib.sha256(raw).hexdigest() != sha:
            raise RebuildError("重算输入容量或内容哈希校验失败。")
        _, document = _parse_document(raw)
        if len(document["features"]) > MAX_FEATURES:
            raise RebuildError("重算演示最多支持 64 个特征。")
        # Reuse the existing native-format, script and external-link boundary.
        derived, _ = derive_document(raw, len(document["features"]))
        return snapshot, raw, derived

    def submit(self, before_id, after_id, mode="edit_undo"):
        if mode not in {"edit_undo", "full"}:
            raise RebuildError("未知重算演示类型。")
        before, raw_before, derived_before = self._snapshot_document(before_id)
        after, raw_after, derived_after = self._snapshot_document(after_id)
        source_id = before.get("summary", {}).get("document_id")
        if not source_id or source_id != after.get("summary", {}).get("document_id"):
            raise RebuildError("只能比较同一现场文档的观察记录。")
        if mode == "edit_undo":
            from recompute_model import validate_replay_documents
            try:
                spec = validate_replay_documents(raw_before, raw_after)
            except ValueError as exc:
                raise RebuildError(str(exc)) from exc
        else:
            spec = None
        with self._condition:
            if self._stopped:
                raise RebuildError("重算工作线程已经停止。")
            if len(self._pending) >= MAX_PENDING:
                raise RebuildError("重算等待队列已满；最多 2 个等待任务。")
            if len(self._jobs) >= MAX_JOBS:
                raise RebuildError("重算任务已达到 512 条保留上限。")
            self._room(WORK_RESERVE_BYTES + len(raw_before) + len(raw_after))
            job_id = uuid.uuid4().hex
            stamp = _now()
            job = {"id": job_id, "status": "queued", "mode": mode,
                "before_id": before_id, "after_id": after_id, "version": self.version,
                "cli_version": None, "created_at": stamp, "updated_at": stamp,
                "phases": [], "message": "等待隔离 CLI。", "spec": spec,
                "source": {"document_id": source_id,
                    "before_hash": hashlib.sha256(raw_before).hexdigest(),
                    "after_hash": hashlib.sha256(raw_after).hexdigest(),
                    "before_version": before.get("summary", {}).get("version", self.version),
                    "after_version": after.get("summary", {}).get("version", self.version)},
                "limitations": LIMITATIONS[:], "budget_bytes": self.limit_bytes,
                "original_model_state": False, "live_app_modified": False}
            try:
                self._persist(job)
            except BaseException:
                # No request was acknowledged and no assets have been created.
                folder = self._job_folder(job_id)
                if folder.is_dir() and not (folder / "result.json").exists():
                    shutil.rmtree(self._owned(folder))
                raise
            self._jobs[job_id] = job
            cancel = threading.Event()
            self._cancel[job_id] = cancel
            self._pending.append((job_id, raw_before, raw_after, derived_before, derived_after))
            self._condition.notify_all()
            return copy.deepcopy(job)

    def get(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            return None
        with self._condition:
            return copy.deepcopy(self._jobs.get(job_id))

    def list_jobs(self):
        with self._condition:
            return [copy.deepcopy(job) for job in sorted(self._jobs.values(),
                    key=lambda item: item["created_at"], reverse=True)]

    def asset_path(self, job_id, phase_index, asset_name):
        if type(phase_index) is not int or phase_index < 0 or asset_name not in ASSET_NAMES:
            raise RebuildError("无效重算资源。")
        with self._condition:
            job = self.get(job_id)
            if not job or phase_index >= len(job["phases"]):
                raise RebuildError("重算阶段不存在。")
            asset = job["phases"][phase_index].get("assets", {}).get(asset_name)
            if not isinstance(asset, dict):
                raise RebuildError("该阶段尚未取得这份资源。")
            path = self._owned(self._job_folder(job_id) / "phases" / str(phase_index) / asset_name)
            if not path.is_file() or path.stat().st_size != asset.get("size"):
                raise RebuildError("重算资源缺失或损坏。")
            if path.stat().st_size > MAX_ASSET_BYTES:
                raise RebuildError("重算资源超过容量上限。")
            # Keep Python 3.10 support and bound reads even if the file grows
            # between stat() and hashing. file_digest is only available in 3.11+.
            digest = hashlib.sha256()
            total = 0
            with path.open("rb") as stream:
                while chunk := stream.read(min(1024 * 1024, MAX_ASSET_BYTES - total + 1)):
                    total += len(chunk)
                    if total > MAX_ASSET_BYTES:
                        raise RebuildError("重算资源超过容量上限。")
                    digest.update(chunk)
            if total != asset.get("size") or digest.hexdigest() != asset.get("sha256"):
                raise RebuildError("重算资源内容哈希校验失败。")
            return path

    def cancel(self, job_id):
        with self._condition:
            event = self._cancel.get(job_id)
            if event is None:
                return self.get(job_id)
            event.set()
            queued = next((item for item in self._pending if item[0] == job_id), None)
            if queued:
                self._pending.remove(queued)
                self._cancel.pop(job_id, None)
                self._finish(job_id, "cancelled", "任务在启动前取消。")
            self._condition.notify_all()
            return self.get(job_id)

    def _finish(self, job_id, status, message):
        with self._condition:
            job = self._jobs[job_id]
            job.update(status=status, message=_safe_error(message), updated_at=_now())
            self._persist(job)

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._stopped)
                if self._stopped:
                    return
                entry = self._pending.popleft()
                cancel = self._cancel[entry[0]]
            try:
                self._run(entry, cancel)
            except BaseException as exc:
                status = "failed"
                if cancel.is_set() or isinstance(exc, JobCancelled):
                    status = "cancelled"
                elif isinstance(exc, JobTimedOut):
                    status = "timed_out"
                try:
                    self._finish(entry[0], status, exc)
                except Exception as storage_error:
                    self.last_error = _safe_error(storage_error)
            finally:
                with self._condition:
                    self._active_client = None
                    self._cancel.pop(entry[0], None)

    def _phase(self, job_id, key, label, command=None, params=None, result=None):
        counts = {"recomputed": None, "restored": None, "availability": "not_returned"}
        if command == "timeline.edit" and isinstance(result, dict):
            if any(type(result.get(field)) is not int or result[field] < 0 for field in ("recomputed", "restored")):
                raise RebuildError("原生 timeline.edit 没有返回有效重算总计。")
            counts = {"recomputed": result["recomputed"], "restored": result["restored"],
                      "availability": "native_command_result"}
        phase = {"index": 0, "key": key, "label": label, "status": "capturing",
            "command": command, "command_params": params or {}, "command_result": result,
            "counts": counts, "assets": {}, "summary": None, "verification": None}
        with self._condition:
            job = self._jobs[job_id]
            phase["index"] = len(job["phases"])
            job["phases"].append(phase)
            job["updated_at"] = _now()
            self._persist(job)  # command reply durable before any refresh/read
        return phase

    def _publish_assets(self, job_id, phase, assets):
        if not isinstance(assets, dict) or set(assets) - ASSET_NAMES:
            raise RebuildError("隔离采集器返回未知资源名称。")
        total = 0
        for name, raw in assets.items():
            if not isinstance(raw, bytes) or len(raw) > MAX_ASSET_BYTES:
                raise RebuildError("隔离采集资源超过 32 MiB 或格式无效。")
            total += len(raw)
            if total > MAX_PHASE_BYTES:
                raise RebuildError("单个重算阶段超过 48 MiB。")
        manifest = {}
        for name, raw in assets.items():
            self._atomic(self._job_folder(job_id) / "phases" / str(phase["index"]) / name, raw)
            manifest[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
        with self._condition:
            phase["assets"].update(manifest)
            self._persist(self._jobs[job_id])

    @staticmethod
    def _verify_document(actual, expected):
        actual_json, actual_doc = _parse_document(actual)
        expected_json, expected_doc = _parse_document(expected)
        # Loading an --in file renames the document to that file's stem.  The
        # complete definitions, parameters and other fields must still agree.
        names = {"actual": actual_doc.get("name"), "expected": expected_doc.get("name")}
        for parsed, document in ((actual_json, actual_doc), (expected_json, expected_doc)):
            document.pop("name", None)
            # Native serde omits None, whereas derive_document explicitly sets
            # marker=None to mean the complete timeline. Both mean the end.
            if document.get("marker") is None:
                document.pop("marker", None)
        if actual_json != expected_json:
            raise RebuildError("实际 CLI 导出与目标 Document 的定义不一致。")
        return {"definitions_match": True,
                "comparison": "all_fields_except_load_name_and_equivalent_end_marker",
                "names": names, "note": "加载可按文件名重命名；末尾 marker 的 null/省略等价；不声称原始字节一致。"}

    def _capture(self, job_id, phase, client, workdir, expected, cancel, deadline):
        from capture import collect_bundle
        _check(cancel, deadline)
        self._room(32 * 1024 * 1024)
        assets, summary = collect_bundle(client, workdir, client.version, source="rebuilt")
        _check(cancel, deadline)
        verification = self._verify_document(assets["document.json"], expected)
        summary.update(source="isolated_recompute", original_model_state=False,
                       history_available=False, cli_version=client.version)
        self._publish_assets(job_id, phase, assets)
        with self._condition:
            phase.update(status="completed", summary=summary, verification=verification)
            self._jobs[job_id]["updated_at"] = _now()
            self._persist(self._jobs[job_id])

    def _run(self, entry, cancel):
        job_id, raw_before, raw_after, derived_before, derived_after = entry
        deadline = time.monotonic() + self.timeout
        _check(cancel, deadline)
        self._room(WORK_RESERVE_BYTES)
        self._finish(job_id, "running", "独立 CLI 正在重算；现场应用不接收修改命令。")
        workdir = self._owned(self._work_root / ("lab-" + uuid.uuid4().hex))
        workdir.mkdir()
        client, phase = None, None
        try:
            job = self._jobs[job_id]
            derived = derived_after if job["mode"] == "full" else derived_before
            path = workdir / "derived.json"
            self._atomic(path, derived)
            client = OfflineClient(self.cli_path, path, workdir, cancel, deadline)
            with self._condition:
                self._active_client = client
                job["cli_version"] = client.version
                self._persist(job)
            expected_version = job["source"]["after_version" if job["mode"] == "full" else "before_version"]
            if not _same_version(client.version, expected_version):
                raise RebuildError("CLI 与采样版本不一致；为避免冒充原版本结果，已拒绝演示。")
            if job["mode"] == "edit_undo" and not _same_version(client.version, job["source"]["after_version"]):
                raise RebuildError("CLI 与修改后采样版本不一致；已拒绝演示。")
            if job["mode"] == "full":
                result = client.query("timeline.compute_all", {}, timeout=self.timeout)
                phase = self._phase(job_id, "full", "完整重算", "timeline.compute_all", {}, result)
                self._capture(job_id, phase, client, workdir, derived_after, cancel, deadline)
            else:
                phase = self._phase(job_id, "baseline", "载入修改前文档")
                self._capture(job_id, phase, client, workdir, derived_before, cancel, deadline)
                self._clear_exports(workdir, client)
                spec = job["spec"]
                params = {"feature": spec["feature_id"], "set": {"extent.distance": spec["after_distance"]}}
                result = client.query("timeline.edit", params, timeout=self.timeout)
                phase = self._phase(job_id, "edited", "实际修改并重算", "timeline.edit", params, result)
                self._capture(job_id, phase, client, workdir, derived_after, cancel, deadline)
                self._clear_exports(workdir, client)
                result = client.query("edit.undo", {}, timeout=self.timeout)
                phase = self._phase(job_id, "undone", "实际 Undo 并恢复", "edit.undo", {}, result)
                self._capture(job_id, phase, client, workdir, derived_before, cancel, deadline)
            _check(cancel, deadline)
        except BaseException as exc:
            if client is not None:
                diagnostic = client.diagnostic()
                client.close()  # stop incomplete native writes before retaining partial files
                try:
                    if phase is None or phase.get("status") == "completed":
                        phase = self._phase(job_id, "partial", "失败诊断与部分采集")
                    partial = _partial_assets(workdir, client, hashlib.sha256(derived).hexdigest())
                    self._publish_assets(job_id, phase, partial)
                    with self._condition:
                        phase.update(status="incomplete", diagnostic=_safe_error(diagnostic),
                                     message=_safe_error(exc))
                        self._persist(job)
                except Exception as partial_error:
                    self.last_error = _safe_error(partial_error)
            if cancel.is_set():
                raise JobCancelled("隔离重算已取消；已取得阶段保留。") from exc
            if time.monotonic() >= deadline:
                raise JobTimedOut("隔离重算超过总时限；已取得阶段保留。") from exc
            raise
        finally:
            if client is not None:
                client.close()
            self._owned(workdir)
            shutil.rmtree(workdir)
        self._finish(job_id, "completed", "真实 CLI 演示完成；仅操作了独立文档副本。")

    def _clear_exports(self, workdir, client):
        for name in ("document.json", "model.3mf"):
            path = self._owned(workdir / name)
            if path.exists():
                path.unlink()
        client._exported.clear()

    def stop(self):
        with self._condition:
            if self._stopped:
                return
            self._stopped = True
            for event in self._cancel.values():
                event.set()
            while self._pending:
                entry = self._pending.popleft()
                self._cancel.pop(entry[0], None)
                try:
                    self._finish(entry[0], "cancelled", "查看器停止，等待任务已取消。")
                except Exception as exc:
                    self.last_error = _safe_error(exc)
            client = self._active_client
            self._condition.notify_all()
        if client is not None:
            client.close()
        while self._thread.is_alive():
            self._thread.join(timeout=1)
        self._unlock_root()
