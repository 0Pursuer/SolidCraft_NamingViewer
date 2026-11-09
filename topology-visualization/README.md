# SolidCraft topology naming explorer

Open `solidcraft-topology.html` in a modern desktop browser. It is a standalone,
offline file: the geometry, names and renderer are embedded, and no server or
external library is required.

The viewer contains three snapshots replayed with the installed SolidCraft CLI
0.30.1 from the open design's `timeline.export_script` output:

| Stage | Features | Faces | Edges | Display endpoints | Volume (mm³) |
| --- | --- | ---: | ---: | ---: | ---: |
| Before split | Sketch1, Extrude1 | 6 | 12 | 8 | 80940.0 |
| After split | + Sketch2, Extrude2 | 8 | 18 | 12 | 72497.12134294157 |
| After chamfer | + Chamfer1 | 9 | 21 | 14 | 72482.16369745867 |

The open desktop model was not rolled back or edited to obtain these snapshots.
The final replay's face and edge names match the live app's queried names.

## Meaning of the labels

- Face and edge names are real values queried from SolidCraft with `model.faces`
  and `model.edges`.
- `F0`, `E0`, etc. in the viewer are short display labels. The details show the
  original complete persistent name.
- `P0`, `P1`, etc. are local display point IDs created by deduplicating edge
  endpoint coordinates. They are **not** SolidCraft persistent vertex names or
  kernel vertex indices, and must not be compared across snapshots as identities.
  Coincident but topologically distinct vertices could be merged by this display
  convention. SolidCraft 0.30.1 does not expose persistent vertex naming here.
- Face tessellation comes from `3mf-tagged` exports. Every triangle carries its
  original B-rep face index. Displayed edge polylines are recovered from boundaries
  between those tagged faces, so curved edges are tessellated approximations.
- The final sloping face is named after the actual input edge used by the chamfer.
  The feature's stored older edge reference remains unchanged by warning recovery.

## Key references

Original input reference: `F2:side:l3|F2:top`

Split pieces:

- `F2:side:l3|F2:top#[F2:side:l4]`
- `F2:side:l3|F2:top#[F2:side:l2]` (the piece used by this chamfer)

Generated chamfer face:
`F6:blend:F2:side:l3|F2:top#[F2:side:l2]`

## Reproducibility

`chamfer-script.json` contains the complete example; the other `*-script.json`
files contain its earlier prefixes. `capture-stage.ps1` replays one prefix in a separate
headless MCP session and captures names, measurements and a tagged mesh.
`build-data.py` joins these results into `topology-data.json` and checks that each
queried edge corresponds to a mesh boundary between the expected named faces.
`data-validation.json` records the snapshot counts and edge-length checks.

## Source

- https://github.com/tangency-labs/solidcraft/blob/v0.30.1/docs/book/src/dev/naming.md
- https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/naming.rs
- https://github.com/tangency-labs/solidcraft/blob/v0.30.1/crates/doc/src/persistent_name.rs
- https://github.com/tangency-labs/solidcraft/blob/v0.30.1/docs/formats/3mf-face-tags.md
