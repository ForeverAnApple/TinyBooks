# Audiobooks workspace

A staging area for converting audiobook source material into m4b files using
`make_m4b.py` (in this directory).

## Layout

```
.
├── make_m4b.py          # the converter — read its docstring before changing it
├── AGENTS.md            # this file
├── CLAUDE.md            # symlink → AGENTS.md
├── processed/           # finished outputs: .m4b files (fragmented MP4) + sidecar
│                        # <basename>.jpg cover next to each m4b
├── raw/                 # source material: per-book directories of mp3s, plus the
│                        # occasional loose source mp3 (e.g. Accelerando)
├── AudioBookConverter/  # third-party tool (separate git repo). NOT used by
│                        # make_m4b.py — leave alone unless explicitly asked.
└── __pycache__/         # leftover from running make_m4b.py — safe to delete
```

## Conventions

- **New source material** (mp3 directory, nested disc folders, single mp3 + cue,
  lone mp3) goes in `raw/<Book Name>/` or as `raw/<Book>.mp3` for a single
  loose file. `make_m4b.py` accepts both directory and direct `.mp3` inputs.
- **Finished m4b output** belongs in `processed/`. `make_m4b.py` does not move
  files there automatically — its `--output` defaults to `<dir_name>.m4b` in
  the *current* working directory, so either `cd processed/` first or pass
  `--output processed/<name>.m4b`.
- Existing output files are protected by default. Pass `--force` only when you
  intentionally want to replace the target; the script writes to a temporary
  sibling and moves it into place after ffmpeg succeeds.
- Some `raw/` book folders also contain a finished `.m4b` (sometimes shipped
  with the torrent, sometimes from a previous run). That's fine — leave them
  in place; only loose root-level outputs from `make_m4b.py` get moved to
  `processed/`.
- Naming: keep the source folder name the user/torrent gave it. The script
  uses `directory.name` as the default title, so don't rename folders unless
  asked.

### Output filename convention (`processed/`)

Files in `processed/` follow this convention — the source folder name is
typically not clean enough, so always pass `--output` rather than relying on
the directory-name default.

- **Standalone book**: `<Title>.m4b`
  - e.g. `Neuromancer.m4b`, `Atlas Shrugged.m4b`, `Accelerando.m4b`
- **Series book**: `<Title> - <Series>, Book <N>.m4b`
  - e.g. `All These Worlds - Bobiverse, Book 3.m4b`,
    `Consider Phlebas - Culture, Book 1.m4b`

Rules:
- Use the canonical book title (publisher's title), not the torrent name.
- No author prefix in the filename — author lives in the m4b metadata.
- No format/source cruft (`- MP3`, `(Unabridged)`, `[BT]`, etc.).
- Keep the `.m4b` extension lowercase.
- Don't rename existing files in `processed/` to match this convention without
  being asked — the rule applies to *new* outputs.

## Running the converter

`make_m4b.py` auto-detects three source layouts (no flag needed) — see its
top-of-file docstring for the full rationale. Quick reference:

These examples show source/output shape. New files for `processed/` also need
complete metadata from a sidecar `.nfo` or explicit flags; the script refuses
missing required fields unless `--allow-missing-metadata` is passed for a
scratch/test build.

```sh
# Multi-file (N mp3s, each becomes a chapter):
python3 make_m4b.py "raw/Blindsight" --output "processed/Blindsight.m4b"

# Single mp3 + cue sheet (cue defines chapters; encodes in parallel):
python3 make_m4b.py "raw/Dennis E. Taylor - The Singularity Trap.MP3" \
  --output "processed/Singularity Trap.m4b"

# Single mp3, no cue (one big chapter, serial — slow):
python3 make_m4b.py "raw/Charles Stross  -  Accelerando.mp3" \
  --output "processed/Accelerando.m4b"
```

Optional flags: `--title`, `--author`, `--narrator`, `--year`, `--genre`,
`--description`, `--description-file <path>`, `--cover <path>`,
`--series <name>`, `--series-part <N>`, `--jobs <N>`, `--tmp-dir <dir>`,
`--force`, `--allow-missing-metadata`.

### Retag mode (existing m4b → new metadata + cover)

If the input path is a `.m4b` file rather than a directory, `make_m4b.py`
runs in **retag mode**: it remuxes the existing audio + chapters via
`-c copy` and rewrites only the format-level metadata + cover. Use this for
Audible AAX rips that already ship as m4b but with wrong year, branded cover,
or messy album text.

```sh
python3 make_m4b.py "raw/05 Excession/Culture Book 5 - Excession.m4b" \
  --output "processed/Excession - Culture, Book 5.m4b" \
  --title "Excession" --series Culture --series-part 5
```

Resolution order per field: explicit `--flag` > sidecar `.nfo` > tag already
baked into the input m4b > fallback. Useful side tags like `album_artist`,
`grouping`, `track` are preserved by the merge unless overridden by series
flags.

### Series convention

When `--series` and `--series-part` are both set, the script emits the
existing processed/ pattern (matches the Bobiverse files):

| Tag            | Value                              |
| -------------- | ---------------------------------- |
| `album`        | `<Title>: <Series>, Book <N>`      |
| `grouping`     | `<Series> #<N>`                    |
| `album_artist` | `<Author>` (forced clean)          |

This is how Audiobookshelf and most player apps derive series + position
from MP4 audiobooks (no standard MP4 atom for "series" exists, so the
album/grouping convention is the de-facto carrier).

Cover art priority for input: `--cover` > sidecar image in the source dir
(`cover.*`/`folder.*`/`front.*` or the largest `.jpg`/`.png`/`.webp`) >
embedded ID3 art on the first audio file.

**Output**: cover is written as a sidecar `<basename>.jpg` adjacent to the
m4b — *not* embedded into the m4b stream. Audiobookshelf reads sidecar
JPEGs with matching basenames, so cover is visible to ABS/plappa without
baking it into a video stream that breaks under `+frag_keyframe`. The
muxer flags `+faststart+frag_keyframe -frag_duration 5000000` produce
fragmented MP4 — required so iOS AVPlayer doesn't have to fetch a
multi-MB sample-table moov before the first audio sample plays. See
`make_m4b.py`'s top-of-file docstring under CORRECTNESS NOTES for the
arithmetic on why this matters.

### Filling metadata before encoding

**Every m4b in `processed/` must have a complete metadata set.** The script
prints a `📝 Metadata:` block at startup listing each field and now refuses
new builds with missing required fields unless `--allow-missing-metadata` is
passed. Use that bypass only for scratch/test outputs, not for files destined
for `processed/`.

**Required fields** (the script refuses production builds without them):

- title, author, narrator, year, genre, description, cover art

Sourcing strategy, in priority order:

1. **Sidecar `.nfo` in the source dir.** The script auto-fills KAZIN-style
   nfos (the common Audible-rip format — see `raw/05 Excession/*.nfo` for an
   example). If one exists and parses cleanly, you're done.
2. **Manual research → flags.** When no nfo exists, look up the book before
   running the script:
   - **Audible** → narrator, audio publisher, audio release year
   - **Goodreads** → description, original publication year, genre
   - **OpenLibrary / ISFDB / Wikipedia** → backup sources for year, genre,
     publisher
   Then pass via flags: `--narrator`, `--year`, `--genre`, plus
   `--description-file <path>` for the blurb (multi-paragraph descriptions
   read poorly on the CLI; put the text in a file).
3. **Hand-written nfo.** For books you'll re-process or want to permanently
   document, drop a minimal KAZIN-style nfo into the source dir. Future runs
   pick it up automatically and you don't need to re-research.

When you research metadata for option (2) or (3), **save it to disk** — write
the description to `description.txt` or hand-roll an `.nfo` — so the next
agent that touches this book doesn't have to redo the lookup.

### Mapping from nfo / flags to MP4 atoms

| Source                 | FFMETADATA key      | MP4 atom |
| ---------------------- | ------------------- | -------- |
| `Title:` / `--title`   | `title`, `album`    | ©nam, ©alb |
| `Author:` / `--author` | `artist`            | ©ART     |
| `Read By:` (or `Narrator:`) / `--narrator` | `composer` | ©wrt |
| `Original Publication:` / `(P)<year>` / `--year` | `date` | ©day |
| `Genre:` / `--genre`   | `genre`             | ©gen     |
| `Book Description` block / `--description[-file]` | `description` | desc |
| *whole nfo verbatim*   | `comment`           | ©cmt     |

`Publisher:` is parsed but **not embedded** — ffmpeg's iPod muxer silently
drops the `publisher` FFMETADATA key (no standard MP4 atom mapping). The
extracted value sits unused; if you need publisher to surface, set it in
Audiobookshelf directly after import.

Precedence: explicit `--flag` > nfo field > built-in fallback (directory name
for title, "Unknown Author" for author, omitted for everything else).

### Sourcing cover art

If the source dir's sidecar is small or missing, fetch a clean hi-res cover and
save it as `cover.jpg` in the source dir before encoding. Goal: hi-res square
art with no retailer branding.

**Never use a cover with retailer branding** — no "Audible Original" banners,
"Only from Audible" corner ribbons, "AUDIOBOOK / MP3 AUDIO" frames, "Apple
Books" overlays, etc.

**Prefer square covers.** Three paths, in priority order:

1. **Clean square exists** — use it directly. Try in order: publisher page,
   Amazon print/Kindle ASIN (not the audio ASIN), OpenLibrary
   (`covers.openlibrary.org/b/isbn/<ISBN>-L.jpg`, capped ~333×500), ISFDB wiki
   cover scans, artist's portfolio.
2. **Clean rectangular exists** (typical: Goodreads/Kindle 2:3 portrait scans) —
   feed it through the **`debrand-audiobook-cover`** skill
   (`.claude/skills/debrand-audiobook-cover/SKILL.md`). The skill is named for
   debranding but the underlying Codex `image_gen` tool also outpaints — write
   the prompt to extend the background sideways into a square while preserving
   the title text, byline, and central artwork. Square clean beats rectangular
   clean.
3. **Only branded hi-res exists** (typical for Audible exclusives) — fetch the
   2400×2400 (or up to 3000×3000) master via the iTunes Lookup API
   (`https://itunes.apple.com/lookup?id=<APPLE_BOOKS_ID>&entity=audiobook`,
   replace `100x100bb.jpg` in the artwork URL with `3000x3000bb.jpg`), then
   run the same skill to strip the branding.

**Codex invocation gotcha (read before retrying the skill).** When invoking
`codex exec` for the debrand, do **not** pass the image with `-i input.jpg` —
that puts Codex into "read my own imagegen SKILL.md" mode and it exits with
code 0 having called no tools (reproduced four times across xhigh/medium/low
reasoning). Instead omit `-i`, set `-c model_reasoning_effort=low`, and tell
Codex in the prompt to call `view_image` on the absolute path itself. Also
note: `image_gen` has no save-path argument — every generation lands in
`$CODEX_HOME/generated_images/<session-id>/ig_*.png`, so the prompt must
include an explicit shell step that locates the newest file there and
`mv`/`ffmpeg` it to your target. Full template + gotchas list in
`.claude/skills/debrand-audiobook-cover/SKILL.md`.

The m4b accepts any aspect ratio, so a clean rectangular cover still beats a
branded square one if no Codex path is available — but with the skill on hand,
square is almost always reachable.

## Requirements

- `ffmpeg` and `ffprobe` on PATH (the script shells out to both).
- Python 3 with stdlib only (no third-party deps).

## Editing `make_m4b.py`

The docstring at the top documents non-obvious correctness constraints —
**read it before changing encoding behavior**. Highlights:

- `-ss`/`-to` go *before* `-i` (demuxer-level seek). Putting them after `-i`
  makes each parallel worker decode-and-discard up to its chapter start.
- Mid-chapter splits with `-c:a copy` concat inject encoder-priming silence
  per seam (2112 samples for AAC-LC: ~48 ms at 44.1 kHz, ~96 ms at 22 kHz —
  the rate AAX rips actually use). The script only splits at cue boundaries
  for that reason. Don't add a "just split the big mp3 in N pieces" parallel
  path for the no-cue case.
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
- **Don't add `Co-Authored-By: Claude …` (or any AI-assistant) trailers to
  commits in this repo.** Git authorship already records the human committer;
  the trailer is noise and the user has to rewrite history to remove it.
  Same applies to `🤖 Generated with [Claude Code]` blocks in commit
  messages, PR bodies, or anywhere else.
