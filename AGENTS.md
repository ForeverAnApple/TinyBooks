# Audiobooks workspace

A staging area for converting audiobook source material into m4b files using
`make_m4b.py` (in this directory).

## Layout

```
.
├── make_m4b.py          # the converter — read its docstring before changing it
├── AGENTS.md            # this file
├── CLAUDE.md            # symlink → AGENTS.md
├── processed/           # finished outputs: .m4b files with embedded covers + chapters
├── raw/                 # source material: per-book directories of mp3s, plus the
│                        # occasional loose source mp3 (e.g. Accelerando)
├── AudioBookConverter/  # third-party tool (separate git repo). NOT used by
│                        # make_m4b.py — leave alone unless explicitly asked.
└── __pycache__/         # leftover from running make_m4b.py — safe to delete
```

## Conventions

- **New source material** (mp3 directory, single mp3 + cue, lone mp3) goes in
  `raw/<Book Name>/` (or `raw/<Book>.mp3` for a single loose file).
- **Finished m4b output** belongs in `processed/`. `make_m4b.py` does not move
  files there automatically — its `--output` defaults to `<dir_name>.m4b` in
  the *current* working directory, so either `cd processed/` first or pass
  `--output processed/<name>.m4b`.
- Some `raw/` book folders also contain a finished `.m4b` (sometimes shipped
  with the torrent, sometimes from a previous run). That's fine — leave them
  in place; only loose root-level outputs from `make_m4b.py` get moved to
  `processed/`.
- Naming: keep the source folder name the user/torrent gave it. The script
  uses `directory.name` as the default title, so don't rename folders unless
  asked.

## Running the converter

`make_m4b.py` auto-detects three source layouts (no flag needed) — see its
top-of-file docstring for the full rationale. Quick reference:

```sh
# Multi-file (N mp3s, each becomes a chapter):
python3 make_m4b.py "raw/Blindsight" --output "processed/Blindsight.m4b"

# Single mp3 + cue sheet (cue defines chapters; encodes in parallel):
python3 make_m4b.py "raw/Dennis E. Taylor - The Singularity Trap.MP3" \
  --output "processed/Singularity Trap.m4b"

# Single mp3, no cue (one big chapter, serial — slow):
python3 make_m4b.py "raw/Charles Stross  -  Accelerando.mp3" \
  --output "processed/Accelerando.m4b"
# ^ won't work as written: the script wants a *directory*. For a loose mp3,
#   put it in its own folder under raw/ first.
```

Optional flags: `--title`, `--author`, `--cover <path>`, `--jobs <N>`.

Cover art priority: `--cover` > sidecar image in source dir
(`cover.*`/`folder.*`/`front.*` or the largest `.jpg`/`.png`/`.webp`) >
embedded ID3 art on the first audio file.

### Sourcing cover art

If the source dir's sidecar is small or missing, fetch a clean hi-res cover and
save it as `cover.jpg` in the source dir before encoding. Goal: hi-res square
art with no retailer branding.

**Never use a cover with retailer branding** — no "Audible Original" banners,
"Only from Audible" corner ribbons, "AUDIOBOOK / MP3 AUDIO" frames, "Apple
Books" overlays, etc.

Two paths, depending on what's available:

1. **Clean source exists** — try in order: publisher page, Amazon print/Kindle
   ASIN (not the audio ASIN), OpenLibrary (`covers.openlibrary.org/b/isbn/<ISBN>-L.jpg`,
   capped ~333×500), ISFDB wiki cover scans, artist's portfolio.
2. **Only branded hi-res exists** (typical for Audible exclusives) — fetch the
   2400×2400 master via the iTunes Lookup API
   (`https://itunes.apple.com/lookup?id=<APPLE_BOOKS_ID>&entity=audiobook`,
   replace `100x100bb.jpg` in the artwork URL with `3000x3000bb.jpg`), then run
   the **`debrand-audiobook-cover`** skill (`.claude/skills/debrand-audiobook-cover/SKILL.md`)
   to strip the branding via Codex's built-in `image_gen` tool.

The m4b accepts any aspect ratio, so a clean rectangular print cover beats a
branded square one if de-branding isn't an option.

## Requirements

- `ffmpeg` and `ffprobe` on PATH (the script shells out to both).
- Python 3 with stdlib only (no third-party deps).

## Editing `make_m4b.py`

The docstring at the top documents non-obvious correctness constraints —
**read it before changing encoding behavior**. Highlights:

- `-ss`/`-to` go *before* `-i` (demuxer-level seek). Putting them after `-i`
  makes each parallel worker decode-and-discard up to its chapter start.
- Mid-chapter splits with `-c:a copy` concat inject ~47 ms of silence per seam
  (AAC encoder priming). The script only splits at cue boundaries for that
  reason. Don't add a "just split the big mp3 in N pieces" parallel path for
  the no-cue case.
- Chapter timestamps are built from the *encoded* durations, not source
  durations, because AAC priming shifts them.
- The encode pass strips embedded art (`-vn`) on purpose so the final concat
  sees stream-compatible inputs; cover art is re-attached in the mux pass.

## What not to do

- Don't move `make_m4b.py` out of this directory.
- Don't recurse into `AudioBookConverter/` for codebase questions — it's an
  unrelated upstream project that happens to live here.
- Don't bulk-delete files in `raw/` after producing an m4b without explicit
  confirmation — the user may want to re-run with different settings.
- Don't rename source directories to "tidy them up" unless asked — naming is
  load-bearing for the script's default title.
