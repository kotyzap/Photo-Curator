# 📸 Photo Curator

<img width="2752" height="1536" alt="Automated_Photo_Culling_Workflow" src="https://github.com/user-attachments/assets/d6092dc5-b13e-469f-bdcb-9f03c7c7d223" />


**v3.4** · A local, browser-based tool for culling and ranking large photo libraries. Point it at a folder of JPEGs and it walks you through three steps — drop the blurry ones, collapse burst duplicates, and surface your best shots — all running entirely on your own machine. Nothing is uploaded anywhere.

Built for photographers who come home from a trip with a few thousand frames and want the keepers fast.

![pipeline: Cull → Dedup → Rank](https://img.shields.io/badge/pipeline-Cull%20→%20Dedup%20→%20Rank-blue)




## Features

- **1 · Cull** — flags out-of-focus shots using a *contrast-normalized* sharpness measure, so genuinely soft frames are caught while low-contrast-but-sharp shots (haze, night, big skies) are kept. Sorts into **Sharp / Soft (recoverable) / Blurry**, with a one-click **Sharp ⇄ Blurry** override on every photo. Blurry shots are moved to `Blurred/` only when you press **Move blurry** — review first, move second.


- **2 · Dedup** — global perceptual-hash clustering collapses burst sequences to a single frame. EXIF capture-time tightens burst detection, ORB feature-matching prevents distinct scenes from being wrongly merged, and the **sharpest** frame of each group is kept and labelled **“Best of N”** (so you can see how many near-duplicates it stood in for). Frames with no near-duplicate are labelled **“Original”**. Matching is vectorized and signatures are cached, so big cards stay fast.

<img width="2167" height="956" alt="Screenshot 2026-05-31 at 20 50 17" src="https://github.com/user-attachments/assets/b09bcc8f-500e-49fa-8f66-917a8be55fc7" />


- **3 · Rank** — scores each photo on composition, lighting, focus, color, and contrast, then shows your **TOP N** with a per-photo hexagonal radar chart and a TOP-N average "metric profile". Ranking shows live per-photo progress with **percentage, elapsed time, and ETA**. If you skip Dedup, ranking folds the clustering in automatically so a one-click run still gives a burst-free result.

<img width="2855" height="1866" alt="SCR-20260531-smzr" src="https://github.com/user-attachments/assets/bc403303-2edf-4a48-9f25-27502627416f" />
<img width="2139" height="954" alt="Screenshot 2026-05-31 at 21 23 11" src="https://github.com/user-attachments/assets/2499032b-35da-4823-8d01-0efee29c7b58" />




- **⚡ God Mode** — one button runs the whole pipeline automatically: **Cull → Dedup → Rank**, advancing through each stage and landing on your ranked TOP N. It produces the ranking without moving any files, so you still review and move rejects yourself.
- Live preview (newest first) with pagination for huge sets, per-stage progress with elapsed time and **ETA**, EXIF-orientation-correct thumbnails, light/dark theme, lightbox with arrow-key review, and optional auto-move of rejects into `Blurred/`, `Duplicates/`, and `TOP_N/` subfolders.

<img width="2151" height="953" alt="Screenshot 2026-05-31 at 21 20 43" src="https://github.com/user-attachments/assets/d42867cb-1b99-4966-8e5e-1693e97a1c25" />



## Install

Requires **Python 3.9+**.

```bash
pip install -r requirements.txt
```

## Run

```bash
python photo_curator.py
```

Then open <http://127.0.0.1:5000> in your browser. Pick a folder (or paste a path), choose a step, and press **Start**.

A typical workflow on a big card is **Cull → Dedup → Rank** in order — each step feeds its survivors to the next, so ranking only scores the photos worth scoring. In a hurry? Press **⚡ God Mode** to run all three automatically and jump straight to your ranked TOP N.

## How it works

<img width="2752" height="1536" alt="Efficient_Photo_Culling_Pipeline" src="https://github.com/user-attachments/assets/4dba8472-c45a-42d6-aa04-070f9843c639" />


| Step | Metric | Notes |
|------|--------|-------|
| Cull | `var(Laplacian) / var(image)` on a 1024px copy | Resolution-independent; normalizes out contrast so haze ≠ blur. Threshold is adjustable. |
| Dedup | 192-bit perceptual hash (avg + dual difference hash) + ORB confirm | Global clustering; EXIF-timed bursts get a relaxed bar; keeps the sharpest frame. |
| Rank | Weighted focus / lighting / contrast / color / composition | Per-photo radar + TOP-N average profile. |

Thumbnails are cached under your system temp dir, so the first pass over a folder is the only slow one.

## Platform notes

Tested on **macOS**. The native folder picker uses `osascript` and SD-card detection scans `/Volumes`; on Windows/Linux those conveniences are skipped, but you can still paste a folder path into the field and everything else works.

## License

[MIT](LICENSE)

<img width="2752" height="1536" alt="Photo_Curator_Software_Processing_Workflow" src="https://github.com/user-attachments/assets/4b197f8b-3df7-44ef-a084-2955a0171ad7" />

## Buy me a Ko-Fi

<div align="center"><a href='https://ko-fi.com/B3S720JCU6' target='_blank'><img height='36' style='border:0px;height:36px;' src='https://storage.ko-fi.com/cdn/kofi6.png?v=6' border='0' alt='Buy Me a Coffee at ko-fi.com' /></a></div>

