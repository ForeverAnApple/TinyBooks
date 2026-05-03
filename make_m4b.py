#!/usr/bin/env python3
"""
make_m4b.py — build an m4b audiobook from a directory of source audio.

USAGE
-----
    python3 make_m4b.py <directory|mp3> [--output X] [--cover Y] [--title T] [--author A] [--jobs N]
    python3 make_m4b.py <existing.m4b> [--output X] [--cover Y] [--title T] ...   # retag mode

When the input path is a `.m4b` file rather than a directory, the script
runs in **retag mode**: it remuxes the existing audio + chapters via
`-c copy` and rewrites only the format-level metadata + cover art. Useful
for Audible AAX rips that already ship as m4b but with inconsistent or
branded metadata. See `retag()` for the priority chain
(flag > sidecar nfo > existing m4b tag > fallback).

Auto-detects three source layouts (heuristic, no flag needed). Input may be
a book directory or a direct loose `.mp3`:

  1) MULTI-FILE          — N (≥2) .mp3 files in the directory, or in nested
                           disc subdirectories when there are no top-level
                           mp3s. Each file becomes one chapter. Any .cue is
                           IGNORED because the files already define chapter
                           boundaries.
                           (Example: "We Are Legion" — 61 mp3s.)

  2) SINGLE-FILE + CUE   — exactly 1 .mp3 file accompanied by a .cue sheet.
                           The cue's INDEX 01 timestamps define chapter starts;
                           each chapter is encoded in parallel from the same
                           source mp3 via `-ss START -to END` (seek BEFORE -i,
                           demuxer-level, ~26 ms imprecision which lands inside
                           the natural chapter pause — confirmed imperceptible).
                           Cue times MM:SS:FF use 1/75 s frames; MM can exceed 59.
                           The last cue INDEX may overshoot the mp3 duration
                           (encoder rounding) — clamp and drop zero-length tracks.
                           (Example: "The Singularity Trap" — 1 mp3 + 91 cue
                           tracks — encodes in ~60–70 s on 16 cores.)

  3) SINGLE-FILE NO CUE  — 1 .mp3, no cue. Falls back to a single-chapter
                           encode. NOT parallelized because splitting mid-audio
                           injects ~47 ms of silence per seam with -c:a copy
                           concat (AAC encoder priming, not fixable without
                           re-encoding — see seam-measurement notes below).

ENCODE LAYOUT (sample rate, channels, bitrate)
----------------------------------------------
All chapters are re-encoded to a uniform AAC layout so the final `-c:a copy`
concat sees stream-compatible inputs (a hard requirement). Layout is chosen by
probing source(s):
  - uniform sources: match exactly (no wasteful upsample of mono voice etc.)
  - mixed sources:   fall back to 44.1 kHz / stereo / max-source bitrate

COVER ART (priority order)
--------------------------
  1. --cover <path>                     (explicit)
  2. Sidecar image in the source dir    (cover.*, folder.*, front.*, or the
                                         largest/only .jpg/.png/.webp)
  3. Embedded ID3 art in the first audio file

The encode phase strips embedded art (-vn) so the concat pass sees clean
streams; art is re-attached in the final mux as `attached_pic`.

CORRECTNESS NOTES
-----------------
  - `-ss` BEFORE `-i` (demuxer-level seek). `-ss` after `-i` would make each
    of 16 workers decode-and-discard up to their chapter start (very slow).
  - Mid-chapter splits inject ~47 ms silence + ~93 ms duration drift per seam
    with `-c:a copy` concat (AAC priming). Only split at cue boundaries.
  - `-movflags +faststart` so players don't buffer the whole file to read the
    index. `-f ipod` for canonical m4b muxer. `media_type=2` marks audiobook.
  - Chapter timestamps are built from *encoded* durations (not source) so
    they land exactly where the audio lands after concat.
  - Final outputs are written to a temporary sibling file and atomically moved
    into place only after ffmpeg succeeds. Existing outputs require --force.

WHY NOT …
---------
  - GPU: no production GPU AAC encoder exists; AAC is sequential and already
    cheap on CPU. Parallelism is CPU-process-level.
  - `-c:a copy` mp3-in-m4b: near-instant but mp3-in-mp4 isn't universally
    supported by audiobook players (e.g., Apple Books). Not default.
  - mp4chaps / MP4Box post-injection: works but needs an external tool. We
    stick to ffmpeg alone.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

FALLBACK_SAMPLE_RATE = 44100
FALLBACK_CHANNELS = 2
FALLBACK_BITRATE = 64000  # bps, if we can't read it off the source
AUDIO_EXTS = {".mp3"}  # expand here if you add flac/wav/m4a source support
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
NFO_EXT = ".nfo"


def natural_sort_key(path: Path):
    """Natural-sort a path by all path components, not just the basename.

    Sorting by basename breaks nested disc rips because every directory has
    "Track 01.mp3", "Track 02.mp3", etc. Including path parts keeps Disc 01
    together before Disc 02 while still sorting numbers numerically.
    """
    key = []
    for part in path.parts:
        chunks = []
        for chunk in re.split(r"(\d+)", part):
            if not chunk:
                continue
            chunks.append((1, int(chunk)) if chunk.isdigit() else (0, chunk.lower()))
        key.append(chunks)
    return key


def output_path_for(output_arg, default_name: str) -> Path:
    return Path(output_arg).expanduser() if output_arg else Path(default_name)


def validate_output_path(path: Path, *, force: bool):
    parent = path.parent
    if not parent.exists():
        raise SystemExit(f"❌ Output directory does not exist: {parent}")
    if not parent.is_dir():
        raise SystemExit(f"❌ Output parent is not a directory: {parent}")
    if path.exists() and not force:
        raise SystemExit(f"❌ Output already exists: {path} (pass --force to replace it)")


def make_temp_dir(prefix: str, output_path: Path, tmp_dir_arg=None) -> Path:
    base = Path(tmp_dir_arg).expanduser() if tmp_dir_arg else output_path.parent
    if not base.exists():
        raise SystemExit(f"❌ Temp directory does not exist: {base}")
    if not base.is_dir():
        raise SystemExit(f"❌ Temp path is not a directory: {base}")
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))


def make_temp_output_path(output_path: Path) -> Path:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=str(output_path.parent),
    )
    os.close(fd)
    return Path(tmp_name)


def install_temp_output(tmp_output: Path, output_path: Path):
    os.replace(tmp_output, output_path)


def metadata_missing(*, title, author, narrator, year, genre, description, cover_art):
    missing = []
    if not str(title or "").strip():
        missing.append("title")
    if not str(author or "").strip() or author == "Unknown Author":
        missing.append("author")
    if not str(narrator or "").strip():
        missing.append("narrator")
    if not str(year or "").strip():
        missing.append("year")
    if not str(genre or "").strip():
        missing.append("genre")
    if not str(description or "").strip():
        missing.append("description")
    if cover_art is None:
        missing.append("cover art")
    return missing


def print_metadata_summary(*, title, author, narrator, year, genre, series_display,
                           description, cover_art):
    print("📝 Metadata:")
    for label, value in (
        ("title", title), ("author", author), ("narrator", narrator),
        ("year", year), ("genre", genre), ("series", series_display),
        ("description", f"{len(description)} chars" if description else None),
        ("cover", cover_art.name if cover_art else None),
    ):
        print(f"   {label:<12}{value if value else '(missing)'}")


def enforce_required_metadata(args, *, title, author, narrator, year, genre,
                              description, cover_art):
    missing = metadata_missing(
        title=title,
        author=author,
        narrator=narrator,
        year=year,
        genre=genre,
        description=description,
        cover_art=cover_art,
    )
    if not missing:
        return
    msg = f"Missing required metadata: {', '.join(missing)}"
    if args.allow_missing_metadata:
        print(f"⚠️ {msg} — continuing because --allow-missing-metadata was set.")
        return
    raise SystemExit(
        f"❌ {msg}\n"
        "   Add flags/sidecars before encoding, or pass --allow-missing-metadata "
        "for a scratch/test build."
    )


def ffconcat_escape(path) -> str:
    # ffmpeg concat demuxer accepts single-quoted paths; embedded single quotes
    # must be escaped as: '\''
    return str(path).replace("'", "'\\''")


def ffmetadata_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace("#", r"\#")
        .replace("=", r"\=")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def ffmetadata_escape_multiline(value: str) -> str:
    """Like ffmetadata_escape, but preserves newlines as FFMETADATA line
    continuations (`\\` + LF). Use for description/comment, where collapsing
    paragraphs to a single line would mangle the displayed text."""
    value = (
        value.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace("#", r"\#")
        .replace("=", r"\=")
    )
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return value.replace("\n", "\\\n")


def probe_stream(file_path):
    """Return (duration_seconds, sample_rate, channels, bit_rate_bps) for the first
    audio stream. bit_rate may be None if neither stream nor format reports it."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,channels,bit_rate:format=duration,bit_rate",
        "-of", "json",
        str(file_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {file_path}: {result.stderr.strip()}")

    try:
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        fmt = data.get("format", {})
        duration = float(fmt["duration"])
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
        # Prefer per-stream bit_rate; fall back to container-level bit_rate.
        bit_rate = stream.get("bit_rate") or fmt.get("bit_rate")
        bit_rate = int(bit_rate) if bit_rate else None
    except (KeyError, ValueError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not parse ffprobe output for {file_path}: {result.stdout!r}") from exc

    if duration <= 0:
        raise RuntimeError(f"Invalid duration for {file_path}: {duration}")

    return duration, sample_rate, channels, bit_rate


def probe_format_tags(file_path) -> dict:
    """Return the format-level tag dict for `file_path` (e.g. an existing m4b).
    Used by retag mode so missing flags fall back to baked-in tags rather
    than blanking them. Empty dict if the file has no tags or ffprobe fails."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format_tags",
        "-of", "json",
        str(file_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout).get("format", {}).get("tags", {}) or {}
    except json.JSONDecodeError:
        return {}


def format_hms(seconds: float) -> str:
    s = max(0, int(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def render_bar(fraction: float, width: int = 24) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(fraction * width)
    return "█" * filled + "░" * (width - filled)


def format_progress_line(label, frac, audio_done, audio_total, elapsed, count_done=None, count_total=None):
    """One-line, self-contained progress string. Always includes the phase
    label and audio-seconds done/total so a `tail -3` of the log file is
    readable in isolation."""
    frac = max(0.0, min(1.0, frac))
    eta = elapsed * (1 - frac) / frac if frac > 0 else 0.0
    bar = render_bar(frac)
    pct = int(frac * 100)
    count = f"{count_done}/{count_total} ch  " if count_total is not None else ""
    audio = f"audio {format_hms(audio_done)}/{format_hms(audio_total)}  "
    return (
        f"   {label:<6} [{bar}] {pct:3d}%  {count}"
        f"{audio}elapsed {format_hms(elapsed)}  eta {format_hms(eta)}"
    )


class ProgressPrinter:
    """Thread-safe, throttled progress emitter. On a TTY, overwrites a single
    line at ~5 Hz for a smooth bar. Off-TTY (piped to a log), emits one full
    line every couple of seconds — readable by `tail`, doesn't explode the log
    on multi-hour encodes (~250 lines for a 13-min job at 0.5 Hz)."""

    def __init__(self, *, is_tty=None):
        self.is_tty = sys.stdout.isatty() if is_tty is None else is_tty
        self.throttle = 0.2 if self.is_tty else 2.0
        self._lock = threading.Lock()
        self._last = 0.0
        self._line_width = 0

    def emit(self, line, *, force=False):
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last < self.throttle:
                return
            self._last = now
            if self.is_tty:
                pad = max(self._line_width, len(line))
                self._line_width = len(line)
                print(f"\r{line:<{pad}}", end="", flush=True)
            else:
                print(line, flush=True)

    def newline(self):
        # Call once after a phase ends if we were emitting on a TTY.
        if self.is_tty:
            print()


def parse_cue_time(s: str) -> float:
    """Cue INDEX time `MM:SS:FF` where FF is 1/75 sec. MM may exceed 59."""
    parts = s.split(":")
    if len(parts) != 3:
        raise ValueError(f"bad cue time {s!r}")
    mm, ss, ff = (int(p) for p in parts)
    return mm * 60 + ss + ff / 75.0


def parse_cue(path: Path):
    """Parse a cue sheet. Returns list of {'num': int, 'title': str|None,
    'start': float}. Ignores fields we don't need (PERFORMER per-track etc)."""
    tracks = []
    current = None
    in_track = False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            m_track = re.match(r"^TRACK\s+(\d+)\s+AUDIO", s)
            m_title = re.match(r'^TITLE\s+"(.*)"', s)
            m_idx = re.match(r"^INDEX\s+01\s+(\S+)", s)
            if m_track:
                if current is not None:
                    tracks.append(current)
                current = {"num": int(m_track.group(1)), "title": None, "start": None}
                in_track = True
            elif m_title and in_track and current is not None:
                current["title"] = m_title.group(1)
            elif m_idx and current is not None:
                current["start"] = parse_cue_time(m_idx.group(1))
    if current is not None:
        tracks.append(current)
    # Drop tracks without a start timestamp (malformed cue); keep sort stable.
    return [t for t in tracks if t["start"] is not None]


def find_nfo(directory: Path):
    """Return the .nfo file in `directory`, or None. Multiple .nfo files are
    rare in practice; if found, pick the first by natural sort and warn."""
    nfos = sorted(
        [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == NFO_EXT],
        key=natural_sort_key,
    )
    if not nfos:
        return None
    if len(nfos) > 1:
        print(f"⚠️ Multiple .nfo files in {directory.name}; using {nfos[0].name}")
    return nfos[0]


# KAZIN nfos almost always store Title: as "<Series> Book <N> - <Real Title>"
# (e.g. "Culture Book 5 - Excession"). Strip the prefix so the cleaned title
# can be combined with --series/--series-part without producing an album like
# "Culture Book 5 - Excession: Culture, Book 5". Conservative: only matches
# the canonical "<Words> Book <digits> - " shape.
KAZIN_TITLE_PREFIX = re.compile(r"^[A-Za-z][A-Za-z .'’]*\s+Book\s+\d+\s*[-–]\s*")


def _strip_kazin_title_prefix(title: str) -> str:
    return KAZIN_TITLE_PREFIX.sub("", title).strip()


def parse_nfo(path: Path) -> dict:
    """Parse a release-info nfo (KAZIN-style 'key: value' table plus a
    'Book Description' block at the end). Returns a dict with keys:
      title, author, narrator, publisher, genre, year, description, raw
    Any field may be None if the nfo doesn't carry it. `raw` is the full file
    text; suitable for stuffing into the m4b `comment` atom for archival.

    `title` is cleaned of the canonical KAZIN "<Series> Book <N> - " prefix;
    when a clean is applied, the original is reported via the `title_raw`
    key so the caller can print a notice.

    Tolerant by design: unknown formats just yield mostly-None — caller falls
    back to existing defaults."""
    text = path.read_text(encoding="utf-8", errors="replace")

    fields = {
        "title": None, "title_raw": None, "author": None, "narrator": None,
        "publisher": None, "genre": None, "year": None,
        "description": None, "raw": text,
    }

    # `Key: value` lines, leading-space tolerant. KAZIN uses fixed-column
    # alignment (`Title:                  ...`); .strip() handles it.
    keymap = {
        "title": "title",
        "author": "author",
        "read by": "narrator",
        "narrator": "narrator",
        "publisher": "publisher",
        "genre": "genre",
    }
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z][A-Za-z .]*?)\s*:\s+(.+?)\s*$", line)
        if not m:
            continue
        key = m.group(1).strip().lower()
        val = m.group(2).strip()
        target = keymap.get(key)
        if target and not fields[target]:
            fields[target] = val

    if fields["title"]:
        cleaned = _strip_kazin_title_prefix(fields["title"])
        if cleaned and cleaned != fields["title"]:
            fields["title_raw"] = fields["title"]
            fields["title"] = cleaned

    # Year: prefer Original Publication (the work's year, not the audio rip
    # year). Fall back to the (P) year inside Copyright if present.
    m = re.search(r"^\s*Original Publication:\s+(\d{4})\b", text, re.MULTILINE)
    if m:
        fields["year"] = m.group(1)
    else:
        m = re.search(r"\(P\)\s*(\d{4})\b", text)
        if m:
            fields["year"] = m.group(1)

    # Book Description block: everything after the section header, stripped.
    # KAZIN format: header line "Book Description" followed by a row of "=".
    m = re.search(
        r"^Book Description\s*\n=+\s*\n(.+?)\Z",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if m:
        fields["description"] = m.group(1).strip()

    return fields


def find_sidecar_cover(directory: Path):
    """Look for a cover image in the source directory. Priority:
    1. Conventional names in order: cover, folder, front, albumart, artwork.
    2. The single largest image file in the directory (if any).

    The preferred-name walk is ordered so that a folder containing both
    e.g. cover.jpg and front.png picks cover.jpg deterministically rather
    than whichever the filesystem yields first."""
    files = sorted(
        [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS],
        key=natural_sort_key,
    )
    if not files:
        return None
    by_stem = {p.stem.lower(): p for p in files}
    for name in ("cover", "folder", "front", "albumart", "artwork"):
        if name in by_stem:
            return by_stem[name]
    # Otherwise pick the largest — publishers often ship one big sidecar jpg.
    return max(files, key=lambda p: p.stat().st_size)


def find_sidecar_cover_for_audio(audio: Path):
    """Cover lookup for a direct loose-mp3 input.

    Avoid picking a random `cover.jpg` from a busy parent directory. Same-stem
    art is always safe; conventional names are only used when the parent holds
    exactly one top-level source audio file.
    """
    parent = audio.parent
    images = sorted(
        [p for p in parent.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS],
        key=natural_sort_key,
    )
    same_stem = [p for p in images if p.stem.lower() == audio.stem.lower()]
    if same_stem:
        return same_stem[0]
    audio_siblings = [
        p for p in parent.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS
    ]
    if len(audio_siblings) == 1:
        return find_sidecar_cover(parent)
    return None


def find_cue_for_audio(audio: Path, root: Path):
    cue_dirs = []
    for directory in (audio.parent, root):
        if directory not in cue_dirs:
            cue_dirs.append(directory)
    cue_files = sorted(
        [
            p
            for directory in cue_dirs
            for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() == ".cue"
        ],
        key=natural_sort_key,
    )
    if not cue_files:
        return None
    return next(
        (c for c in cue_files if c.stem == audio.stem or c.stem == audio.name),
        cue_files[0],
    )


def find_audio_files(directory: Path):
    top_level = sorted(
        [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS],
        key=natural_sort_key,
    )
    if top_level:
        return top_level
    return sorted(
        [p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS],
        key=lambda p: natural_sort_key(p.relative_to(directory)),
    )


def detect_layout(source: Path):
    """Classify the source. Returns (layout, payload) where layout is one of
    'multi_file', 'single_file_with_cue', 'single_file_no_cue'.

    `source` may be a book directory or a direct loose mp3 path.
    """
    if source.is_file() and source.suffix.lower() in AUDIO_EXTS:
        cue = find_cue_for_audio(source, source.parent)
        if cue:
            return "single_file_with_cue", (source, cue)
        return "single_file_no_cue", source

    directory = source
    audio_files = find_audio_files(directory)
    if len(audio_files) >= 2:
        return "multi_file", audio_files
    if len(audio_files) == 1:
        audio = audio_files[0]
        cue = find_cue_for_audio(audio, directory)
        if cue:
            return "single_file_with_cue", (audio, cue)
        return "single_file_no_cue", audio
    raise SystemExit(f"❌ No supported audio files found in {directory}")


def build_chapters(layout: str, payload, probe_fn):
    """Return list of (title, source_path, ss_or_None, to_or_None).
    `probe_fn(path)` must return the file's duration in seconds (for cue end-clamping)."""
    if layout == "multi_file":
        files = payload
        stem_counts = {}
        for f in files:
            stem_counts[f.stem] = stem_counts.get(f.stem, 0) + 1
        chapters = []
        for f in files:
            title = f.stem
            if stem_counts[f.stem] > 1 and f.parent.name:
                title = f"{f.parent.name} - {f.stem}"
            chapters.append((title, f, None, None))
        return chapters

    if layout == "single_file_no_cue":
        source = payload
        return [(source.stem, source, None, None)]

    # single_file_with_cue
    source, cue_path = payload
    total_dur = probe_fn(source)
    tracks = parse_cue(cue_path)
    if not tracks:
        raise SystemExit(f"❌ Cue sheet has no usable tracks: {cue_path}")
    tracks.sort(key=lambda t: t["start"])

    chapters = []
    dropped = 0
    for i, t in enumerate(tracks):
        start = min(t["start"], total_dur)
        end = tracks[i + 1]["start"] if i + 1 < len(tracks) else total_dur
        end = min(end, total_dur)
        if end <= start + 0.01:  # zero/negative length — cue overshoot at EOF or dup
            dropped += 1
            continue
        title = t["title"] or f"Chapter {t['num']:02d}"
        chapters.append((title, source, start, end))
    if dropped:
        print(f"   (dropped {dropped} zero-length cue track(s) near EOF)")
    return chapters


def extract_embedded_art(src, dst):
    """Copy the first video/image stream out of src (e.g. ID3 APIC) to dst.
    Returns dst on success, None if the source has no art."""
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", str(src),
        "-an",
        "-map", "0:v:0",
        "-c:v", "copy",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
        return dst
    return None


def encode_to_aac(job, on_progress=None):
    """Re-encode one (range of a) file to uniform AAC. Strips all source
    metadata and embedded art so the later concat pass sees stream-compatible
    inputs. `ss`/`to` may be None (whole file) or floats (seconds). -ss/-to
    go BEFORE -i so ffmpeg uses fast demuxer seek, not decode-and-discard.

    If `on_progress(dst, encoded_seconds)` is given, it is called as ffmpeg
    emits -progress events (~2 Hz). Stderr is drained in a side thread so the
    pipe buffer can't deadlock if ffmpeg ever talks more than 64 KB on it."""
    src, dst, sample_rate, channels, bit_rate, ss, to = job
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostats"]
    if ss is not None:
        cmd.extend(["-ss", f"{ss:.3f}"])
    if to is not None:
        cmd.extend(["-to", f"{to:.3f}"])
    cmd.extend([
        "-i", str(src),
        "-vn",
        "-map_metadata", "-1",
        "-c:a", "aac",
        "-b:a", str(bit_rate),
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-progress", "pipe:1",
        "-f", "ipod",
        str(dst),
    ])
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    err_chunks = []

    def _drain_stderr():
        err_chunks.append(proc.stderr.read())

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    if proc.stdout is not None:
        for line in proc.stdout:
            if on_progress is None:
                continue
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                val = line.split("=", 1)[1].strip()
                if not val or val == "N/A":
                    continue
                try:
                    us = int(val)
                except ValueError:
                    continue
                on_progress(dst, us / 1_000_000.0)

    proc.wait()
    err_thread.join()
    if proc.returncode != 0:
        raise RuntimeError(f"encode failed for {src} [{ss}..{to}]:\n{''.join(err_chunks)}")
    return dst


def build_metadata(title, author, chapters, enc_durations, *,
                   narrator=None, genre=None, year=None,
                   description=None, comment=None,
                   series=None, series_part=None) -> str:
    """chapters: list of (title, src, ss, to). enc_durations: matching list of
    measured post-encode durations in seconds — these are what actually ends
    up in the concatenated m4b, so chapter marks line up with the audio.

    Optional kwargs land in standard MP4 atoms via FFMETADATA:
      narrator → composer (©wrt) — audiobook convention
      genre → genre
      year → date
      description → desc (book blurb; what ABS displays)
      comment → ©cmt (we use this for the full nfo dump)

    Series convention (when both series and series_part are set): album becomes
    "<Title>: <Series>, Book <N>", album_artist is forced to <author>, and
    grouping is "<Series> #<N>". This matches retag mode and the existing
    processed/ files — no standard MP4 atom for "series" exists, so the
    album/grouping/album_artist triple is the de-facto carrier ABS reads.
    `track` is always set to "1/1": one m4b is one track of one, and some MP4
    series parsers (incl. parts of ABS's heuristic chain) treat a missing
    track count as an incomplete file and skip series detection.

    NOT included: publisher. ffmpeg's ipod muxer silently drops the
    `publisher` FFMETADATA key (no standard atom mapping). Set it via the
    Audiobookshelf API after import if needed."""
    has_series = bool(series and series_part)
    album_value = f"{title}: {series}, Book {series_part}" if has_series else title

    current_time_ms = 0
    lines = [
        ";FFMETADATA1",
        f"title={ffmetadata_escape(title)}",
        f"artist={ffmetadata_escape(author)}",
        f"album={ffmetadata_escape(album_value)}",
        "media_type=2",
        "track=1/1",
    ]
    if has_series:
        lines.append(f"album_artist={ffmetadata_escape(author)}")
        lines.append(f"grouping={ffmetadata_escape(f'{series} #{series_part}')}")
    if narrator:
        lines.append(f"composer={ffmetadata_escape(narrator)}")
    if genre:
        lines.append(f"genre={ffmetadata_escape(genre)}")
    if year:
        lines.append(f"date={ffmetadata_escape(year)}")
    if description:
        lines.append(f"description={ffmetadata_escape_multiline(description)}")
    if comment:
        lines.append(f"comment={ffmetadata_escape_multiline(comment)}")
    lines.append("")

    for (chapter_title, _src, _ss, _to), duration in zip(chapters, enc_durations):
        duration_ms = round(duration * 1000)
        start_ms = current_time_ms
        end_ms = current_time_ms + duration_ms

        lines.extend([
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_ms}",
            f"END={end_ms}",
            f"title={ffmetadata_escape(chapter_title)}",
            "",
        ])
        current_time_ms = end_ms

    return "\n".join(lines)


# Keys we never want to carry over from an input m4b's format tags. The
# `major_brand` / `minor_version` / `compatible_brands` triple is muxer-set
# (ffmpeg's ipod muxer rewrites them on output anyway, and stale `isom`
# brands here can confuse strict players). `encoder` is informational,
# `creation_time` is set fresh by the muxer, and the `handler_name` /
# `vendor_id` / `language` keys belong to the audio stream, not the format.
RETAG_DROP_TAGS = {
    "major_brand", "minor_version", "compatible_brands",
    "encoder", "creation_time",
    "handler_name", "vendor_id", "language",
}


def retag(src: Path, args):
    """Retag mode: rewrite format-level metadata + cover on an existing m4b
    without re-encoding the audio. Audio + chapters pass through via
    `-c copy` / `-map_chapters 0`.

    Resolution order for each metadata field (first non-empty wins):
      1. explicit --flag
      2. sidecar .nfo in the m4b's parent directory (KAZIN-style)
      3. tag already baked into the input m4b (read via ffprobe)
      4. fallback (src.stem for title, "Unknown Author" for author, else
         omitted)

    Cover priority:
      1. --cover <path>
      2. sidecar image in the parent directory
      3. existing embedded art on the input m4b (extracted and re-attached
         as `attached_pic` so the disposition is correct)

    Tags from the input m4b that we DON'T have a resolution for are passed
    through verbatim — this keeps useful side metadata like `album_artist`,
    `grouping`, and `track` from KAZIN rips intact instead of accidentally
    blanking them.
    """
    parent = src.parent
    nfo_path = find_nfo(parent) if parent.is_dir() else None
    nfo = parse_nfo(nfo_path) if nfo_path else {}
    if nfo_path:
        print(f"📝 Found nfo: {nfo_path.name}")
        if nfo.get("title_raw"):
            print(f"   cleaned title: {nfo['title_raw']!r} → {nfo['title']!r} "
                  "(pass --title to override)")

    existing = probe_format_tags(src)

    output_path = output_path_for(args.output, f"{src.stem}.m4b")
    validate_output_path(output_path, force=args.force)
    title = args.title or nfo.get("title") or existing.get("title") or src.stem
    author = args.author or nfo.get("author") or existing.get("artist") or "Unknown Author"
    narrator = args.narrator or nfo.get("narrator") or existing.get("composer")
    year = args.year or nfo.get("year") or existing.get("date")
    genre = args.genre or nfo.get("genre") or existing.get("genre")
    if args.description_file:
        description = Path(args.description_file).expanduser().read_text(
            encoding="utf-8", errors="replace"
        ).strip()
    else:
        description = args.description or nfo.get("description") or existing.get("description")
    # Comment is reserved for archival nfo dumps (per AGENTS.md). With a
    # fresh nfo we replace whatever was there (including the stale-truncated-
    # description pattern KAZIN rips bake in). Without an nfo we leave
    # existing.comment alone — a previous run may have archived an nfo and
    # we don't want to wipe it on every retag.
    comment = nfo.get("raw")
    cover_art = Path(args.cover).expanduser().resolve() if args.cover else None

    series_display = (
        f"{args.series}, Book {args.series_part}"
        if args.series and args.series_part else None
    )
    print("📐 Mode: retag (existing m4b)")

    tmpdir = make_temp_dir("make_m4b_retag_", output_path, args.tmp_dir)
    tmp_output = make_temp_output_path(output_path)
    try:
        # Cover priority: --cover > sidecar in parent > extracted embedded.
        if cover_art is None and parent.is_dir():
            sidecar = find_sidecar_cover(parent)
            if sidecar is not None:
                cover_art = sidecar
                print(f"🖼  Using sidecar cover: {sidecar.name}")
        if cover_art is None:
            extracted = extract_embedded_art(src, tmpdir / "embedded_cover.jpg")
            if extracted is not None:
                cover_art = extracted
                print(f"🖼  Using embedded cover from {src.name}")

        print_metadata_summary(
            title=title,
            author=author,
            narrator=narrator,
            year=year,
            genre=genre,
            series_display=series_display,
            description=description,
            cover_art=cover_art,
        )
        enforce_required_metadata(
            args,
            title=title,
            author=author,
            narrator=narrator,
            year=year,
            genre=genre,
            description=description,
            cover_art=cover_art,
        )

        # Series convention (matches existing processed/ Bobiverse files): when
        # both --series and --series-part are set, album becomes
        # "<Title>: <Series>, Book <N>", grouping becomes "<Series> #<N>",
        # and album_artist becomes the bare author. When series flags are
        # absent, album falls back to title and album_artist/grouping are
        # preserved from existing tags by the merge below.
        if args.series and args.series_part:
            album_value = f"{title}: {args.series}, Book {args.series_part}"
        else:
            album_value = title

        # Merge: existing tags as the baseline, our resolved values override.
        # Always force album = computed value (existing albums tend to be
        # messy: "Culture Book 4 ", "01 Consider Phlebas", etc.).
        merged = {k: v for k, v in existing.items() if k.lower() not in RETAG_DROP_TAGS}
        # When we have a fresh nfo we drop the existing comment so the
        # "stale truncated description" pattern from KAZIN rips doesn't
        # survive into the rewrite. Without an nfo, leave existing.comment
        # alone — a prior run may have archived one we don't want to wipe.
        if comment:
            merged.pop("comment", None)
        merged["title"] = title
        merged["artist"] = author
        merged["album"] = album_value
        merged["media_type"] = "2"
        # Normalize track to "1/1" — an m4b is one audio track of one. Some MP4
        # series parsers (incl. parts of Audiobookshelf's heuristic chain) treat
        # a missing track count as an incomplete file and skip series detection.
        merged["track"] = "1/1"
        if args.series and args.series_part:
            merged["album_artist"] = author
            merged["grouping"] = f"{args.series} #{args.series_part}"
        if narrator:
            merged["composer"] = narrator
        if year:
            merged["date"] = year
        if genre:
            merged["genre"] = genre
        if description:
            merged["description"] = description
        if comment:
            merged["comment"] = comment

        lines = [";FFMETADATA1"]
        for k, v in merged.items():
            v_str = str(v)
            if "\n" in v_str or "\r" in v_str:
                lines.append(f"{k}={ffmetadata_escape_multiline(v_str)}")
            else:
                lines.append(f"{k}={ffmetadata_escape(v_str)}")
        metadata_path = tmpdir / "metadata.txt"
        metadata_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Mux: audio + chapters from input via copy; metadata from our
        # FFMETADATA file; cover from disk (re-attached as attached_pic so
        # the disposition flag survives even when the source had embedded
        # art with the wrong disposition).
        cmd = [
            "ffmpeg", "-y", "-v", "error", "-nostats",
            "-i", str(src),
            "-f", "ffmetadata", "-i", str(metadata_path),
        ]
        cover_idx = None
        if cover_art:
            cmd.extend(["-i", str(cover_art)])
            cover_idx = 2

        cmd.extend(["-map", "0:a:0"])
        if cover_idx is not None:
            cmd.extend(["-map", f"{cover_idx}:v:0", "-disposition:v:0", "attached_pic"])

        cmd.extend([
            "-map_metadata", "1",
            "-map_chapters", "0",
            "-c:a", "copy",
        ])
        if cover_idx is not None:
            cmd.extend(["-c:v", "mjpeg"])

        cmd.extend([
            "-movflags", "+faststart",
            "-f", "ipod",
            str(tmp_output),
        ])

        print(f"🎬 Remuxing: {title}...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            install_temp_output(tmp_output, output_path)
            tmp_output = None
            print(f"✅ Success! Created: {output_path}")
        else:
            print(f"❌ FFmpeg Error:\n{result.stderr}")
            raise SystemExit(result.returncode)

    finally:
        if tmp_output is not None:
            tmp_output.unlink(missing_ok=True)
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description="Create an m4b audiobook from a directory of mp3 files, "
                    "or retag an existing .m4b file in place.",
    )
    parser.add_argument("directory",
                        help="Directory of source audio, path to a single .mp3 "
                             "(encode mode), or path to an existing .m4b file "
                             "(retag mode)")
    parser.add_argument("--output", help="Output filename (default: folder_name.m4b)")
    parser.add_argument("--cover", help="Path to cover art image")
    parser.add_argument("--title", help="Book title")
    parser.add_argument("--author", help="Author name")
    parser.add_argument("--narrator", help="Narrator (goes in MP4 composer field)")
    parser.add_argument("--year", help="Publication year (4 digits)")
    parser.add_argument("--genre", help="Genre")
    parser.add_argument("--series",
                        help='Series name (e.g. "Culture"). When combined with '
                             '--series-part, populates album as "<Title>: <Series>, '
                             'Book <N>" plus grouping "<Series> #<N>" — the convention '
                             'Audiobookshelf and the existing processed/ files use.')
    parser.add_argument("--series-part",
                        help="Position in the series (e.g. 1, 2, 3). Use with --series.")
    parser.add_argument("--description",
                        help="Book blurb (single line; use --description-file for paragraphs)")
    parser.add_argument("--description-file",
                        help="Path to a text file containing the book blurb (paragraphs preserved)")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4,
                        help="Parallel encode workers (default: cpu count)")
    parser.add_argument("--tmp-dir",
                        help="Directory for temporary encoded chapters (default: output directory)")
    parser.add_argument("--force", action="store_true",
                        help="Replace an existing output file")
    parser.add_argument("--allow-missing-metadata", action="store_true",
                        help="Allow scratch/test builds with missing required metadata")

    args = parser.parse_args()

    # Validate flag inputs up front so we fail before kicking off probe/encode
    # work. Late failures (e.g. missing --description-file caught mid-pipeline)
    # waste time and leave the user staring at a half-printed startup banner.
    if args.year and not re.fullmatch(r"\d{4}", args.year):
        raise SystemExit(f"❌ --year must be 4 digits (got {args.year!r})")
    if args.description_file:
        desc_path = Path(args.description_file).expanduser()
        if not desc_path.is_file():
            raise SystemExit(f"❌ --description-file not found: {desc_path}")
    if args.cover:
        cover_path = Path(args.cover).expanduser()
        if not cover_path.is_file():
            raise SystemExit(f"❌ --cover not found: {cover_path}")
    if bool(args.series) != bool(args.series_part):
        raise SystemExit("❌ --series and --series-part must be set together")
    if args.jobs < 1:
        raise SystemExit(f"❌ --jobs must be >= 1 (got {args.jobs})")

    src_path = Path(args.directory).expanduser().resolve()
    # Retag mode: input is an existing .m4b file. Skip the encode pipeline
    # entirely and just remux audio + chapters with new format-level tags.
    if src_path.is_file() and src_path.suffix.lower() == ".m4b":
        retag(src_path, args)
        return

    if src_path.is_file() and src_path.suffix.lower() in AUDIO_EXTS:
        source_input = src_path
        metadata_dir = src_path.parent
        default_title = src_path.stem
        default_output_name = f"{src_path.stem}.m4b"
        direct_audio_input = True
    elif src_path.is_dir():
        source_input = src_path
        metadata_dir = src_path
        default_title = src_path.name
        default_output_name = f"{src_path.name}.m4b"
        direct_audio_input = False
    else:
        raise SystemExit(f"❌ Directory, .mp3 file, or .m4b file not found: {src_path}")

    # Pre-flag-resolution pass: pull what we can from a sidecar .nfo (KAZIN
    # release info etc). Explicit --flag values still win over nfo values,
    # which still win over directory-name / "Unknown Author" fallbacks.
    nfo_path = find_nfo(metadata_dir)
    nfo = parse_nfo(nfo_path) if nfo_path else {}
    if nfo_path:
        print(f"📝 Found nfo: {nfo_path.name}")
        if nfo.get("title_raw"):
            print(f"   cleaned title: {nfo['title_raw']!r} → {nfo['title']!r} "
                  "(pass --title to override)")

    output_path = output_path_for(args.output, default_output_name)
    validate_output_path(output_path, force=args.force)
    title = args.title or nfo.get("title") or default_title
    author = args.author or nfo.get("author") or "Unknown Author"
    narrator = args.narrator or nfo.get("narrator")
    year = args.year or nfo.get("year")
    genre = args.genre or nfo.get("genre")
    if args.description_file:
        description = Path(args.description_file).expanduser().read_text(
            encoding="utf-8", errors="replace"
        ).strip()
    else:
        description = args.description or nfo.get("description")
    # Stuff the full nfo into `comment` for archival when we have one. Without
    # an nfo, leave comment empty (don't synthesize one — it's just noise).
    comment = nfo.get("raw")
    cover_art = Path(args.cover).expanduser().resolve() if args.cover else None

    series_display = (
        f"{args.series}, Book {args.series_part}"
        if args.series and args.series_part else None
    )
    # Layout detection drives the whole pipeline. See top-of-file docstring.
    layout, payload = detect_layout(source_input)
    layout_labels = {
        "multi_file": "multi-file (one chapter per mp3)",
        "single_file_with_cue": "single mp3 + cue (parallel chapter splits)",
        "single_file_no_cue": "single mp3, no cue (serial — slow)",
    }
    print(f"📐 Layout: {layout_labels.get(layout, layout)}")

    tmpdir = make_temp_dir("make_m4b_", output_path, args.tmp_dir)
    tmp_output = make_temp_output_path(output_path)

    try:
        # Cover priority: --cover > sidecar image > embedded art.
        if cover_art is None:
            sidecar = (
                find_sidecar_cover_for_audio(source_input)
                if direct_audio_input else find_sidecar_cover(metadata_dir)
            )
            if sidecar is not None:
                cover_art = sidecar
                print(f"🖼  Using sidecar cover: {sidecar.name}")

        # Probe once up-front. For single-file layouts we just need its duration
        # (for cue end-clamping) and stream params. For multi-file we'll
        # broadcast-probe below.
        if layout == "single_file_with_cue":
            source = payload[0]
        elif layout == "single_file_no_cue":
            source = payload
        else:
            source = None  # multi_file

        if source is not None:
            source_probe = probe_stream(source)  # (dur, rate, ch, bitrate)
            chapters = build_chapters(layout, payload, lambda _: source_probe[0])
            if layout == "single_file_no_cue":
                print("⚠️ Single mp3 with no cue sheet — encoding serially as one chapter (slow).")
        else:
            chapters = build_chapters(layout, payload, lambda _: 0.0)

        if not chapters:
            raise SystemExit("❌ No chapters to encode.")

        print(f"📖 Chapters: {len(chapters)}")

        # Embedded-art fallback is last since the per-file encode will strip
        # the embedded stream. Use the first chapter's source.
        if cover_art is None:
            first_src = chapters[0][1]
            extracted = extract_embedded_art(first_src, tmpdir / "embedded_cover.jpg")
            if extracted is not None:
                cover_art = extracted
                print(f"🖼  Using embedded cover art from {first_src.name}")

        print_metadata_summary(
            title=title,
            author=author,
            narrator=narrator,
            year=year,
            genre=genre,
            series_display=series_display,
            description=description,
            cover_art=cover_art,
        )
        enforce_required_metadata(
            args,
            title=title,
            author=author,
            narrator=narrator,
            year=year,
            genre=genre,
            description=description,
            cover_art=cover_art,
        )

        # Probe unique sources (for multi_file that's all files; for single-file
        # layouts that's the one big mp3 we already probed).
        unique_sources = list(dict.fromkeys(ch[1] for ch in chapters))
        workers = max(1, min(args.jobs, len(chapters)))

        if len(unique_sources) == 1 and source is not None:
            probed_by_src = {source: source_probe}
        else:
            print(f"🔎 Probing {len(unique_sources)} file(s) ({workers} workers)...")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                probed_by_src = dict(zip(unique_sources, ex.map(probe_stream, unique_sources)))

        src_rates = {probed_by_src[s][1] for s in unique_sources}
        src_channels = {probed_by_src[s][2] for s in unique_sources}
        src_bitrates = [probed_by_src[s][3] for s in unique_sources if probed_by_src[s][3] is not None]

        # Chapter-level durations drive schedule (longest-first) and the progress bar.
        def chapter_duration(ch):
            _title, src, ss, to = ch
            if ss is not None and to is not None:
                return to - ss
            return probed_by_src[src][0]
        src_durations = [chapter_duration(ch) for ch in chapters]

        # If every input shares a layout, match it — avoids inflating mono voice
        # into stereo or 22k into 44.1k for no audible gain. Fall back to a safe
        # uniform layout only when inputs disagree (concat-with-copy requires
        # stream-compatible AAC).
        if len(src_rates) == 1 and len(src_channels) == 1:
            enc_rate = next(iter(src_rates))
            enc_channels = next(iter(src_channels))
            print(f"   inputs uniform: {enc_rate} Hz, {enc_channels} ch — matching source")
        else:
            enc_rate = FALLBACK_SAMPLE_RATE
            enc_channels = FALLBACK_CHANNELS
            print(f"   inputs mixed ({sorted(src_rates)} Hz, {sorted(src_channels)} ch) — normalizing to {enc_rate} Hz, {enc_channels} ch")

        # Match the source bitrate rather than hardcoding. AAC is more efficient
        # than MP3, so the same nominal bitrate typically yields slightly better
        # perceptual quality. With mixed inputs we pick the max so we never
        # degrade the highest-quality source below its own bitrate.
        if not src_bitrates:
            enc_bitrate = FALLBACK_BITRATE
            print(f"   no source bitrate reported — using {enc_bitrate // 1000}k")
        elif len(set(src_bitrates)) == 1:
            enc_bitrate = src_bitrates[0]
            print(f"   inputs uniform: {enc_bitrate // 1000}k — matching source")
        else:
            enc_bitrate = max(src_bitrates)
            print(f"   inputs mixed bitrate ({min(src_bitrates)//1000}k..{max(src_bitrates)//1000}k) — using {enc_bitrate//1000}k")

        # Phase 2: parallel re-encode to a uniform AAC layout. Doing this in
        # parallel is the main speedup vs. a single ffmpeg process (ffmpeg's
        # AAC encoder is single-threaded per stream). For the single-file+cue
        # layout each worker seeks into the shared source via -ss/-to (before
        # -i, demuxer-level seek — not decode-and-discard).
        encoded = [tmpdir / f"{i:05d}.m4a" for i in range(len(chapters))]
        schedule = sorted(
            zip(chapters, encoded, src_durations),
            key=lambda j: -j[2],
        )
        jobs = [
            (ch[1], dst, enc_rate, enc_channels, enc_bitrate, ch[2], ch[3])
            for ch, dst, _ in schedule
        ]
        # Map each job (by its dst path) to its audio duration so we can weight
        # the progress bar by audio-seconds — makes the bar linear even though
        # chapter lengths vary by an order of magnitude.
        dst_duration = {dst: dur for _, dst, dur in schedule}
        total_audio = sum(src_durations)

        print(f"🎧 Encoding {len(chapters)} chapter(s) to AAC ({workers} workers)...")
        # Per-chapter encoded-seconds, fed by ffmpeg -progress events from
        # each worker. Aggregating these (rather than only marking a chapter
        # 100% at completion) gives a smooth bar even when one chapter is
        # the entire book (single-file no-cue layout).
        chapter_seconds = {dst: 0.0 for _, dst, _ in schedule}
        progress_lock = threading.Lock()
        done_count = [0]
        printer = ProgressPrinter()
        start = time.monotonic()

        def _emit(force=False):
            with progress_lock:
                done_audio = sum(chapter_seconds.values())
                done_n = done_count[0]
            elapsed = time.monotonic() - start
            frac = done_audio / total_audio if total_audio > 0 else 1.0
            line = format_progress_line(
                "encode", frac, done_audio, total_audio, elapsed,
                count_done=done_n, count_total=len(chapters),
            )
            printer.emit(line, force=force)

        def on_progress(dst, secs):
            with progress_lock:
                # Clamp — ffmpeg sometimes overshoots the trim window slightly.
                chapter_seconds[dst] = min(secs, dst_duration[dst])
            _emit()

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(encode_to_aac, job, on_progress): job[1] for job in jobs}
            for fut in as_completed(futures):
                fut.result()  # re-raises any encode failure
                dst = futures[fut]
                with progress_lock:
                    # Snap to chapter duration on completion in case we missed
                    # the final progress event on a fast chapter.
                    chapter_seconds[dst] = dst_duration[dst]
                    done_count[0] += 1
                _emit(force=True)
        # Final 100% line so the last log entry shows completion.
        with progress_lock:
            for dst in chapter_seconds:
                chapter_seconds[dst] = dst_duration[dst]
        _emit(force=True)
        printer.newline()

        # Phase 3: probe encoded durations. AAC adds encoder priming so the
        # encoded file isn't exactly the source duration — using the encoded
        # value keeps chapter marks aligned after the -c:a copy concat.
        print("🔎 Measuring encoded durations...")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            enc_durations = [p[0] for p in ex.map(probe_stream, encoded)]

        # Phase 4: build concat list (in original order) and chapter metadata.
        concat_path = tmpdir / "concat.txt"
        with concat_path.open("w", encoding="utf-8") as f:
            for path in encoded:
                f.write(f"file '{ffconcat_escape(path)}'\n")

        metadata_path = tmpdir / "metadata.txt"
        metadata_path.write_text(
            build_metadata(
                title, author, chapters, enc_durations,
                narrator=narrator,
                genre=genre,
                year=year,
                description=description,
                comment=comment,
                series=args.series,
                series_part=args.series_part,
            ),
            encoding="utf-8",
        )

        # Phase 5: mux. -c:a copy since encodes are already uniform AAC.
        # -movflags +faststart moves the index to the file head so players
        # can start playback without reading to EOF. -f ipod is the canonical
        # m4b muxer. -progress pipe:1 streams out_time_us so we can show a
        # real bar instead of a 30-second blank wait on big concat outputs.
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-v", "error", "-nostats",
            "-f", "concat", "-safe", "0", "-i", str(concat_path),
            "-f", "ffmetadata", "-i", str(metadata_path),
        ]
        metadata_idx = 1
        cover_idx = None
        if cover_art:
            ffmpeg_cmd.extend(["-i", str(cover_art)])
            cover_idx = 2

        ffmpeg_cmd.extend(["-map", "0:a:0"])
        if cover_idx is not None:
            ffmpeg_cmd.extend(["-map", f"{cover_idx}:v:0", "-disposition:v:0", "attached_pic"])

        ffmpeg_cmd.extend([
            "-map_metadata", str(metadata_idx),
            "-map_chapters", str(metadata_idx),
            "-c:a", "copy",
        ])
        if cover_idx is not None:
            ffmpeg_cmd.extend(["-c:v", "mjpeg"])

        ffmpeg_cmd.extend([
            "-movflags", "+faststart",
            "-f", "ipod",
            "-progress", "pipe:1",
            str(tmp_output),
        ])

        print(f"🎬 Muxing: {title}...")
        total_dur = sum(enc_durations)
        mux_printer = ProgressPrinter()
        mux_start = time.monotonic()

        def _emit_mux(secs, *, force=False):
            elapsed = time.monotonic() - mux_start
            frac = secs / total_dur if total_dur > 0 else 1.0
            line = format_progress_line("mux", frac, secs, total_dur, elapsed)
            mux_printer.emit(line, force=force)

        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        err_chunks = []

        def _drain_mux_stderr():
            err_chunks.append(proc.stderr.read())

        err_thread = threading.Thread(target=_drain_mux_stderr, daemon=True)
        err_thread.start()

        if proc.stdout is not None:
            for line in proc.stdout:
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    val = line.split("=", 1)[1].strip()
                    if not val or val == "N/A":
                        continue
                    try:
                        us = int(val)
                    except ValueError:
                        continue
                    _emit_mux(min(us / 1_000_000.0, total_dur))

        proc.wait()
        err_thread.join()
        _emit_mux(total_dur, force=True)
        mux_printer.newline()

        if proc.returncode == 0:
            install_temp_output(tmp_output, output_path)
            tmp_output = None
            print(f"✅ Success! Created: {output_path}")
        else:
            print(f"❌ FFmpeg Error:\n{''.join(err_chunks)}")
            raise SystemExit(proc.returncode)

    finally:
        if tmp_output is not None:
            tmp_output.unlink(missing_ok=True)
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
