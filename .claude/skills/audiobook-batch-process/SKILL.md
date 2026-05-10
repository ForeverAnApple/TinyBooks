# Audiobook batch process

End-to-end workflow for processing N new audiobooks in `raw/` into properly
tagged `processed/<Title>.m4b` files using `make_m4b.py`. Follow this when the
user adds a batch of new audiobooks (mp3 dirs and/or loose .m4b files) and
asks you to "process them all."

## When to use

- New items appear in `raw/` (the user usually says "after X" or "the latest X
  books")
- You need cover art, descriptions, narrators, year, genre for each
- You want them in `processed/` with the canonical filename + tags

Skip this and just call `make_m4b.py` directly when there's only one book or
the user is iterating on metadata for a single existing output.

## The 8-step pipeline

### 1. Inventory — quick `/bin/ls -lt raw/` to anchor mtimes

The user will say "the books added after X" or "the latest N." Confirm you're
looking at the same set:

```sh
/bin/ls -lt /mnt/alpha-oguri/Torrents/Audiobooks/raw/ | head -15
```

Note `eza` (the default `ls` here) doesn't accept `-t`; use `/bin/ls`.

### 2. Deep probe each item in parallel

For each new item, determine source layout in **one parallel batch** of Bash
calls:

- Directory of mp3s → multi-file encode mode
- Single .mp3 → encode mode (single file)
- `<Title>.m4b` → **retag mode** (just remux + new metadata)
- Directory containing one .m4b → retag mode on that .m4b

For **encode-mode** sources, also probe:
```sh
ffprobe -v error -select_streams a:0 \
  -show_entries "stream=channels,sample_rate,bit_rate:format=duration" \
  -of compact "$file"
```

Record the **chapter count** (number of mp3 files) and **total duration** —
these drive the parallelism strategy in step 5.

For **retag-mode** sources, also probe existing tags + cover dimensions:
```sh
ffprobe -v error -show_entries format_tags -of default "$file"
ffprobe -v error -select_streams v:0 \
  -show_entries "stream=width,height" -of compact "$file"
```

Most existing m4bs ship with a 500×500 embedded cover — you'll want to upgrade
to 3000×3000 from the iTunes Lookup API in step 4.

### 3. Set up TaskCreate with one task per output

For 8 books you'll have 8+ output tasks plus probe/research/cover/skill tasks.
This makes parallelism and progress visible.

### 4. Metadata research — iTunes Lookup API in one batch

iTunes is the **fastest reliable single source** for: hi-res cover, audio year,
genre, description. Narrator usually requires WebSearch (it's not in the
iTunes API for most audiobooks).

```sh
# Batch search all books at once — collect collectionId for each
for q in "Title+Author" "Title2+Author2" ...; do
  curl -s "https://itunes.apple.com/search?term=${q}&entity=audiobook&limit=3" \
    > "/tmp/audiobook_lookup/${q//+/_}.json"
done

# Then lookup full metadata for each chosen ID
curl -s "https://itunes.apple.com/lookup?id=${ID}&entity=audiobook" > lookup_${ID}.json
```

The lookup payload's `artworkUrl100` ends in `100x100bb.jpg`. Replace with
`3000x3000bb.jpg` to get the master (works ~95% of the time; some Audible-UK
exclusives cap at 1400×1400).

```python
art100 = lookup["artworkUrl100"]
hires = art100.replace("100x100bb.jpg", "3000x3000bb.jpg")
# fall back to 2400x2400bb.jpg, then art100, if the 3000 returns < 5KB
```

The `description` field is HTML — strip `<br/>` `<p>` `<i>` `<b>` and unescape
entities before saving. See the helper in step 6.

For **narrators** missing from iTunes, WebSearch is fast — most series have one
narrator across all books (e.g., Mistborn × 3 = Michael Kramer, Bobiverse =
Ray Porter, Old Man's War 1–4 = William Dufris).

### 5. Parallelism strategy

The single most important optimization: **don't waste idle cores**. There are
three regimes:

#### Regime A: All retags (fast, I/O-bound)
Run all retags in parallel as `run_in_background: true` Bash calls. Each retag
is `ffmpeg -c copy` — a few seconds of disk I/O, negligible CPU. 4 retags
finish in ~25 seconds total walltime, not 4× one retag.

#### Regime B: One encode at a time (CPU-bound, evenly distributed source)
For multi-file sources where chapters ≤ jobs (16 cores) the script saturates
all cores. Run encodes sequentially.

Approximate throughput: **~10 sec wall time per hour of source audio** with
default settings. So a 25h audiobook encodes in ~4 minutes.

#### Regime C: Big chapters / few files (under-utilization)
Hero of Ages has 4 mp3 files for 27.31h of audio. Default `make_m4b.py` only
uses 4 of 16 cores. **Two ways to recover the idle cores:**

**C1 — Cross-book parallelism**: while encoding a low-chapter book, run a
high-chapter book in parallel:
```sh
# Mistborn 3 has 4 chapters → it'll only use 4 workers
python3 make_m4b.py "Book 03..." --jobs 4 ... &
# Ringworld has 25 chapters → give it the remaining 12
python3 make_m4b.py "01 Ringworld" --jobs 12 ... &
wait
```
This is the **safe, no-script-change win**. Used in the May 2026 batch to
finish Mistborn 3 + Ringworld in ~5 min instead of ~6.

**C2 — Mid-chapter splitting (proven 2.9× on Hero of Ages)**: pre-split big
mp3s into ~10-min pieces using ffmpeg's demuxer-level seek (`-ss/-to` BEFORE
`-i`), encode all pieces in parallel, concat with `-c copy`. The resulting
m4a is **byte-equivalent to a direct single-thread encode** (verified — only
30 packet difference over 473k packets, all in the AAC priming pre-roll which
the edit list trims). See `parallel_encode_demo.py` (sibling of this skill)
for the working prototype. Cost: chapter markers count split pieces, so a
4-chapter book becomes a 144-chapter book unless the chapter-build code is
also patched. **Not yet integrated into `make_m4b.py`** — opportunity for the
next agent.

### 6. Description file helper

Most descriptions need HTML cleanup before they read well in CLI output and
audiobook player UIs:

```python
import json, html, re

def clean_itunes_description(s: str) -> str:
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.I)
    s = re.sub(r'</p>', '\n\n', s, flags=re.I)
    s = re.sub(r'<[^>]+>', '', s)
    s = html.unescape(s)
    return re.sub(r'\n{3,}', '\n\n', s).strip()
```

Save the cleaned description as a sidecar `.txt` next to the source and pass
via `--description-file`. CLAUDE.md mandates this for paragraph-heavy blurbs
(the CLI doesn't render multi-line `--description` cleanly).

### 7. Cover handling

**Acceptable badges (publisher-side)**: "Hugo Award Winner", "Sci Fi Essential
Book", "Sequel to ...", "from the author of ..." — these are publisher
marketing badges, not retailer branding.

**Forbidden (retailer-side)**: "ONLY FROM AUDIBLE" yellow ribbon, "Audible
Original" banner, "Apple Books" overlay, "AUDIOBOOK / MP3 AUDIO" frame.

If you hit a retailer-branded cover (typical for Audible UK exclusives like
Children of Time), invoke the `debrand-audiobook-cover` skill — it spawns
Codex with `image_gen` to clean the artwork. ~30-90 second turnaround.

### 8. Output naming + invocation

Per CLAUDE.md, processed/ outputs follow:

- Standalone: `<Title>.m4b`
- Series: `<Title> - <Series>, Book <N>.m4b`

Always pass `--output processed/<canonical>.m4b` explicitly — the script's
default uses the source folder name, which is usually messy.

Required flags for production (script will refuse without them):
- `--title`, `--author`, `--narrator`, `--year`, `--genre`,
  `--description-file`, `--cover`
- `--series` + `--series-part` (when applicable; populates album/grouping)

## Performance reference (May 2026 batch, 16-core machine)

| Operation                          | Throughput              |
| ---------------------------------- | ----------------------- |
| Retag (`-c copy` remux)            | ~0.5 sec/hr-of-audio    |
| Encode, all 16 cores saturated     | ~10 sec/hr-of-audio     |
| Encode, only N cores active        | ~10 × (16/N) sec/hr     |
| Encode w/ split-large parallelism  | ~5 sec/hr-of-audio (proto, not yet integrated) |

Disk vs `/dev/shm` for `--tmp-dir` benchmarked at **0.3% delta** — within
noise. The earlier guess that tmpfs would help was wrong; the workload is
CPU-bound, not I/O-bound. **Don't bother with `--tmp-dir /dev/shm`** for
single-book runs; it just adds an operational gotcha (limited size, not
persistent).

## Anti-patterns

- **Don't use `tail -F` in a Monitor** to watch encode logs — its `==>` file
  switch markers fire false-positive events. Use `Bash` with an `until`
  predicate instead, or grep the log directly with no `tail -F`.
- **Don't pass `--year` mixed across a batch** — pick a convention (original
  print year vs audio release year) and stick to it. The May 2026 batch used
  original print year for new encodes and kept the existing m4b's date for
  retags.
- **Don't `cat` paths with spaces unquoted** — the user's filenames have
  spaces, apostrophes, and parens. Quote everything.
- **Don't write descriptions inline as `--description "..."`** — multi-paragraph
  text reads poorly through shell escaping. Always use `--description-file`.
- **Don't fetch covers serially** — batch the iTunes search calls and the JPEG
  downloads in one Python script. Saves real wall time.

## Reference: working invocation patterns

```sh
# Multi-file encode (mp3 directory)
python3 make_m4b.py "raw/Book Dir Name" \
  --output "processed/Title - Series, Book N.m4b" \
  --title "Title" --author "Author" --narrator "Narrator" \
  --year YYYY --genre "Science Fiction" \
  --description-file "raw/Book Dir Name/description.txt" \
  --cover "raw/Book Dir Name/cover.jpg" \
  --series "Series" --series-part N

# Retag (existing .m4b)
python3 make_m4b.py "raw/Existing.m4b" \
  --output "processed/Title - Series, Book N.m4b" \
  --title "..." --author "..." --narrator "..." \
  --year YYYY --genre "Science Fiction" \
  --description-file "raw/Title.description.txt" \
  --cover "raw/Title.cover.jpg" \
  --series "Series" --series-part N
```
