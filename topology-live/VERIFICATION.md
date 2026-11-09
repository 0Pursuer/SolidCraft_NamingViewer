# Verification record

Date: 2026-11-08 (Asia/Shanghai). Installed SolidCraft: 0.30.1.

- `python -m unittest -v test_live.py test_topology_geometry.py`: 21 checks passed.
  Covers automatic revision-following worker, changed-during-capture discard,
  document switching, request allowlists, HTTP origin/Host/session boundaries,
  unknown file/write routes, resource bounds and idempotent connection teardown.
- The three earlier CAD exports reproduce their real face/edge/endpoint counts.
- Browser fixture checks: 14 passed, including selection identity across changed
  indices, point references, empty/new documents, multiple bodies, pause/resume,
  untrusted model names and mobile layout. Renderer face-index caching was checked
  again after its change.
- Final real-app/browser integration: connected to active Untitled/Body1,
  document revision 2, 8 faces, 18 edges, 12 display endpoints, 1784 display triangles.
  Edge directory, point reference object, labels and pause/resume worked; no
  JavaScript runtime error was observed. This snapshot is dated evidence, not a
  permanent assertion about the user's currently active document.
- Real CAD modification was not used as a test operation. Automatic update and
  mid-capture changes were exercised against isolated replay data; final installed
  CAD connectivity and rendering were exercised against the actual running app.
- Independent review findings addressed: atomic socket ownership during close,
  bounded rendering work, and per-port session cookie isolation. The reviewer
  rechecked those changes and reported no remaining blocking finding.

Operational limits: committed/recomputed B-rep only, separate browser viewport,
software rendering budget 8 million work units per body, 32 bodies, 16 MiB response
data. Export/measurement cost depends on model complexity. No claim is made that
arbitrary-size models update at a fixed frame rate.

## Multi-body and refresh regression fix

The first live viewer retained a selected Body1 and rendered only that body even
after a second body arrived. The refresh button also only read the cached state.
Both behaviors were reproduced from the user's live page and the native/API data.

- The default view now combines all visible bodies in CAD coordinates and fits
  their union bounds. Per-body filtering remains explicit. Aggregate render indices
  preserve original body and query-index provenance; display points are not merged
  across bodies.
- Manual refresh uses an authenticated, parameter-free POST request to the one
  collector worker. Same-revision captures are supported, pending clicks coalesce,
  and the UI waits for the requested capture to complete.
- Backend/geometry regression checks: 23 passed. New checks prove same-revision
  re-capture and authenticated/coalesced refresh requests.
- Pure JavaScript checks cover the real two-body union (12 faces, 24 edges,
  16 display points), valid remapped adjacency, preserved origin indices,
  cross-body coincident endpoints, paused manual refresh, queued clicks and
  one-time session renewal.
- The existing user browser tab was reloaded and visually verified with both the
  block and new left-hand cylinder visible. Clicking the real refresh button
  showed capture progress, then completion; generation advanced 1 to 2 while
  native document revision stayed 3. Proof: `live-all-bodies-fixed.png`.
- No CAD model edits were used in this regression repair or its verification.

## Curved boundaries and drag direction

The Sample Plate snapshot reproduced 12 hole half-arcs falling back to three
query points, which made pairs of arcs look like square or diamond outlines.
The tagged export contained the correct curved paths, but a strict length gate
rejected them because the native query uses a coarser sampled polyline. Boundary
matching still requires the same adjacent faces, connected paths and matching
endpoints; sampled length now ranks those candidates without rejecting the
otherwise matching finer curve.

- The captured Sample Plate fixture fails the new 12-arc regression before the
  fix. After the fix, all 12 use tagged mesh curves with more than 20 points,
  and their points lie on the expected cylinders within 1e-5 mm.
- `python -m unittest -v test_live.py test_topology_geometry.py`: 24 passed.
- `node test_rotation.js`: 13 projected rotation/pan checks passed. The checks
  exercise the generated pointer/keyboard handlers and projection in isometric,
  front and back views. Horizontal yaw and left/right keyboard direction were
  corrected; vertical rotation and Shift panning remain covered.
- The existing browser tab was reloaded against the updated service and actual
  Sample Plate snapshot (19 kernel faces, 48 edges, 32 display endpoints).
  Circular hole outlines and pointer rotation were checked in that tab.
  Proof: `live-curves-fixed.png`.

The cylinder wall's two differently named halves are native topology, not this
rendering defect. Native face queries report `F7:side:c1#1` and `F7:side:c1#2`;
the native UI selection contains both indices 0 and 1. SolidCraft v0.30.1
`kernel/src/topo.rs` documents the cylinder split, and native `surface_patch`
selection groups seam-connected pieces of the same analytic surface. The viewer
retains each raw face so both native names can be inspected. These two faces
currently report `exact: false`; numeric suffix stability is not guaranteed.

## Per-user startup and fixed instance selection

The old client was reproduced switching from fixture A to B after `current.port`
changed, and adopting a replacement launch on the same port. Both behavioral
checks failed before this change and pass with explicit startup selection.

- Default HTTP port is now 0, bound directly by the OS, with no probe-and-release
  race. Fixed ports remain optional. Windows listeners use exclusive address
  ownership; concurrent listener and occupied-port checks passed.
- Discovery scans only the selected/current-user control directory, authenticates
  bounded candidates, and presents document title, PID and native port. Startup
  prompts for selection; `--control-port` is the noninteractive alternative.
- Each client retains its selected launch's token, available PID and credential
  file identity. Reconnect validates both before and after authentication and
  never follows `current.port`. Native credentials stay out of URLs and labels.
- Per-process mesh files now live in the launch user's TEMP directory. The
  export boundary permits only that session's one `snapshot.3mf` path.
- `python -X utf8 -m unittest -v test_startup test_instances test_live
  test_topology_geometry`: 47 tests passed. New coverage includes retained
  snapshots after identity loss and replacement during connection authentication.
- `node test_rotation.js`: 13 projected rotation/pan checks passed;
  `node --check live-ui-syntax.js` passed after regenerating the frontend.
- The real CMD launcher passed `--help` and discovered an isolated installed
  SolidCraft 0.30.1 instance using `--list-instances`.
- Two real viewer processes concurrently received HTTP ports 50321 and 50316,
  both bound to the test app's native port 27901 / PID 46352. Browser status and
  a real manual refresh showed the pinned port/PID. Proof:
  `launch-binding-verified.png` (an intentionally empty test document).
- The owned test app was terminated and relaunched at native port 27901 with
  PID 74844. Both viewers retained the original document/instance metadata and
  reported identity change instead of adopting the replacement. Sanitized API
  evidence: `verification-startup.json`. Test viewers and owned native processes
  were stopped after verification; the other users' CAD processes were untouched.
- An independent focused lifecycle/concurrency/security review found no blocking
  issue; its suggested authentication-race regression was added and passed.

Different Windows logins were not created or switched during this test. Profile
directory selection was checked with isolated directory fixtures. HTTP remains a
loopback service without Windows-user authentication; separate ports isolate
viewer state but do not provide model confidentiality between local users.

## Native naming provenance and display integrity

The original UI's "geometry naming / unverified" label conflated SolidCraft's
`exact` provenance flag with whether this viewer extracted a name successfully.
Native v0.30.1 `model.faces` returns both fields independently. `exact: false`
can describe native geometry fallback naming or an unverified piece ordering.
Neither value promises that arbitrary later edits preserve a stored name.

- Raw face and edge query records are retained for inspection. Independent copies
  are checked after adaptation and immediately before publishing: native index
  set, names, exact field presence/type/value, complete raw records, body and query
  provenance must agree. A mismatch rejects the new snapshot and retains the old.
- Native face tags must cover exactly the queried faces. Edge display has no
  native export edge index, so it remains explicitly identified as reconstruction.
  Endpoint/midpoint/length evidence must identify one path, and different native
  edges cannot claim the same path. Ambiguity, conflicts and exceeded budgets hide
  the named curve, label and picking target instead of drawing a three-point guess.
- The UI now distinguishes native name provenance, native exact status, and display
  mapping. Missing exact is never shown as true. Raw records and adapter warnings
  are visible. Unverified face/edge/display-point selections do not follow a rebuild
  solely because a name happens to be the same; accepted snapshot identity controls
  that decision even after a rejected response.
- Regression evidence includes RED reproductions for name/exact modification,
  ambiguous arc selection, duplicate/reversed/phase-shifted edge paths, and a
  rejected response incorrectly preserving an unverified selection.
- Final Python suite: 69 passed (`test_startup`, `test_instances`, `test_live`,
  `test_topology_geometry`, `test_name_integrity`). Generated JavaScript syntax,
  native-name UI checks, 4 selection-integrity checks, and 13 rotation checks passed.
- Direct live comparison used the user's supplied viewer at port 51046 and its
  selected native app at control port 27879 / PID 44044. Document
  `5ea0b27496c02459-3`, revision 2, Bolted Flange / Flange: all 21 face names,
  54 edge names, original indices and exact fields matched the native queries.
  Face 4 was natively `F2:side:c1#1`, `exact: false`; 9 of 21 faces reported false.
- A fresh native tagged export and final deployed capture passed name and mapping
  checks with 0 unresolved edges. Evidence: `current-flange-native.json`,
  `flange-live-audit/audited-snapshot.json`, `flange-live-audit/deployed-state.json`.
- Viewer port 51046 was updated in the same Windows account, retaining the selected
  native app. Chrome's existing page was reloaded; face 4's native name, history
  flag, native-face mapping and expanded original query were verified visually.
  Proof: `native-name-audit-verified.png`. No CAD features were modified.
- Independent review's cross-edge ownership and rejected-snapshot selection
  findings were both fixed and covered by regression checks.

The result verifies current native records and conservative display association;
it does not certify SolidCraft's own naming algorithm for all future topology
changes, or turn reconstructed edge polylines into native edge-index-tagged data.
