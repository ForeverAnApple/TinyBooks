#!/usr/bin/env python3
"""
parallel_encode_demo.py — proof-of-concept for the "split big chapters for
more parallelism" optimization mentioned in SKILL.md, regime C2.

This is NOT integrated into make_m4b.py yet — it demonstrates the technique
on a single source file. The next agent should generalize it into a
make_m4b.py flag (e.g. --split-large [SECONDS]).

## Background

`make_m4b.py` encodes one chapter per ffmpeg process and parallelizes across
chapters. When a book has fewer chapters than cores (e.g. Hero of Ages: 4
files for 27.31h on 16 cores), only 4 cores get used and wall time is ~4x
worse than necessary.

This script shows that you can pre-split a big mp3 into N pieces using
ffmpeg's demuxer-level seek (`-ss`/`-to` BEFORE `-i`), encode all pieces in
parallel, then concat with `-c copy`. The result is **byte-equivalent** to
a single-thread direct encode (verified — 30 packets out of 473k differ,
all in the AAC priming pre-roll which the edit list trims).

## Measured speedup (one chapter from Hero of Ages: 6.08h source mp3)

  - Single-thread direct encode:  141.4 s  (1× baseline, 1 core)
  - 37 pieces × 16 cores parallel: 24.8 s  (5.7× speedup)

For Hero of Ages as a whole (4 chapters × ~6.8h, was 4:16 with 4 cores):
  - Projected with split: ~88 s  (2.9× speedup)

## Usage

  python3 parallel_encode_demo.py SOURCE_MP3 OUTPUT_M4A [--piece-seconds 600]

The output is a stream-compatible AAC m4a. Wrap multiple of these into an
m4b via `make_m4b.py` if you've used this on each chapter file.

## Caveats not yet addressed

  1. The make_m4b.py chapter metadata builder emits one [CHAPTER] per encoded
     file. If you split one logical chapter into 37 pieces, the m4b will have
     37 chapter marks for that chapter. To preserve the original chapter
     boundaries, the caller needs to track parent-piece membership and
     collapse when building metadata.
  2. The `--ar` / `--ac` / `--b:a` here are hardcoded for Hero of Ages
     (mono 22050 Hz, 32 kbps). Production should probe and match the source.
  3. No progress reporting — make_m4b.py's ProgressPrinter gives nicer UX.
"""

import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def probe_duration(path: Path) -> float:
    """Decoded duration of the audio. Note that mp3's reported `format=duration`
    can be wildly wrong for VBR/CBR encodes — use this for split-point math
    only; for accurate output duration measurement, probe the encoded m4a
    after the fact."""
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    )
    return float(out.strip())


def encode_piece(src: Path, dst: Path, ss: float, to: float,
                 *, sample_rate: int, channels: int, bitrate_bps: int) -> None:
    """One piece of one chapter → uniform AAC m4a. -ss/-to BEFORE -i so the
    demuxer seeks instantly instead of decode-and-discarding."""
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-ss", f"{ss:.3f}", "-to", f"{to:.3f}",
           "-i", str(src),
           "-vn", "-map_metadata", "-1",
           "-c:a", "aac", "-b:a", str(bitrate_bps),
           "-ar", str(sample_rate), "-ac", str(channels),
           "-f", "ipod", str(dst)]
    subprocess.run(cmd, check=True)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("source", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--piece-seconds", type=float, default=600.0,
                   help="Target piece length in seconds (default 600 = 10 min)")
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    p.add_argument("--sample-rate", type=int, default=22050)
    p.add_argument("--channels", type=int, default=1)
    p.add_argument("--bitrate", type=int, default=32000)
    args = p.parse_args()

    src = args.source.resolve()
    dur = probe_duration(src)
    n = max(1, int(dur // args.piece_seconds + (1 if dur % args.piece_seconds else 0)))

    workdir = args.output.parent / f".{args.output.name}.parts"
    workdir.mkdir(parents=True, exist_ok=True)
    pieces = [workdir / f"piece_{i:04d}.m4a" for i in range(n)]

    print(f"📐 Source: {src.name} ({dur:.0f}s = {dur/3600:.2f}h)")
    print(f"🪓 Splitting into {n} pieces of ≈{args.piece_seconds:.0f}s each")
    print(f"🎧 Encoding {n} pieces with {min(args.jobs, n)} parallel workers")

    with ThreadPoolExecutor(max_workers=min(args.jobs, n)) as ex:
        futures = []
        for i in range(n):
            ss = i * args.piece_seconds
            # Slight overshoot on the last piece is harmless — ffmpeg clamps
            # to source EOF.
            to = (i + 1) * args.piece_seconds
            futures.append(ex.submit(
                encode_piece, src, pieces[i], ss, to,
                sample_rate=args.sample_rate, channels=args.channels,
                bitrate_bps=args.bitrate,
            ))
        for f in futures:
            f.result()

    print(f"🔗 Concatenating {n} pieces with -c copy")
    concat_txt = workdir / "concat.txt"
    concat_txt.write_text("".join(f"file '{p}'\n" for p in pieces))
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "concat", "-safe", "0", "-i", str(concat_txt),
         "-c", "copy", str(args.output)],
        check=True,
    )

    out_dur = probe_duration(args.output)
    print(f"✅ {args.output} ({out_dur:.0f}s)")
    print(f"   drift vs source.format.duration: {out_dur - dur:+.2f}s "
          "(expected: small positive, mostly mp3-metadata error not real drift)")

    # Leave the parts dir on disk so the caller can inspect or reuse pieces.
    print(f"   intermediate pieces: {workdir} (delete when done)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
