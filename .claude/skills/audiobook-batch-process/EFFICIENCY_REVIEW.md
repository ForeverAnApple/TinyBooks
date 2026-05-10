# Efficiency review: 3 rounds (May 2026 batch)

Notes from processing the May 2026 batch of 10 audiobooks (6 + 2 mid-session).
Each round is one pass over the workflow with a different lens.

---

## Round 1 — what can be MORE EFFICIENT

### Findings (with measured numbers)

**1. Cross-book parallelism when chapters < cores.**
Hero of Ages (4 mp3s for 27.31h) only saturates 4 of 16 cores. Standalone it
took 4:16. Run in parallel with Ringworld (`--jobs 4` + `--jobs 12`), the
two together finished in 4:16 instead of 4:16 + 1:59 = 6:15. **~30% wall-time
savings** on that pair.

**General rule**: when scheduling a batch, look at chapter counts. Run
many-chapter books one at a time (they saturate). Pair few-chapter books with
larger ones, splitting `--jobs` so the sum equals total cores.

**2. Mid-chapter splitting (Regime C2 in SKILL.md) for big-chapter books.**
Prototyped Hero of Ages chapter 1 (6.08h) two ways:
  - Single-thread direct AAC encode: **141.4 s**
  - 37 pieces of ~10 min, all 16 cores, then `-c copy` concat: **24.8 s**

That's a **5.7× speedup** for one chapter. Verified bit-equivalent output
(473825 vs 473795 packets — only 30 packets of AAC priming pre-roll
difference, all in the edit-list-trimmed region).

For Hero of Ages full book (4 chapters → 144 pieces): projected ~88 s total
versus 256 s actual. **2.9× speedup** for the worst-case book.

Demo at `parallel_encode_demo.py` — not yet integrated into `make_m4b.py`
because the chapter-metadata builder needs a parent-piece grouping pass.

**3. Don't bother with `--tmp-dir /dev/shm`.**
I assumed RAM tmpfs would speed up the temp-file I/O. **Benchmark proved
me wrong**: clean controlled run of Ringworld twice with identical params,
disk-tmp vs `/dev/shm`-tmp, results were 1:37.77 vs 1:37.44. **0.3% delta —
within run-to-run noise.** The workload is CPU-bound (AAC encode), not
I/O-bound. The temp .m4a files are written sequentially as encodes finish,
then read once during concat — small data, dwarfed by encode time. Skip the
operational complexity.

**4. Batch all metadata research in one parallel sweep.**
The May batch did this well: one Python script with `urllib.request` made all
8 iTunes Search calls + 8 Lookup calls + 8 cover downloads in one pass
(~3 s total). Doing it serially would have been ~30 s. Apply same to
WebFetch/WebSearch when researching narrators — fire all queries in one
multi-tool-call message.

**5. Run the 4 retags in parallel as background bash jobs.**
Each retag is ~5–25 s of `-c copy` ffmpeg + a tiny mux step. Negligible CPU.
Running 3 in parallel finished them in 9 s walltime instead of 9+9+9 = 27 s.
The Bash `run_in_background: true` pattern is the right tool.

### What I'd do differently next batch

- Implement Regime C2 properly in `make_m4b.py` (add `--split-large` flag).
  The win on books with few-but-huge chapters is significant and the change
  is contained to (a) chapter-list expansion after `build_chapters`, (b)
  parent-piece tracking, (c) chapter-metadata grouping.
- Write a `batch_runner.py` that takes a directory of source dirs and
  emits an optimal schedule (sort by chapter count desc, pair small with
  large) instead of me doing the scheduling by hand.

---

## Round 2 — what can be SIMPLIFIED

### Findings

**1. The `--tmp-dir /dev/shm` thing wasn't an optimization, it was noise.**
Removing the suggestion entirely simplifies the next agent's mental model:
"just let it default."

**2. Stop using `tail -F` in Monitor.**
The `==>` file-switch markers fire as false-positive events even with
strict `grep -E`. Plain `Bash` with `until [ -f output ]; do sleep 5; done`
in `run_in_background: true` mode is simpler and correct.

**3. Year handling is muddled across the workflow.**
There are three plausible years for any audiobook:
  - Original print publication year (Goodreads canonical)
  - Audio production year (Audible / iTunes)
  - "First released" if there's a rerelease

I picked print year for new encodes and audio year (whatever the m4b already
has) for retags. **Pick ONE and document it.** The simplest rule: "Use the
original print publication year. ABS sorts series by it cleanly." Then retags
should override the existing audio year too.

**4. The metadata-summary block in `make_m4b.py` is exactly right.**
Don't simplify it — every field listed is checked by the production-build
guard. The block's value is "here's what you're about to bake in, last chance
to abort." Keep it.

**5. Cover-handling priority chain is good but the third fallback is rare.**
`--cover` > sidecar in dir > extracted-from-source-art. The third fallback
fires for retag-mode m4bs that have low-res embedded art and no sidecar — but
in practice we always grab a hi-res iTunes cover into the source dir before
running, so the embedded-art path basically never triggers. It's still worth
keeping for the "user tossed a single .m4b at me with no other files" case.

### What I'd simplify next batch

- Drop the per-book `description.txt` filename convention I made up
  (`<Title>.description.txt` for loose files vs `description.txt` inside dirs).
  Just always use `description.txt` next to the source — the script's
  `--description-file` resolves the path explicitly anyway.
- Consolidate the "iTunes lookup + cover download + description clean"
  into one helper script instead of three blocks of inline Python.

---

## Round 3 — what can I do BETTER FOR THE NEXT AGENT

### Findings

**1. The CLAUDE.md is already very good.** Specifically, the "What not to do"
list saved me from making the "rename source dir to tidy up" mistake, and
the Y output filename convention table prevented me from inventing my own
naming. The next agent should read CLAUDE.md before doing anything.

**2. Probe BEFORE planning.** I almost started encoding Hero of Ages with
default `--jobs 16` before noticing it has only 4 chapters. The deep-probe
pass in step 2 of the new SKILL.md is mandatory for a reason — the chapter
counts drive the parallelism strategy entirely.

**3. The skill (`audiobook-batch-process`) captures most of the institutional
knowledge.** It has the iTunes URL pattern, the cover branding rules, the
parallelism regimes, the description cleaner, and the CLI invocation
examples. The next agent should be able to follow it end-to-end.

**4. The prototype script is the best handoff for the C2 optimization.**
Rather than me half-implementing `--split-large` in `make_m4b.py`, the
`parallel_encode_demo.py` lets the next agent see the technique working,
verify the bit-equivalent output claim, and decide whether to integrate.
Risk-managed handoff.

**5. Benchmark reference numbers in SKILL.md are gold.** "~10 sec wall time
per hour of source audio" lets the next agent estimate how long a batch will
take before kicking it off and whether it's worth parallelizing further.

### Concrete handoff items

- **Skill**: `.claude/skills/audiobook-batch-process/SKILL.md` — the workflow
- **Demo**: `.claude/skills/audiobook-batch-process/parallel_encode_demo.py` —
  the unintegrated 5.7× speedup
- **Bench data**: this file — the measurements behind the claims
- **Open work**: integrate `--split-large` into `make_m4b.py`. Sketch:
  1. After `build_chapters`, add a `subdivide_oversize_chapters(chapters,
     threshold, jobs)` pass that returns `(expanded_chapters, parent_index)`.
  2. The encoding loop runs unchanged on `expanded_chapters`.
  3. Before `build_metadata`, sum encoded durations by `parent_index` so the
     output has one [CHAPTER] per logical (pre-split) chapter.
  4. Add `--split-large [SECONDS]` flag (default off; user opts in).
  5. Test on Hero of Ages — should match the existing 4-chapter output's
     chapter marks, with ~3× faster wall time.
