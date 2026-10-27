# FastRawPhotoView — NAS RAW browser, foundation increment

Approved product direction, unchanged. This revision is the first
implementation increment of the agreed application; it is not a reduced
substitute for it. If the Worker image cannot provide the toolchains below,
the correct outcome is a provisioning prerequisite — not a smaller product.

## Product

A cross-platform desktop application (Windows, Linux, macOS) that browses RAW
photographs stored on a NAS and imports selected frames to local disk.

- **Transports:** SMB and FTP. Both are first-class; neither is a later phase.
- **RAW formats:** Nikon NEF first, behind a manufacturer-extensible decoder
  interface so Canon CR3, Sony ARW and others are added without changing the
  browser or the cache.
- **Deployment:** client-only. The application must be fully usable with no
  server-side component. A Raspberry Pi 4 NAS-side helper remains an option to
  be discussed later and must not be assumed by any interface here.
- **Commercial use is intended.** Every third-party dependency added must have
  its licence read from authoritative package metadata and recorded. Do not
  assume a licence from a project's reputation, and do not add a dependency
  whose licence has not been checked.

## Stack

- **Tauri + Rust core**, web front end. This is the approved direction.
- Rust owns transport, RAW parsing, the thumbnail cache and all file I/O.
- The web layer owns presentation only. It must never talk to SMB or FTP.

## Agreed interface

Three panes, dark theme tuned for looking at photographs:

- **Left** — connection list and folder tree. A connection is a saved SMB or
  FTP endpoint; the tree is lazy and must stay responsive on directories with
  thousands of entries.
- **Centre** — virtualised thumbnail grid. Only visible cells decode. Scrolling
  a large directory must not decode the whole directory.
- **Right** — metadata and import panel: capture settings from EXIF, the
  current rating/pick/reject state, and the import target.

Also required in this increment:

- Single-image preview from the grid.
- Rating (0–5), pick and reject flags, persisted locally per file identity.
- Filtered import: import the current selection, or everything matching the
  active rating/pick/reject filter, to a chosen local directory.

## Scope of this increment

1. `src-tauri/` Rust workspace with the Tauri application shell.
2. Transport layer with one trait covering SMB and FTP: list a directory, stat
   an entry, read a byte range. Byte-range reads matter — a NEF is large and
   the embedded preview is near the front of the file.
3. NEF reader: parse the TIFF/EXIF container, locate the largest embedded
   JPEG preview, extract it, and read orientation plus the capture metadata
   shown in the right pane. Full raw demosaicing is explicitly out of scope for
   this revision; the embedded preview is what the grid displays.
4. Decoder registry keyed by extension and magic bytes, so a second
   manufacturer is a new module and a registration, not an edit to the grid.
5. Thumbnail cache on local disk, keyed by a content identity that survives a
   NAS remount (path plus size plus mtime), with a bounded size.
6. Front-end shell implementing the three-pane layout, the virtualised grid,
   preview, rating controls and the import panel.
7. Import: copy selected originals to a local directory, verify the copied
   size, and never modify anything on the NAS. The application must have no
   write path to the source at all.
8. `LICENSES.md` recording every dependency, its licence and the metadata
   source the licence was read from.

## Out of scope for this revision

Full RAW demosaicing and colour management; a NAS-side helper; catalogue
database beyond the local rating store; non-Nikon decoders (the registry must
accept them, but none ship here); cloud or network sync.

## Acceptance

Deterministic checks, evaluated by the supervisor:

- `src-tauri/Cargo.toml` exists and the workspace builds.
- `src-tauri/src/raw/nef.rs` contains an embedded-preview extraction entry
  point.
- `src-tauri/src/transport/mod.rs` contains one trait implemented by both the
  SMB and the FTP backends.
- `LICENSES.md` exists and names every added dependency.
- The registered test command passes, covering: NEF preview extraction against
  a committed synthetic fixture, the transport trait against an in-process fake
  endpoint, cache key stability across a simulated remount, and the import
  path's refusal to write to a source location.
- All changes stay inside the registered write scope.

## Prerequisites

This increment needs `rustc`, `cargo`, `node` and `npm` in the Worker image, in
addition to `python3` and `git`. The image measured for this deployment
provides `python3` and `git` only, and the Worker container runs with no
network. Until the image provides the Rust and Node toolchains, this revision
cannot be built or tested, and the handoff route must report that as a
provisioning prerequisite rather than dispatching a job that cannot succeed.
