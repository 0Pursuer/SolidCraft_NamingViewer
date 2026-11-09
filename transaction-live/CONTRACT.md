# Internal module contract

Python 3.10+, standard library only. Work in this staging directory; final delivery is E:\solidcraft\transaction-live. Do not modify topology-live. Tokens must never enter archives, URLs, error text or browser data. Public native observations and inferred changes must remain distinguishable.

## Archive API (`archive.py`)

- `Archive(root: Path, limit_bytes=2*1024**3, read_only=False)`, `close()`.
- Exceptions `ArchiveError`, `QuotaExceeded`.
- `new_session(info: dict) -> str` (only safe instance metadata; no credentials).
- `add_event(session_id, document_id, revision, kind, raw=None, status='pending', message=None) -> int`.
- `set_event_status(event_id, status, message=None)`.
- `commit_snapshot(event_id, assets: dict[str, bytes|Path], summary: dict) -> dict`.
- `snapshot(snapshot_id) -> dict|None`, `latest_snapshot(document_id=None) -> dict|None`.
- `list_events(after=0, limit=100, session_id=None) -> list[dict]`, `list_sessions() -> list[dict]`.
- `read_asset(sha256) -> bytes`, `asset_path(sha256) -> Path`, `status() -> dict`.
- `add_job(snapshot_id, position, version) -> str`, `update_job(job_id, status, **fields)`, `get_job(job_id) -> dict|None`, `list_jobs() -> list[dict]`.
- `commit_stage(job_id, assets, summary) -> dict`, `stage(stage_id) -> dict|None`.
- `export_zip(destination: Path) -> Path` creates a consistent archive without including unrelated files; caller owns destination.

Snapshot IDs equal their event ID, label O0001. Stage IDs are a safe opaque string; label O0001/S2. Snapshot/stage return `{id,label,summary,assets,diff,inference,...}`; `assets` maps names to `{sha256,size}`. Missing records return None. Event rows include id,label,session_id,document_id,revision,kind,status,message,observed_at,snapshot_id. Geometry data is stored as `geometry.json`; raw document as `document.json`, inspect as `inspect.json`, sketches as `sketches.json`, topology as `topology.json`, tagged mesh as `model.3mf`. All raw JSON blobs are UTF-8 and immutable. Summary includes document_hash,document_id,revision,name,undo,redo,timeline,params,version,warnings,source ('observed' or 'rebuilt'), completeness. Diff is a bounded list of `{path,kind,old,new}` with stable feature IDs. Inference always explicitly marked inferred, with evidence and optional linked prior snapshot. Archive must be thread-safe and publish assets before database references atomically. Quota failures preserve older data. Recover interrupted pending jobs/captures visibly.

## Capture API (`capture.py`, root-owned)

`collect_bundle(client, workdir: Path, version: str, expected_key=None, source='observed') -> (assets,summary)`.
Client has `request(method,params=None,timeout=...)`, `query(command,params,timeout=...)`, `export(destination: Path, format: str, timeout=...)`. Live mode uses document.revision around the full collection; offline mode has expected_key=None and source='rebuilt'. `SourceChanged`, `CaptureLimit` exceptions. Return assets as bytes/Paths; caller commits before deleting workdir. Never save/roll live document. Geometry comes from local topology_geometry.py and original native names/records remain intact. Partial geometry is represented by warnings and completeness, not invented geometry.

## Rebuilder API (`rebuild.py`)

`Rebuilder(archive, cli_path: Path, version: str, timeout=120)`, `submit(snapshot_id:int,position:int) -> dict job`, `cancel(job_id)`, `stop()`.
Use isolated CLI only, never --connect. Single worker, bounded queue, CREATE_NO_WINDOW on Windows. Fresh process per job, cancellation/timeout reaps only owned child. May use MCP stdio with `mcp --headless --in derived.json --allow-path <job root>` and the shared collect_bundle API, or a bounded CLI-run workflow. Stage marker is prefix count, not feature ID. Stage records must identify input document hash and CLI version, with explicit external dependency/version/incomplete warnings. Never fabricate internal ModelState or copy observer undo/redo depths into reconstructed history; no original native state object is available. All source JSON immutable; edit only derived copy. For unresolved external links fail or mark incomplete, never silently resolve relative paths against capture folder. Jobs return failed/cancelled/timed_out states and preserve diagnostics.

## HTTP/UI API (`server.py` root; `viewer.html`/`geometry.html` UI agent)

- GET `/api/status`: `{recording,status,error,archive,instance,version,session_id}`.
- GET `/api/sessions`: sessions array; GET `/api/events?after=0&limit=100&session_id=...`: `{events:[...]}`.
- GET `/api/observations/<id>` snapshot record; GET `/api/stages/<id>` stage record.
- GET `/api/assets/<sha>` immutable asset bytes with JSON MIME where applicable, authenticated.
- GET `/api/jobs`: `{jobs:[...]}`; GET `/api/jobs/<id>` job.
- POST `/api/capture` `{}`; POST `/api/recording` `{paused:bool}`; POST `/api/rebuild` `{snapshot_id:int,position:int}` -> job; POST `/api/jobs/<id>/cancel` `{}`.
- GET `/api/archive.zip` consistent downloadable archive.
- `/` serves viewer.html; `/geometry.html` serves the reusable renderer frame.

API errors `{error: string}` appropriate status. Same-origin credentials/cookie; POST JSON exact key validation and origin checks. No generic native command or filesystem route. UI should show native/inferred/rebuilt/unavailable badges, raw JSON/downloads, differences, observed stack depths, incomplete captures, and position↔feature ID mapping. `geometry.html` receives same-origin parent `postMessage({type:'trace-geometry',data:<geometry.json>, sketches:<sketches JSON>,label})`, responds `{type:'trace-geometry-ready'}`. Geometry is `{meta:{...},stages:[<existing topology body stage>]}`. Empty body stages show available sketch linework or explicit no-entity state. UI never claims actual ModelState/stack contents. Do not interpolate raw data into executable HTML.
