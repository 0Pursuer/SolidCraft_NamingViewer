"""Reconstruct sampled feature prefixes in an isolated, owned SolidCraft CLI.

These stages are fresh evaluations of a sampled Document.  They are never
presented as the running application's original ModelState or undo snapshots.
"""
from __future__ import annotations

from collections import deque
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time


MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_REPLY_BYTES = 16 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_PARTIAL_READ_BYTES = 8 * 1024 * 1024
MAX_PENDING_JOBS = 4
MAX_FEATURES = 100_000
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
WORK_RESERVATION_BYTES = 256 * 1024 * 1024
TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out", "interrupted"})


class RebuildError(RuntimeError):
    pass


class JobCancelled(RebuildError):
    pass


class JobTimedOut(RebuildError):
    pass


def _safe_error(value) -> str:
    text = str(value)
    text = re.sub(r"(?i)(token|authorization|password|secret)(\s*[:=]\s*)[^\s,;]+", r"\1\2[redacted]", text)
    text = re.sub(r"(?i)\b[0-9a-f]{32,128}\b", "[redacted]", text)
    return text[:3000]


def _object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise RebuildError("Document JSON contains duplicate keys.")
        out[key] = value
    return out


def _parse_document(raw: bytes):
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise RebuildError("Document exceeds the 32 MiB reconstruction limit.")
    try:
        parsed = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RebuildError("The sampled document is not valid bounded JSON.") from exc
    if (not isinstance(parsed, dict) or not isinstance(parsed.get("format"), str)
            or not re.fullmatch(r"solidcraft/[0-9]+", parsed["format"]) or "commands" in parsed):
        raise RebuildError("The sample is not a native SolidCraft Document JSON.")
    # Native releases have used a flattened Document.  Permit explicit envelope
    # names too, without rewriting any unrelated fields or guessing a recipe.
    candidates = [parsed, parsed.get("document"), parsed.get("design")]
    documents = [item for item in candidates if isinstance(item, dict) and isinstance(item.get("features"), list)]
    if len(documents) != 1:
        raise RebuildError("The sampled Document has no unique feature list.")
    document = documents[0]
    if len(document["features"]) > MAX_FEATURES:
        raise RebuildError("The sampled timeline exceeds the reconstruction limit.")
    if any(not isinstance(feature, dict) for feature in document["features"]):
        raise RebuildError("The sampled timeline contains an invalid feature.")
    return parsed, document


def derive_document(raw: bytes, position: int) -> tuple[bytes, list]:
    """Change only a copy's history marker; reject unresolved external links."""
    parsed, document = _parse_document(raw)
    features = document["features"]
    if type(position) is not int or not 0 <= position <= len(features):
        raise RebuildError("Stage position must be a prefix count within the sampled timeline.")
    if document.get("links"):
        raise RebuildError(
            "external_dependencies: this sample has external design links; "
            "reconstruction is refused because their original contents are not frozen.")
    document["marker"] = position if position < len(features) else None
    ids = [feature.get("id") for feature in features[:position]]
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"), ids


def _check(cancel: threading.Event, deadline: float):
    if cancel.is_set():
        raise JobCancelled("Reconstruction was cancelled.")
    if time.monotonic() >= deadline:
        raise JobTimedOut("Reconstruction exceeded its time limit.")


class OfflineClient:
    """Bounded MCP stdio adapter implementing capture.collect_bundle's interface.

    No token is needed or accepted.  Every instance owns exactly one headless
    subprocess; it never discovers ports or passes --connect.
    """

    def __init__(self, cli_path: Path, document: Path | None, workdir: Path,
                 cancel: threading.Event, deadline: float):
        self.cli_path = Path(cli_path).resolve()
        self.workdir = Path(workdir).resolve()
        self.cancel = cancel
        self.deadline = deadline
        self.version = "unknown"
        self._execute_tool = None
        self._next_id = 1
        self._responses = queue.Queue(maxsize=8)
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._native_reads = []
        self._native_read_bytes = 0
        self._omitted_reads = 0
        self._exported = set()
        args = [str(self.cli_path), "mcp", "--headless"]
        if document is not None:
            args += ["--in", str(Path(document).resolve())]
        args += ["--allow-path", str(self.workdir)]
        env = dict(os.environ)
        # The child has no reason to inherit credentials for any running app.
        env = {key: value for key, value in env.items() if key.upper() not in {
            "SOLIDCRAFT_CONTROL_TOKEN", "SOLIDCRAFT_CONTROL_DIR", "SOLIDCRAFT_CONNECT"}}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, cwd=str(self.workdir),
                                            env=env, creationflags=flags, bufsize=0)
        except OSError as exc:
            raise RebuildError("Unable to start the installed headless SolidCraft CLI.") from exc
        self._reader = threading.Thread(target=self._read_stdout, name="rebuild-mcp-stdout", daemon=True)
        self._errors = threading.Thread(target=self._read_stderr, name="rebuild-mcp-stderr", daemon=True)
        self._reader.start()
        self._errors.start()
        try:
            initialized = self._rpc("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "solidcraft-transaction-viewer", "version": "1"},
            })
            self.version = str(initialized.get("serverInfo", {}).get("version", "unknown"))[:200]
            self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools = self._rpc("tools/list", {}).get("tools", [])
            if not isinstance(tools, list) or len(tools) > 1000:
                raise RebuildError("The CLI returned an invalid MCP tool catalog.")
            candidates = []
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                props = tool.get("inputSchema", {}).get("properties", {})
                if isinstance(props, dict) and "command" in props and "params" in props:
                    candidates.append(tool.get("name"))
            # Query the catalog rather than assuming a tool naming convention.
            if "execute" in candidates:
                self._execute_tool = "execute"
            elif len(candidates) == 1 and isinstance(candidates[0], str):
                self._execute_tool = candidates[0]
            else:
                raise RebuildError("The installed CLI exposes no unique command execution tool.")
        except BaseException:
            self.close()
            raise

    def _deliver(self, value):
        try:
            self._responses.put_nowait(value)
        except queue.Full:
            # A sequential client cannot legitimately receive eight unmatched
            # replies.  Stop reading and make the protocol failure visible.
            self.cancel.set()

    def _read_stdout(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_REPLY_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_REPLY_BYTES:
                    self._deliver(RebuildError("The CLI response exceeded 16 MiB."))
                    return
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    self._deliver(RebuildError("The CLI returned invalid MCP JSON."))
                    return
                if isinstance(message, dict) and "id" in message:
                    self._deliver(message)
        except (OSError, ValueError):
            pass
        finally:
            self._deliver(RebuildError("The headless CLI closed its output."))

    def _read_stderr(self):
        try:
            while True:
                chunk = self.process.stderr.read(4096)
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr.extend(chunk)
                    if len(self._stderr) > MAX_STDERR_BYTES:
                        del self._stderr[:-MAX_STDERR_BYTES]
        except (OSError, ValueError):
            return

    def diagnostic(self) -> str:
        with self._stderr_lock:
            return _safe_error(bytes(self._stderr).decode("utf-8", errors="replace"))

    def _write(self, value):
        _check(self.cancel, self.deadline)
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
        if len(encoded) > MAX_REPLY_BYTES:
            raise RebuildError("The MCP request exceeded 16 MiB.")
        try:
            remaining = memoryview(encoded)
            while remaining:
                written = self.process.stdin.write(remaining)
                if not written:
                    raise OSError("closed input")
                remaining = remaining[written:]
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RebuildError("Unable to write to the isolated CLI.") from exc

    def _rpc(self, method: str, params: dict, timeout: float | None = None):
        with self._io_lock:
            _check(self.cancel, self.deadline)
            request_id = self._next_id
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            end = self.deadline
            if timeout is not None:
                end = min(end, time.monotonic() + max(0.001, float(timeout)))
            while True:
                _check(self.cancel, end)
                try:
                    reply = self._responses.get(timeout=min(0.05, max(0.001, end - time.monotonic())))
                except queue.Empty:
                    continue
                if isinstance(reply, Exception):
                    raise reply
                if reply.get("id") != request_id:
                    raise RebuildError("The CLI returned an unexpected MCP response id.")
                if "error" in reply:
                    error = reply["error"]
                    detail = error.get("message", "MCP error") if isinstance(error, dict) else "MCP error"
                    raise RebuildError(_safe_error(detail))
                result = reply.get("result")
                if not isinstance(result, dict):
                    raise RebuildError("The CLI returned an invalid MCP result.")
                return result

    def query(self, command: str, params=None, timeout=30):
        result = self._rpc("tools/call", {
            "name": self._execute_tool, "arguments": {"command": command, "params": params or {}},
        }, timeout=timeout)
        content = result.get("content", [])
        text = next((part.get("text") for part in content
                     if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)), None)
        if result.get("isError"):
            raise RebuildError(_safe_error(text or "The isolated command failed."))
        if text is None:
            raise RebuildError("The isolated command returned no JSON text.")
        try:
            value = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise RebuildError("The isolated command returned invalid JSON text.") from exc
        if command in {"document.inspect", "document.revision", "sketch.inspect", "model.faces", "model.edges"}:
            recorded = json.dumps({"command": command, "params": params or {}, "result": value},
                                  ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(self._native_reads) < 512 and self._native_read_bytes + len(recorded) <= MAX_PARTIAL_READ_BYTES:
                self._native_reads.append(recorded)
                self._native_read_bytes += len(recorded)
            else:
                self._omitted_reads += 1
        return value

    def request(self, method: str, params=None, timeout=30):
        params = params or {}
        if method == "engine.execute":
            return self.query(params.get("command", ""), params.get("params", {}), timeout=timeout)
        if method in {"document.inspect", "document.revision"}:
            return self.query(method, params, timeout=timeout)
        raise RebuildError("This method is unavailable in the isolated reconstruction client.")

    def export(self, destination: Path, format: str, timeout=30):
        destination = Path(destination).resolve()
        if not destination.is_relative_to(self.workdir) or destination == self.workdir:
            raise RebuildError("The reconstruction export is outside its owned work directory.")
        if format not in {"json", "3mf-tagged"}:
            raise RebuildError("Unsupported reconstruction export format.")
        result = self.query("file.export", {"path": str(destination), "format": format}, timeout=timeout)
        self._exported.add(destination.name)
        return result

    def partial_reads(self) -> bytes:
        return (b'{"source":"isolated_cli","original_model_state":false,"reads":['
                + b",".join(self._native_reads)
                + b'],"omitted_reads":' + str(self._omitted_reads).encode("ascii") + b"}")

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            process = self.process
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            else:
                process.wait()
            for pipe in (process.stdin, process.stdout, process.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass
            # Readers see EOF after this owned process is reaped.  Never wait
            # for a reader while leaving a full stdout/stderr pipe undrained.
            self._reader.join(timeout=0.5)
            self._errors.join(timeout=0.5)

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def _collect_bundle(*args, **kwargs):
    # Lazy import keeps the transport independently testable and avoids a module
    # import cycle with the root-owned capture implementation.
    from capture import collect_bundle
    return collect_bundle(*args, **kwargs)


def _partial_assets(workdir: Path, client: OfflineClient | None, input_hash: str) -> dict:
    """Only retain files that actually exist and successful native read replies."""
    assets, manifest = {}, []
    total = 0
    for name in ("derived.json", "document.json", "model.3mf", "inspect.json",
                 "sketches.json", "topology.json", "geometry.json"):
        path = workdir / name
        if not path.is_file():
            continue
        info = {"name": name, "source": "derived_input" if name == "derived.json" else "owned_native_export",
                "native_export_completed": bool(client and name in client._exported)}
        try:
            info["size"] = path.stat().st_size
            if info["size"] > MAX_DOCUMENT_BYTES:
                info["omitted"] = "file exceeds 32 MiB limit"
            else:
                with path.open("rb") as source:
                    raw = source.read(MAX_DOCUMENT_BYTES + 1)
                if len(raw) > MAX_DOCUMENT_BYTES or total + len(raw) > MAX_BUNDLE_BYTES - MAX_REPLY_BYTES:
                    info["omitted"] = "partial bundle size limit"
                else:
                    assets[name] = raw
                    total += len(raw)
                    info["sha256"] = hashlib.sha256(raw).hexdigest()
        except OSError:
            info["omitted"] = "file could not be read after failure"
        manifest.append(info)
    if client is not None:
        assets["native-reads.json"] = client.partial_reads()
    assets["partial-manifest.json"] = json.dumps({
        "source": "rebuilt", "incomplete": True, "original_model_state": False,
        "input_document_hash": input_hash, "files": manifest,
        "note": "Only successful read replies and existing owned files are retained. An unfinished export may be truncated.",
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return assets


def _same_version(left: str, right: str) -> bool:
    pattern = r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)"
    a, b = re.search(pattern, str(left)), re.search(pattern, str(right))
    return a.group(1) == b.group(1) if a and b else str(left) == str(right)


class Rebuilder:
    def __init__(self, archive, cli_path: Path, version: str, timeout=120):
        if not math.isfinite(float(timeout)) or float(timeout) <= 0:
            raise ValueError("Reconstruction timeout must be finite and positive.")
        self.archive = archive
        self.cli_path = Path(cli_path).resolve()
        self.version = str(version)[:200]
        self.timeout = float(timeout)
        self._condition = threading.Condition()
        self._pending = deque()
        self._cancel = {}
        self._stopped = False
        self.last_error = None
        self._active_client = None
        self._work_root = Path(archive.root).resolve() / ".work"
        self._thread = threading.Thread(target=self._work, name="transaction-rebuilder", daemon=True)
        self._thread.start()

    def submit(self, snapshot_id: int, position: int) -> dict:
        if type(snapshot_id) is not int or snapshot_id < 1:
            raise RebuildError("A valid observation id is required.")
        snapshot = self.archive.snapshot(snapshot_id)
        if snapshot is None:
            raise RebuildError("The observation does not exist.")
        asset = snapshot.get("assets", {}).get("document.json")
        if not isinstance(asset, dict) or not isinstance(asset.get("sha256"), str):
            raise RebuildError("This observation has no native Document JSON to reconstruct.")
        raw = self._read_document(asset["sha256"])
        # Validate prefix bounds immediately; dependency failures belong to the
        # durable job so the UI can show their explicit reason.
        _, document = _parse_document(raw)
        if type(position) is not int or not 0 <= position <= len(document["features"]):
            raise RebuildError("Stage position must be a prefix count within the sampled timeline.")
        with self._condition:
            if self._stopped:
                raise RebuildError("The reconstruction worker has stopped.")
            if len(self._pending) >= MAX_PENDING_JOBS:
                raise RebuildError("The reconstruction queue is full; wait for or cancel a job.")
            job_id = self.archive.add_job(snapshot_id, position, self.version)
            self.archive.update_job(job_id, "queued")
            self._cancel[job_id] = threading.Event()
            self._pending.append((job_id, snapshot_id, position, asset["sha256"]))
            self._condition.notify()
            return self.archive.get_job(job_id)

    def cancel(self, job_id):
        with self._condition:
            event = self._cancel.get(job_id)
            if event is None:
                return
            event.set()
            queued = next((item for item in self._pending if item[0] == job_id), None)
            if queued is not None:
                self._pending.remove(queued)
                self._cancel.pop(job_id, None)
                self._record_failure(job_id, "cancelled", "Reconstruction was cancelled before starting.")
            self._condition.notify_all()

    def stop(self):
        with self._condition:
            if self._stopped:
                return
            self._stopped = True
            for event in self._cancel.values():
                event.set()
            while self._pending:
                job_id, *_ = self._pending.popleft()
                self._cancel.pop(job_id, None)
                self._record_failure(job_id, "cancelled", "The reconstruction worker stopped.")
            client = self._active_client
            self._condition.notify_all()
        if client is not None:
            client.close()
        # Cancellation closes the owned CLI immediately.  The bounded Python
        # geometry adapter may still be unwinding; its archive/workspace owners
        # must remain alive until it actually leaves the worker.
        while self._thread.is_alive():
            self._thread.join(timeout=1)

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._stopped)
                if self._stopped and not self._pending:
                    return
                job = self._pending.popleft()
                cancel = self._cancel[job[0]]
            try:
                self._run(job, cancel)
            except JobCancelled as exc:
                self._record_failure(job[0], "cancelled", exc)
            except JobTimedOut as exc:
                self._record_failure(job[0], "timed_out", exc)
            except Exception as exc:
                self._record_failure(job[0], "cancelled" if cancel.is_set() else "failed", exc)
            finally:
                with self._condition:
                    self._active_client = None
                    self._cancel.pop(job[0], None)

    def _record_failure(self, job_id, status, exc):
        try:
            current = self.archive.get_job(job_id)
            if current and current.get("partial_assets"):
                return  # already atomically published with its original diagnostic
            self.archive.update_job(job_id, status, message=_safe_error(exc))
        except Exception as storage_error:
            # A closed/full archive must not kill the worker while leaving its
            # queued jobs or owned subprocess without cleanup.  Recovery will
            # expose any durable job that could not be updated.
            self.last_error = _safe_error(storage_error)

    def _read_document(self, sha):
        path = self.archive.asset_path(sha)
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise RebuildError("Document exceeds the 32 MiB reconstruction limit.")
        with path.open("rb") as source:
            raw = source.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise RebuildError("Document exceeds the 32 MiB reconstruction limit.")
        return raw

    def _run(self, job, cancel):
        job_id, snapshot_id, position, document_hash = job
        deadline = time.monotonic() + self.timeout
        _check(cancel, deadline)
        self.archive.update_job(job_id, "running")
        snapshot = self.archive.snapshot(snapshot_id)
        if snapshot is None:
            raise RebuildError("The source observation is unavailable.")
        original = self._read_document(document_hash)
        if hashlib.sha256(original).hexdigest() != document_hash:
            raise RebuildError("The source Document failed its content hash check.")
        derived, prefix_ids = derive_document(original, position)
        self._work_root.mkdir(parents=True, exist_ok=True)
        with ExitStack() as resources:
            temporary = tempfile.TemporaryDirectory(prefix="rebuild-", dir=self._work_root)
            resources.callback(temporary.cleanup)
            workdir = Path(temporary.name).resolve()
            if not workdir.is_relative_to(self._work_root.resolve()):
                raise RebuildError("The reconstruction directory escaped its owned root.")
            resources.enter_context(self.archive.reserve(WORK_RESERVATION_BYTES, workdir=workdir))
            # Cleanup runs before the reservation is released, including faults.
            resources.callback(temporary.cleanup)
            document_path = workdir / "derived.json"
            document_path.write_bytes(derived)
            client = None
            try:
                client = OfflineClient(self.cli_path, document_path, workdir, cancel, deadline)
                with self._condition:
                    self._active_client = client
                _check(cancel, deadline)
                assets, summary = _collect_bundle(client, workdir, client.version,
                                                  expected_key=None, source="rebuilt")
                _check(cancel, deadline)
                summary = dict(summary)
                warnings = list(summary.get("warnings") or [])
                warnings.append("Rebuilt from sampled definitions; original internal ModelState and undo/redo snapshots are unavailable.")
                input_version = str(snapshot.get("summary", {}).get("version", self.version))
                if not _same_version(client.version, input_version):
                    warnings.append("CLI version differs from the observed application's version; reconstructed geometry may differ.")
                if client.version == "unknown":
                    warnings.append("The installed CLI did not report its version.")
                failing = [item for item in summary.get("timeline", [])
                           if isinstance(item, dict) and item.get("error")]
                if failing:
                    warnings.append(f"Reconstruction contains {len(failing)} failing feature(s); geometry includes successful results only.")
                    summary["completeness"] = "partial_reconstruction"
                summary.update({
                    "source": "rebuilt", "undo": None, "redo": None,
                    "snapshot_id": snapshot_id, "position": position,
                    "input_document_hash": document_hash,
                    "derived_document_hash": hashlib.sha256(derived).hexdigest(),
                    "prefix_feature_ids": prefix_ids, "cli_version": client.version,
                    "input_version": input_version, "version": client.version,
                    "warnings": warnings,
                    "history_available": False, "native_model_state_available": False,
                })
                # The source sample remains immutable.  Keep the exact derived
                # input even if collect_bundle exports a normalized Document.
                assets = dict(assets)
                assets["derived.json"] = derived
                total = 0
                for name, value in assets.items():
                    if isinstance(value, Path):
                        if not value.resolve().is_relative_to(workdir):
                            raise RebuildError("A reconstruction asset escaped its owned work directory.")
                        with value.open("rb") as source:
                            value = source.read(MAX_DOCUMENT_BYTES + 1)
                        if len(value) > MAX_DOCUMENT_BYTES:
                            raise RebuildError("A reconstruction asset exceeded 32 MiB.")
                        assets[name] = value
                    if not isinstance(value, bytes):
                        raise RebuildError("The reconstruction collector returned an invalid asset.")
                    total += len(value)
                    if total > MAX_BUNDLE_BYTES:
                        raise RebuildError("The reconstruction bundle exceeded 128 MiB.")
            except BaseException as exc:
                detail = client.diagnostic() if client is not None else ""
                # Reap before reading files: cancellation may have interrupted
                # an export.  Its manifest explicitly marks unconfirmed output.
                if client is not None:
                    client.close()
                status = "failed"
                error = exc
                if cancel.is_set() or isinstance(exc, JobCancelled):
                    status, error = "cancelled", JobCancelled("Reconstruction was cancelled.")
                elif time.monotonic() >= deadline or isinstance(exc, JobTimedOut):
                    status, error = "timed_out", JobTimedOut("Reconstruction exceeded its time limit.")
                elif detail:
                    error = RebuildError(f"{_safe_error(exc)}\nCLI: {detail}")
                try:
                    partial = _partial_assets(workdir, client, document_hash)
                    self.archive.commit_job_partial(job_id, partial, _safe_error(error), status)
                except Exception as storage_error:
                    self.last_error = _safe_error(storage_error)
                    if status == "failed":
                        error = RebuildError(f"{_safe_error(error)}; partial data could not be saved: {_safe_error(storage_error)}")
                raise error from exc
            finally:
                if client is not None:
                    client.close()
            # A published completed stage has no surviving CLI child or
            # temporary directory; owned content is now bounded byte strings.
            temporary.cleanup()
            with self._condition:
                _check(cancel, deadline)
                self.archive.update_job(job_id, "running", cli_version=summary["cli_version"])
                self.archive.commit_stage(job_id, assets, summary)
