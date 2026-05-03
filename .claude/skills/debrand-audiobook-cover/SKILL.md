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

Save this to `<workdir>/prompt.txt`, edited per book. Be specific about what to remove and what to preserve:

```
Use your built-in `image_gen` tool to EDIT input_branded.jpg (attached via -i and present in <workdir>) and produce a clean version with all <retailer> retailer branding removed.

Save the cleaned result as <workdir>/output_clean.jpg (JPEG, match the input's dimensions e.g. 2400x2400).

DO NOT use the CLI fallback (scripts/image_gen.py). DO NOT pip install anything. DO NOT use LaMa, opencv, torch, or any local inpainting library. Use ONLY the built-in `image_gen` tool — it uses Codex's own auth and does not require OPENAI_API_KEY.

Source image: <dimensions> audiobook cover for "<TITLE>" by <AUTHOR>, cover art by <ARTIST_IF_KNOWN>. Branding to remove (these are <retailer>'s overlays, NOT part of the original artwork):

1. <describe each branding element with its position, color, and approximate size>
2. <next branding element>

What MUST be preserved exactly:
- The title text "<TITLE>" (note color/position)
- The author byline "<AUTHOR>" (note position)
- The full underlying artwork: <describe the scene>

What the cleaned cover should look like: a clean publisher-style audiobook cover — same artwork, same title text, same author text, but with empty/extended-background pixels where the branding used to be. As if the publisher made a master that never had retailer branding.

Verify the result by inspecting it visually after generation. If branding text remains, retry with a stricter prompt. Print "SUCCESS: output_clean.jpg ready" when complete, or "FAILED: <reason>" if you can't get a clean result.
```

### 3. Invoke Codex

```sh
codex exec \
  --skip-git-repo-check \
  --sandbox danger-full-access \
  --cd <workdir> \
  -i <workdir>/input_branded.jpg \
  < <workdir>/prompt.txt \
  > <workdir>/codex.log 2>&1
```

Run it in the background (it takes ~1–3 min). The image_gen call itself is fast; the rest is Codex reading its skill docs and reasoning about the image.

### 4. Codex CLI gotchas (learned the hard way)

- **`-i` is variadic.** `codex exec -i img.jpg "prompt text"` will eat the prompt as another image path. Always pass the prompt via stdin redirection (`< prompt.txt`), never as a positional after `-i`.
- **Stdin must be the prompt or `/dev/null`.** If stdin is an empty pipe (e.g. `</dev/null` with no positional prompt), Codex prints `Reading prompt from stdin... No prompt provided via stdin.` and exits 1.
- **Don't pipe stdout through `tail`.** `codex … | tail -200` buffers everything until exit, making the run look frozen. Redirect to a file (`> codex.log 2>&1`) and tail the file separately.
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
