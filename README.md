# 📸 Photo Curator

**v3.2** · A local, browser-based tool for culling and ranking large photo libraries. Point it at a folder of JPEGs (and see SD card folders in DCIM folder automatically) and it walks you through three steps — drop the blurry ones, collapse burst duplicates, and surface your best shots — all running entirely on your own machine. Nothing is uploaded anywhere.

Built for photographers who come home from a trip with a few thousand frames and want the keepers fast.

![pipeline: Cull → Dedup → Rank](https://img.shields.io/badge/pipeline-Cull%20→%20Dedup%20→%20Rank-blue)

<img width="1918" height="965" alt="Screenshot 2026-05-30 at 17 08 51" src="https://github.com/user-attachments/assets/0ce6577f-6f0b-4109-9d82-c570e238b03a" />

## ✨ Features

* 📸 **Cull (Smart Sharpness Detection)** — Automatically flags out-of-focus shots using a contrast-normalized sharpness measure. Genuinely soft frames are caught, while low-contrast-but-sharp scenes (like haze, night shots, or vast skies) are safely preserved. Includes a quick one-click `Sharp ⇄ Blurry` override for total control.
* 👯 **Dedup (Intelligent Burst Collapse)** — Merges identical burst sequences into a single frame using global perceptual-hash clustering. It utilizes EXIF capture-time to tighten burst detection and ORB feature-matching to prevent distinct scenes from being wrongly merged. The sharpest frame of each group is automatically selected as the keeper.
* 🏆 **Rank (Esthetic Scoring)** — Scores each photo based on composition, lighting, focus, color, and contrast. It highlights your `TOP N` best shots, complete with a per-photo hexagonal radar chart and a `TOP-N` average "metric profile". If you skip the Dedup step, Ranking automatically folds the clustering in so a one-click run still delivers a burst-free result.
* 💻 **Modern UX & Automation** — Features a real-time live preview as it processes, full dark/light theme support, and a responsive lightbox with arrow-key navigation. Optionally, it can automatically sort and move your files into `Blurred/`, `Duplicates/`, and `TOP_N/` subfolders.

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

A typical workflow on a big card is **Cull → Dedup → Rank** in order — each step feeds its survivors to the next, so ranking only scores the photos worth scoring.

<div align="center">
  <a href="https://www.youtube.com/watch?v=99fJBdjp3is">
    <img width="75%" alt="SCR-20260531-npnc" src="https://github.com/user-attachments/assets/caf8b6a4-0337-4d1e-b46e-f2ff06360239" alt="Watch on YouTube" />
  </a>
</div>

## How it works

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


## Do you like Photo Curator? Buy me a coffee! ;-)

<div align="center"><a href='https://ko-fi.com/B3S720JCU6' target='_blank'><img height='36' style='border:0px;height:36px;' src='https://storage.ko-fi.com/cdn/kofi6.png?v=6' border='0' alt='Buy Me a Coffee at ko-fi.com' /></a>
</div>
