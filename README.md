# 📸 Photo Curator

<img width="1503" height="744" alt="Photo-Curator-header" src="https://github.com/user-attachments/assets/7c3a22ff-b035-4feb-8d13-07f26dbf1879" />
<img width="2752" height="1536" alt="Automated_Photo_Culling_Workflow" src="https://github.com/user-attachments/assets/d6092dc5-b13e-469f-bdcb-9f03c7c7d223" />

**v7.0** · A local, browser-based tool for culling and ranking large photo libraries. Point it at a folder of **JPEG, PNG, HEIC or RAW** files (Canon CR2/CR3, Nikon NEF, Sony ARW, DNG and more) — and it walks you through three steps — **drop the blurry ones, collapse burst duplicates, and surface your best shots** — all running entirely on your own machine. Nothing is ever uploaded anywhere.

Built for photographers who come home from a trip with a few thousand frames and want the keepers fast.

<p align="center">
  <img src="https://img.shields.io/badge/pipeline-Cull%20→%20Dedup%20→%20Rank-blue" alt="pipeline: Cull → Dedup → Rank">
  <img src="https://img.shields.io/badge/RAW-CR2%20·%20CR3%20·%20NEF%20·%20ARW%20·%20DNG%20%2B%20more-8a2be2" alt="RAW support">
  <img src="https://img.shields.io/badge/HEIC-iPhone%20·%20HEIF%20·%20HIF-0d9488" alt="HEIC support">
  <img src="https://img.shields.io/badge/python-3.9+-blue" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/runs-100%25%20local-16a34a" alt="100% local">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <a href="https://ko-fi.com/B3S720JCU6"><img src="https://img.shields.io/badge/☕%20Support-Ko--Fi-FF5E5B" alt="Support on Ko-Fi"></a>
</p>

> 💛 **Photo Curator is free and open source.** If it saved you an evening of culling, please [**buy me a Ko-Fi**](https://ko-fi.com/B3S720JCU6) — it directly funds new features.

---

## Contents

- [Why Photo Curator](#why-photo-curator)
- [What's new in v7.0 — HEIC / iPhone photos](#-whats-new-in-v70--heic--iphone-photos)
- [RAW support (since v6.0)](#raw-support-since-v60)
- [Features](#photo-curator-features)
- [Install](#install)
- [Run](#run)
- [Workflow](#workflow)
- [How it works](#how-it-works)
- [Platform notes](#platform-notes)
- [Support the project](#-support-the-project)
- [License](#license)

## Why Photo Curator

A long shoot leaves you with thousands of near-identical frames, blurred misfires, and a handful of genuine keepers buried in the middle. Going through them by hand is slow and easy to get wrong. Photo Curator does the first ruthless pass for you — in seconds per hundred photos — and **leaves every decision reversible**. Nothing is deleted or moved until you say so, and your files never leave your computer.

## 🆕 What's new in v7.0 — HEIC / iPhone photos

Photo Curator now reads **HEIC/HEIF** straight from an iPhone import — no conversion step, no detour through Photos:

- **Formats** — `.heic`, `.heif` and `.hif` (iPhone and Android stills, plus Canon/Sony HEIF), decoded by [`pillow-heif`](https://pypi.org/project/pillow-heif/) (libheif). EXIF — date, lens, GPS, orientation — comes through exactly as it does for JPEG, so the map view and burst timing work unchanged.
- **A HEIC filter chip** in Cull sits next to *All types · RAW only · JPG only*, and every HEIC card carries a teal **HEIC** tag.
- **Browser-safe display** — only Safari renders HEIC natively, so the full-size view is served as a transcoded JPEG (cached, same as RAW previews). Your `.heic` files are never modified, and exports always copy the untouched original.
- **Mixed folders just work** — a card or folder holding JPEG + HEIC + RAW is culled, deduped and ranked in one pass, and every card is tagged with its real format (HEIC, CR2, PNG, TIFF…).

**Full format list:** JPEG · PNG · HEIC / HEIF / HIF · TIFF · BMP · WebP · every RAW format LibRaw reads. PNG, TIFF and BMP carry no EXIF, so those frames get no capture time or map pin — dedup falls back to hash-only clustering for them, everything else works the same.
- Bundled in the offline packages. Running from source? It's in `requirements.txt`; without it the app keeps working and says so in the sidebar.

## RAW support (since v6.0)

Photo Curator also curates your **RAW files** alongside JPEGs:

- **Formats** — Canon **CR2/CR3**, Nikon NEF, Sony ARW, Adobe DNG, Fuji RAF, Olympus ORF, Panasonic RW2, Pentax PEF and more (anything LibRaw reads).
- **Fast by design** — instead of demosaicing every file, the full-size JPEG preview your camera embeds in each RAW is used for thumbnails, analysis and on-screen display. EXIF (date, lens, GPS, orientation) comes along with it. Files without a usable preview fall back to a half-size RAW develop.
- **Shoot RAW+JPG?** Two tools keep pairs under control:
  - **Cull filter chips** — view **All types · RAW only · JPG only**; every card carries a **RAW** (purple) or **JPG** (gray) tag. The filter carries into Dedup and Rank, and the app tells you when only one format continues.
  - **RAW+JPG pair setting** (Dedup panel) — collapse same-frame pairs (`IMG_0001.CR2` + `IMG_0001.JPG`) to one file before deduping: keep both, keep RAW, or keep JPG. Applies in Rank too, so no more duplicate keepers.
- **Originals stay originals** — exports always copy the untouched RAW file, never a converted preview.
- Powered by [`rawpy`](https://pypi.org/project/rawpy/) (LibRaw). It's in `requirements.txt`; without it the app keeps working JPEG-only and says so in the sidebar.

## Photo Curator Features

## 1. Cull
- **1 · Cull** — flags out-of-focus shots using a *contrast-normalized* sharpness measure, so genuinely soft frames are caught while low-contrast-but-sharp shots (haze, night, big skies) are kept. Sorts into **Sharp / Soft (recoverable) / Blurry**, with a one-click tier toggle on the badge of every photo. Filter by **RAW / JPG** when you shoot both. Blurry shots move to `Blurred/` only when you press **Move blurry** — review first, move second.

<div align="center"><img width="800" height="450" alt="PhotoCuratorv3 4-ezgif com-video-to-gif-converter" src="https://github.com/user-attachments/assets/aa7b4452-f6ba-497c-8ed6-b31748a7e068" /></div>

## 2. Dedup
- **2 · Dedup** — global perceptual-hash clustering collapses burst sequences to a single frame. EXIF capture-time tightens burst detection, ORB feature-matching prevents distinct scenes from being wrongly merged, and the **sharpest** frame of each group is kept and labelled **"Best of N"** (so you can see how many near-duplicates it stood in for). Frames with no near-duplicate are labelled **"Original"**. The **RAW+JPG pairs** setting collapses same-frame format pairs before clustering. Matching is vectorized and signatures are cached, so big cards stay fast.

<img width="2157" height="963" alt="Dedup-Japan" src="https://github.com/user-attachments/assets/a7a7eb2d-f68e-4ec0-9b0b-efc50769c0b2" />

## 3. Rank and find TOP Photos
- **3 · Rank** — scores each photo on composition, lighting, focus, color, and contrast, then shows your **TOP N** with a per-photo hexagonal radar chart and a TOP-N average "metric profile". Ranking shows live per-photo progress with **percentage, elapsed time, and ETA**. If you skip Dedup, ranking folds the clustering in automatically so a one-click run still gives a burst-free result.
<div align="center">
<img width="800" height="450" alt="PhotoCuratorv3 4-ezgif com-video-to-gif-converter (2)" src="https://github.com/user-attachments/assets/1a5a205a-9358-4422-9da4-c1d1e64e3416" />
</div>

<img width="2855" height="1866" alt="Rank view" src="https://github.com/user-attachments/assets/bc403303-2edf-4a48-9f25-27502627416f" />

<img width="3550" height="1497" alt="SCR-20260531-sxoh" src="https://github.com/user-attachments/assets/f63464b1-02d2-4bb6-8db1-72b4c861f5b5" />


<img width="2139" height="954" alt="Rank radar" src="https://github.com/user-attachments/assets/2499032b-35da-4823-8d01-0efee29c7b58" />

## 📱 Phone Background selector - (new since v3.5)
- *(new since v3.5)* — being in the TOP N already vouches for a photo's quality, so a single tap marks any top shot as a phone wallpaper. Each ranked card and the lightbox get a **📱 toggle** (press **B** in the lightbox), a **Phone BG** filter chip shows just the ones you picked, and **Export Phone BG** writes a `PhoneBG/` folder with two subfolders: `Original/` (full-res copies) and `Wallpaper_19.5x9/` (each photo center-cropped and resized to **1290×2796, 19.5:9**). That ratio is pixel-perfect on iPhones and, because phones zoom wallpapers to fill, covers nearly all Android (20:9) too — one universal crop, no device picker.

<div align="center"><img width="75%" alt="Phones-PhotoCurator-BG" src="https://github.com/user-attachments/assets/f6652bbd-575c-4500-9234-c78b7601e085" /></div>

## 📍 Photo EXIF & Location Data - (new since v3.7)

(new since v3.7) — full photo context in one glance. The lightbox Details panel now shows EXIF info (camera, lens, aperture, shutter, ISO), date & time, and an interactive map view with exact coordinates showing where each shot was taken — for RAW files too, read from the camera's embedded preview. Browse by place with the 📍 Location filter chip. Perfect for travel curation — instantly map your top-ranked images and remember where you captured each golden moment.

<div align="center">
<img width="1626" height="950" alt="Screenshot 2026-06-01 at 12 44 07" src="https://github.com/user-attachments/assets/a1b0dc08-fd82-4c4f-844a-9e65e6f8bc3f" />
</div>

## ⚡️God Mode

- **⚡ God Mode** — one button runs the whole pipeline automatically: **Cull → Dedup → Rank**, advancing through each stage and landing on your ranked TOP N. It produces the ranking *without moving any files*, so you still review and move rejects yourself.

- **Built for big libraries** — live preview (newest first) with pagination for huge sets, per-stage progress with elapsed time and **ETA**, EXIF-orientation-correct thumbnails, light/dark theme, a lightbox with arrow-key review, and optional auto-move of rejects into `Blurred/`, `Duplicates/`, and `TOP_N/` subfolders.

<img width="2151" height="953" alt="Library view" src="https://github.com/user-attachments/assets/d42867cb-1b99-4966-8e5e-1693e97a1c25" />

## Install

### 📦 Offline package for Mac (recommended)

No Python, no terminal, no internet needed — everything is bundled (RAW and HEIC support included).

➡️ **[Download · Apple Silicon (Proton Drive)](https://drive.proton.me/urls/E3KZNWRZRC#keyO4AK7vjMA)** — ~88 MB · M1–M6 · macOS 11+

Unzip, keep the **PhotoCurator** folder together, right-click **"Start Photo Curator.command"** → **Open** (first time only) — your browser opens automatically.

### 📦 Offline package for Windows

Self-contained Python 3.11 and every library included — nothing is installed into Windows.

➡️ **[Download · Windows x64](TODO-WINDOWS-DOWNLOAD-URL)** — ~95 MB · Windows 10 / 11 (64-bit)

Unzip the folder, keep it together, double-click **"Start Photo Curator.bat"**. SmartScreen may ask once: *More info → Run anyway*.

### 🛠️ Run from source

Requires **Python 3.9+**.

```bash
pip install -r requirements.txt
```

> RAW support comes from `rawpy` and HEIC support from `pillow-heif` (both in `requirements.txt`). Installing on an older setup? Run `pip install rawpy pillow-heif`. Without either one Photo Curator keeps working on the remaining formats and shows a notice in the sidebar.

## Run

```bash
python photo_curator.py
```

Then open <http://127.0.0.1:5014> (note 50 mm, F1.4 in the port) in your browser. Pick a folder (or paste a path), choose a step, and press **Start**.

## Workflow

A typical pass on a full card is **Cull → Dedup → Rank** in order — each step feeds its survivors to the next, so ranking only scores the photos worth scoring.

In a hurry? Press **⚡ God Mode** to run all three automatically and jump straight to your ranked TOP N. Either way, **no files are deleted or moved until you explicitly choose to** — every stage is review-first.

## How it works

<img width="2752" height="1536" alt="Pipeline diagram" src="https://github.com/user-attachments/assets/4dba8472-c45a-42d6-aa04-070f9843c639" />

| Step | Metric | Notes |
|------|--------|-------|
| RAW decode | Embedded JPEG preview via `rawpy`/LibRaw (fallback: half-size demosaic) | ~50× faster than developing the sensor data; EXIF/GPS/orientation preserved. Exports copy the original RAW. |
| HEIC decode | `pillow-heif`/libheif, registered as a Pillow plugin | EXIF/GPS/orientation preserved. OpenCV can't read HEIF, so analysis routes through Pillow; the browser gets a cached JPEG transcode. Exports copy the original `.heic`. |
| Cull | `var(Laplacian) / var(image)` on a 1024px copy | Resolution-independent; normalizes out contrast so haze ≠ blur. Threshold is adjustable. |
| Dedup | 192-bit perceptual hash (avg + dual difference hash) + ORB confirm | Global clustering; EXIF-timed bursts get a relaxed bar; keeps the sharpest frame. RAW+JPG pairs can pre-collapse to one. |
| Rank | Weighted focus / lighting / contrast / color / composition | Per-photo radar + TOP-N average profile. |

Thumbnails are cached under your system temp dir, so the first pass over a folder is the only slow one.

## Platform notes

Tested on **macOS** (Apple Silicon) and **Windows 10/11 x64**. On macOS the native folder picker uses `osascript` and SD-card detection scans `/Volumes`; on Windows it uses the standard folder dialog and scans drive letters for `DCIM`. On Linux those conveniences are skipped, but you can paste a folder path into the field and everything else works.

## ☕ Support the project

Photo Curator is free, open source, and runs entirely on your own machine. If it saved you time, the best way to say thanks is to fuel the next feature:

<div align="center">
  <a href='https://ko-fi.com/B3S720JCU6' target='_blank'><img height='44' style='border:0px;height:44px;' src='https://storage.ko-fi.com/cdn/kofi6.png?v=6' border='0' alt='Buy Me a Coffee at ko-fi.com' /></a>
</div>

Every coffee genuinely helps — thank you! 🙏

## License

[MIT](LICENSE)
