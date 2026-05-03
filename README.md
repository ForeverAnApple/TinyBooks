# TinyBooks

A small, dependency-free Python script that turns a folder of mp3 audiobook
sources into a single, properly-chaptered `.m4b` with cover art — fast,
because every chapter encodes in parallel.

`make_m4b.py` is the whole tool. It auto-detects the layout of your source
material, picks the right encode strategy, and produces an m4b that plays
correctly in Apple Books, Plex, and friends.

## Features

- **Three source layouts, one command** — auto-detected, no flag needed:
  - **Multi-file** — N mp3s in a folder, each becomes a chapter.
  - **Single mp3 + cue sheet** — chapter starts come from `INDEX 01`
    timestamps; each chapter is encoded in parallel via demuxer-level seek
    (`-ss` before `-i`), so a 12-hour book finishes in ~60 s on 16 cores.
  - **Single mp3, no cue** — falls back to one big chapter (serial — see
    "Why no parallel split here?" below).
- **Parallel AAC encoding** scaled to your CPU count (`--jobs N` to override).
- **Cover art** picked in priority order: `--cover` flag → sidecar image
  (`cover.*` / `folder.*` / `front.*` / largest jpeg) → embedded ID3 art
  on the first source file.
- **Bitrate / sample-rate matching** — uniform sources keep their layout
  (no wasteful upsampling of mono voice into stereo); mixed sources
  normalize to 44.1 kHz / stereo / max-source bitrate so the final
  `-c:a copy` concat sees stream-compatible inputs.
- **Live progress** — every ffmpeg encoder is invoked with
  `-progress pipe:1`; intra-chapter `out_time_us` events stream back to a
  shared aggregator so the bar moves smoothly even with one giant chapter.
  TTY mode does `\r`-overwrite at 5 Hz; non-TTY mode emits one self-contained
  line every couple of seconds — `tail -3` of a log gives you phase, audio
  done/total, elapsed, and ETA.

## Requirements

- `ffmpeg` and `ffprobe` on `PATH`.
- Python 3 (standard library only — no `pip install` needed).

## Usage

```sh
# Multi-file (N mp3s, each becomes a chapter):
python3 make_m4b.py "raw/Blindsight" --output "processed/Blindsight.m4b"

# Single mp3 + cue sheet (cue defines chapters; encodes in parallel):
python3 make_m4b.py "raw/Singularity Trap" \
  --output "processed/Singularity Trap.m4b"

# Single mp3, no cue (one big chapter, serial — slow):
# Drop the loose mp3 in its own folder under raw/ first; the script
# wants a directory.
python3 make_m4b.py "raw/Accelerando" \
  --output "processed/Accelerando.m4b"
```

### Retag mode

Pass an existing `.m4b` instead of a directory and the audio + chapters
pass through via `-c copy`; only the format-level metadata + cover are
rewritten. Useful for Audible AAX rips with messy tags or branded covers.

```sh
python3 make_m4b.py "raw/Excession/Culture Book 5 - Excession.m4b" \
  --output "processed/Excession - Culture, Book 5.m4b" \
  --title "Excession" --series Culture --series-part 5
```

Resolution order per field: explicit `--flag` > sidecar `.nfo` (KAZIN
release-info format auto-detected) > tag baked into the input m4b >
fallback.

### Flags

- `--title`, `--author`, `--narrator`, `--year`, `--genre`
- `--description "..."` or `--description-file path/to/blurb.txt`
- `--series <name>` + `--series-part <N>` — emits the de-facto MP4
  audiobook-series triple (`album = "<title>: <series>, Book <N>"`,
  `album_artist = <author>`, `grouping = "<series> #<N>"`) that
  Audiobookshelf and most players read.
- `--cover <path>` — sidecar image fallback if omitted.
- `--jobs <N>` — parallel encoders (default: cpu count).

## Sample output

```
📐 Layout: multi-file (one chapter per mp3)
📖 Chapters: 5
🔎 Probing 5 file(s) (3 workers)...
   inputs uniform: 44100 Hz, 2 ch — matching source
   inputs uniform: 64k — matching source
🎧 Encoding 5 chapter(s) to AAC (3 workers)...
   encode [█████████░░░░░░░░░░░░░░░]  40%  2/5 ch  audio 2:00/5:00  elapsed 0:02  eta 0:03
   encode [████████████████████████] 100%  5/5 ch  audio 5:00/5:00  elapsed 0:04  eta 0:00
🔎 Measuring encoded durations...
🎬 Muxing: Multi...
   mux    [████████████████████████] 100%  audio 5:00/5:00  elapsed 0:01  eta 0:00
✅ Success! Created: processed/Multi.m4b
```

## Why no parallel split here?

The single-file-no-cue case isn't parallelized on purpose. Splitting an
mp3 mid-audio and concatenating the AAC outputs with `-c:a copy` injects
encoder-priming silence at every seam — 2112 samples for AAC-LC, which
is ~48 ms at 44.1 kHz but ~96 ms at 22 kHz (the sample rate Audible AAX
rips actually use). Not fixable without re-encoding the seams. Cue
boundaries land at natural pauses where that imprecision is inaudible,
but a midpoint split in the middle of a sentence is not. So when
there's no cue, the script does one serial encode to keep the audio
clean.

If you're regularly hitting this path on long books, the right fix is to
generate a cue sheet (e.g. via silence detection) rather than to split
blindly.

## Layout of this repo

```
.
├── make_m4b.py    # the converter (read its docstring for deep correctness notes)
├── AGENTS.md      # workspace conventions for the audiobook directory
├── CLAUDE.md      # → AGENTS.md (symlink, for Claude Code)
└── .gitignore     # raw/, processed/, AudioBookConverter/ stay local
```

`raw/` (sources) and `processed/` (m4b outputs) are deliberately not
tracked — TinyBooks is the converter, not your audio library.

## Notes for hackers

The top of `make_m4b.py` documents the non-obvious correctness constraints
in detail — read it before changing encode behavior. Highlights:

- `-ss` / `-to` go *before* `-i` (demuxer-level seek). Putting them after
  `-i` makes each parallel worker decode-and-discard up to its chapter
  start.
- Chapter timestamps in the metadata are built from the *encoded*
  durations, not source durations, because AAC priming shifts them.
- The encode pass strips embedded art (`-vn`) so the final concat sees
  stream-compatible inputs; cover art is re-attached in the mux pass.
