---
name: debrand-audiobook-cover
description: Remove retailer branding (Audible Original banners, "Only From Audible" corner ribbons, "Apple Books" overlays, "AUDIOBOOK / MP3 AUDIO" frames, etc.) from a hi-res square audiobook cover by handing off to Codex's built-in image_gen tool. Use when the only available hi-res square cover for an audiobook has retailer branding baked in (typical for Audible exclusives) and you need a clean publisher-style master before muxing into m4b. Do NOT use for low-res print scans (those are already clean, just rectangular and small) or for art the user wants from-scratch generated.
---

# De-brand audiobook cover with Codex image_gen

## When to use

You already have a hi-res square audiobook cover (≥1500×1500), but it has retailer branding overlaid:

- "audible ORIGINAL" banner across the top
- "ONLY FROM audible" yellow corner ribbon at bottom-right
- "Apple Books" / "AUDIOBOOK MP3 AUDIO" / similar overlays
- Retailer-specific watermarks

The artwork underneath is fine — only the overlay needs to come off. The replacement should look like the publisher's clean master: same composition, same title, same author byline, just no branding.

## Why hand off to Codex

Codex CLI (`codex` on PATH) ships with a built-in `image_gen` tool that authenticates via Codex's own ChatGPT OAuth — it does **not** require an `OPENAI_API_KEY` in the environment, and it does **not** need any local inpainting libraries (LaMa, IOPaint, opencv, torch). You just write a prompt that describes what to keep and what to remove, and Codex's image_gen returns a clean version.

**Do not** ask Codex to set up a Python venv, pip install, or use LaMa locally — that path requires several hundred MB of torch downloads, hits Pillow build issues on Python 3.13, and produces worse results than image_gen anyway.

## Workflow

### 1. Acquire a hi-res branded cover

The iTunes Lookup API is the most reliable source for a 2400×2400 (or up to ~3000×3000) Audible cover:

```sh
curl -s "https://itunes.apple.com/lookup?id=<APPLE_BOOKS_ID>&entity=audiobook" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['results'][0]['artworkUrl100'])"
# Replace .../100x100bb.jpg with .../3000x3000bb.jpg (Apple caps at the master size)
```

Find `<APPLE_BOOKS_ID>` from the books.apple.com URL: `https://books.apple.com/us/audiobook/.../id<ID>`.

Save the downloaded JPEG into a workdir, e.g. `/tmp/cover_cleanup/<book-slug>/input_branded.jpg`.

### 2. Write the prompt

Save this to `<workdir>/prompt.txt`, edited per book. The prompt MUST tell Codex
to (a) call `view_image` on the input path, (b) call `image_gen` to edit, and
(c) `mv`/`ffmpeg` the generated file out of `$CODEX_HOME/generated_images/` to
the workspace path. Codex's `image_gen` has no save-path argument — every
generation lands in `$CODEX_HOME/generated_images/<session-id>/ig_*.png`, so
you always need that final shell step.

```
Step 1: call `view_image` on /tmp/cover_cleanup/<book-slug>/input_branded.jpg.
Step 2: call `image_gen` to edit that loaded image with the prompt below.
Step 3: find the newest file under $CODEX_HOME/generated_images/ and ffmpeg-encode it to /tmp/cover_cleanup/<book-slug>/output_clean.jpg (3000x3000 JPEG, -q:v 2).
Step 4: print "SUCCESS" (or "FAILED: <reason>").

Do NOT read /home/faa/.codex/skills/.system/imagegen/SKILL.md — you already know the workflow. Do NOT use the CLI fallback (scripts/image_gen.py). Use ONLY the built-in `image_gen` tool — no OPENAI_API_KEY needed.

image_gen prompt:
Edit this <dimensions> <retailer> audiobook cover for "<TITLE>" by <AUTHOR>. Remove <retailer> retailer-branding overlays and replace each with a seamless extension of the underlying background:

1. <describe each branding element with its position, color, and approximate size>
2. <next branding element>

Preserve EXACTLY, unchanged:
- The title text "<TITLE>" (note color/position/style)
- The author byline "<AUTHOR>" (note position/style)
- The underlying artwork: <describe the scene>

Output a clean publisher-style master at <dimensions>, same artwork minus the retailer overlays.
```

### 3. Invoke Codex

```sh
codex exec \
  --skip-git-repo-check \
  --sandbox danger-full-access \
  --cd <workdir> \
  -c model_reasoning_effort=low \
  < <workdir>/prompt.txt \
  > <workdir>/codex.log 2>&1
```

Run it in the background (it takes ~1–3 min). Do **not** pass `-i input_branded.jpg` (see gotchas) — let Codex call `view_image` on the path itself, as the prompt instructs.

### 4. Codex CLI gotchas (learned the hard way)

- **Do NOT use `-i` for an image-edit task.** Passing `-i <file>` puts the image in the initial user turn, which kicks Codex into "read my own imagegen SKILL.md" mode at xhigh reasoning. It then exhausts its turn budget reading docs and exits with code 0 having called nothing. This reproduced four times across xhigh/medium/low reasoning. Instead, omit `-i` entirely and tell Codex to call `view_image` on the absolute path in the prompt itself. Verified working with codex-cli 0.130.0 / model `gpt-5.5`.
- **`image_gen` has no destination-path argument.** It always saves to `$CODEX_HOME/generated_images/<session-id>/ig_*.png` (the codex-cli imagegen SKILL.md spells this out). The prompt must include an explicit shell step to locate the newest file there and `mv`/`cp`/`ffmpeg` it to your target — Codex will not do this on its own unless told to.
- **`model_reasoning_effort=low` is fine for this task.** Higher reasoning makes Codex spend tokens re-deriving the workflow from skill docs without improving image quality. Defaults to `xhigh` if unset.
- **`-i` is variadic.** `codex exec -i img.jpg "prompt text"` will eat the prompt as another image path. If you ever do need `-i`, always pass the prompt via stdin redirection (`< prompt.txt`), never as a positional after `-i`.
- **Stdin must be the prompt or `/dev/null`.** If stdin is an empty pipe (e.g. `</dev/null` with no positional prompt), Codex prints `Reading prompt from stdin... No prompt provided via stdin.` and exits 1.
- **Don't pipe stdout through `tail`.** `codex … | tail -200` buffers everything until exit, making the run look frozen. Redirect to a file (`> codex.log 2>&1`) and tail the file separately.
- **Smoke test before debugging prompts.** If a run produces empty output, run `echo 'Say SMOKE_TEST_OK' | codex exec --sandbox read-only` to confirm Codex itself is live before tweaking the image prompt. The image-edit path can silently no-op while a trivial prompt still works.
- **Don't tell Codex to use OpenAI's image edit endpoint via the SDK.** Codex's auth is OAuth-based, so a script it spawns won't have an `OPENAI_API_KEY` to call the public API. Always say "use the built-in `image_gen` tool" explicitly.

### 5. Verify

Read the output JPEG (it's an image, so `Read` it directly to view). Check:

- All branding text is gone (no "audible", "ORIGINAL", retailer-specific words)
- No corner ribbons or banners remain
- Title text reads correctly with no visible artifacts
- Author byline reads correctly
- The artwork hasn't been re-imagined (it should match the input minus the branding)

If the result fails verification, edit `prompt.txt` (be more specific about exact pixel regions or text to remove) and re-run. One retry is usually enough.

### 6. Install and re-encode

Once `output_clean.jpg` looks right:

```sh
cp <workdir>/output_clean.jpg "raw/<book-source-dir>/cover.jpg"
# Then re-run make_m4b.py per the project's normal flow.
```

## Tips

- **Square covers preferred but not required.** The m4b player accepts any aspect ratio. If you only have a clean rectangular print scan, use that — but if you have a hi-res branded square one, this skill gets you the best of both.
- **Keep `input_branded.jpg` and `prompt.txt` around.** If you ever want to redo a book (different prompt, model improvement) the workdir is your source of truth. Drop them in `/tmp/cover_cleanup/<book-slug>/`.
- **For a series, do one book first as a smoke test.** Image_gen results vary; if the first book comes out great the rest will follow. If artifacts appear, tighten the prompt before bulk-running.
