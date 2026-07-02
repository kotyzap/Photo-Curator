# 📸 Photo Curator v6.0

## 📸 Come home with 3,000 photos - Leave with your 50 best ✨

**v6.0** · A local, browser-based tool for culling and ranking large photo libraries. Point it at a folder of JPEGs — or **RAW files** (Canon CR2/CR3, Nikon NEF, Sony ARW, DNG, Fuji RAF, Olympus ORF, Panasonic RW2 and more) — and it walks you through three steps — **drop the blurry ones, collapse burst duplicates, and surface your best shots** — all running entirely on your own machine. Nothing is ever uploaded anywhere.

Built for photographers who come home from a trip with a few thousand frames and want the keepers fast.

![pipeline: Cull → Dedup → Rank](https://img.shields.io/badge/pipeline-Cull%20→%20Dedup%20→%20Rank-blue)
![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue)
![runs 100% local](https://img.shields.io/badge/runs-100%25%20local-16a34a)
![photos never uploaded](https://img.shields.io/badge/photos-never%20uploaded-16a34a)
![RAW support](https://img.shields.io/badge/RAW-CR2%20·%20CR3%20·%20NEF%20·%20ARW%20·%20DNG-8a2be2)

> **RAW support (v6.0):** RAW files are handled via `rawpy` (LibRaw). For speed, the full-size JPEG preview embedded in every RAW is used for thumbnails, analysis and on-screen display — EXIF (date, lens, GPS, orientation) comes along with it. Files with no usable preview fall back to a half-size demosaic. Exports always copy the **original RAW file**, untouched. If `rawpy` isn't installed, the app simply ignores RAW files and works as before.
![telemetry: none](https://img.shields.io/badge/telemetry-none-16a34a)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

> 💛 **Photo Curator is free and open source.** If it saved you an evening of culling, please consider [buying me a Ko-Fi](https://ko-fi.com/B3S720JCU6) — it directly funds new features.

---

## Contents

- [Why Photo Curator](#why-photo-curator)
- [What's new in v6.0](#whats-new-in-v60)
- [Features](#features)
- [Install & Run](#install--run)
- [Workflow](#workflow)
- [How it works](#how-it-works)
- [Privacy & Safety](#privacy--safety)
- [Platform notes](#platform-notes)
- [License](#license)

## Why Photo Curator

A long shoot leaves you with thousands of near-identical frames, blurred misfires, and a handful of genuine keepers buried in the middle. Going through them by hand is slow and easy to get wrong. Photo Curator does the first ruthless pass for you — in seconds per hundred photos — and **leaves every decision reversible**. Nothing is deleted or moved until you say so, and your files never leave your computer.

## What's new in v6.0

- **RAW files, first-class.** Canon CR2/CR3, Nikon NEF, Sony ARW, Adobe DNG, Fuji RAF, Olympus ORF, Panasonic RW2, Pentax PEF and more are culled, deduped and ranked alongside JPEGs (via `rawpy`/LibRaw — see the note above for how it stays fast).
- **RAW / JPG filter chips in Cull.** Shooting RAW+JPG? Filter the grid to **RAW only** or **JPG only**; RAW frames carry a purple **RAW** tag.
- **Originals untouched.** Exports always copy the original RAW file, never a converted preview. If `rawpy` isn't installed, a sidebar banner tells you RAW support is off.

### Earlier (v4.0)

- **One-click app launcher.** A proper `Photo Curator.app` for macOS — double-click the camera icon and your browser opens automatically once the engine is ready. No Terminal window, no commands. Quit it from the Dock and the engine stops cleanly.
- **Fully offline bundle for Apple Silicon.** A self-contained package (all image libraries included) that runs on an M-series Mac with no pip, no terminal, and no internet at run time.
- **Moved off port 5000.** The app now serves on **port 5014** (`http://127.0.0.1:5014`). macOS reserves port 5000 for its AirPlay Receiver, which answers every request with `HTTP 403` — that conflict is now gone. Override with the `PHOTOCURATOR_PORT` environment variable if you ever need to.

## Features

- **1 · Cull** — flags out-of-focus shots using a *contrast-normalized* sharpness measure, so genuinely soft frames are caught while low-contrast-but-sharp shots (haze, night, big skies) are kept. Sorts into **Sharp / Soft (recoverable) / Blurry**, with a one-click **Sharp ⇄ Blurry** override on every photo. Blurry shots move to `Blurred/` only when you press **Move blurry** — review first, move second.

- **2 · Dedup** — global perceptual-hash clustering collapses burst sequences to a single frame. EXIF capture-time tightens burst detection, ORB feature-matching prevents distinct scenes from being wrongly merged, and the **sharpest** frame of each group is kept and labelled **"Best of N"**. Frames with no near-duplicate are labelled **"Original"**. Matching is vectorized and signatures are cached, so big cards stay fast.

- **3 · Rank** — scores each photo on composition, lighting, focus, color, and contrast, then shows your **TOP N** with a per-photo hexagonal radar chart and a TOP-N average "metric profile". Ranking shows live per-photo progress with **percentage, elapsed time, and ETA**. If you skip Dedup, ranking folds the clustering in automatically so a one-click run still gives a burst-free result.

- **📱 Phone Background selector** — a single tap marks any top shot as a phone wallpaper. **Export Phone BG** writes a `PhoneBG/` folder with `Original/` (full-res copies) and `Wallpaper_19.5x9/` (each photo center-cropped to **1290×2796, 19.5:9** — pixel-perfect on iPhone and covering nearly all Android).

- **⚡ God Mode** — one button runs the whole pipeline automatically: **Cull → Dedup → Rank**, landing on your ranked TOP N *without moving any files*, so you still review and move rejects yourself.

- **Built for big libraries** — live preview with pagination, per-stage progress with ETA, EXIF-orientation-correct thumbnails, light/dark theme, a lightbox with arrow-key review, and optional auto-move of rejects into `Blurred/`, `Duplicates/`, and `TOP_N/` subfolders.

## Install & Run

### Option A — One-click app (macOS, recommended)

Double-click **`Photo Curator.app`**. Your browser opens at `http://127.0.0.1:5014` once it's ready.

The first time, macOS may say the app is from an unidentified developer — right-click (Control-click) the app → **Open** → **Open**. After that, a normal double-click works. To quit, right-click the Dock icon → **Quit**.

### Option B — Run from source

Requires **Python 3.9+**.

```bash
pip install -r requirements.txt
python photo_curator.py
```

Then open <http://127.0.0.1:5014> in your browser. Pick a folder (or paste a path), choose a step, and press **Start**.

## Workflow

A typical pass on a full card is **Cull → Dedup → Rank** in order — each step feeds its survivors to the next, so ranking only scores the photos worth scoring. In a hurry? Press **⚡ God Mode** to run all three automatically. Either way, **no files are deleted or moved until you explicitly choose to** — every stage is review-first.

## How it works

| Step | Metric | Notes |
|------|--------|-------|
| Cull | `var(Laplacian) / var(image)` on a 1024px copy | Resolution-independent; normalizes out contrast so haze ≠ blur. Threshold is adjustable. |
| Dedup | 192-bit perceptual hash (avg + dual difference hash) + ORB confirm | Global clustering; EXIF-timed bursts get a relaxed bar; keeps the sharpest frame. |
| Rank | Weighted focus / lighting / contrast / color / composition | Per-photo radar + TOP-N average profile. |

Thumbnails are cached under your system temp dir, so the first pass over a folder is the only slow one.

## Privacy & Safety

Photo Curator is private by default — not as a policy, but by architecture.

- **Runs 100% on your machine.** All analysis (sharpness, duplicate detection, ranking) happens locally in Python. Your photos never leave your computer.
- **No uploads, ever.** There is no cloud, no server, no storage bucket. The only network socket the app opens is a local web server bound to `127.0.0.1` (localhost) so your browser can talk to it — it is not reachable from your network or the internet.
- **No accounts, no sign-in.** Nothing to register, no email required.
- **No telemetry or analytics.** The app collects nothing, phones home to nothing, and has no third-party trackers. You can verify this — the source is open.
- **Nothing is deleted or moved without your say-so.** Every stage is review-first. Rejects are only relocated into `Blurred/`, `Duplicates/`, or `TOP_N/` subfolders when you explicitly press the button — and the originals stay on disk.
- **Works fully offline.** Once installed, it needs no internet connection to run.

Because it's open source, you don't have to take our word for any of this — read the code.

## Platform notes

Tested on **macOS** (Apple Silicon and Intel). The native folder picker uses `osascript` and SD-card detection scans `/Volumes`; on Windows/Linux those conveniences are skipped, but you can still paste a folder path into the field and everything else works. The app serves on **port 5014** to avoid the macOS AirPlay Receiver on port 5000.

## License

Photo Curator is released under the [MIT License](LICENSE) — free to use, modify, and distribute, including commercially. It is provided as-is, with no warranty.

> 💛 If it saved you time, the best way to say thanks is a [Ko-Fi](https://ko-fi.com/B3S720JCU6) — every coffee genuinely helps fund the next feature. Thank you! 🙏
