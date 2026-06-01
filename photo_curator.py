#!/usr/bin/env python3
"""
Photo Curator v3 — full pipeline (Cull · Dedup · Rank)
=============================================================================
v3 combines v2's proven Cull and Dedup steps with the advanced, magazine-style
Ranking Studio:

  1 · CULL   contrast-normalized sharpness (haze/night/low-contrast shots are
             kept; only true blur is flagged). Per-photo Sharp/Blurry override
             that physically moves files in/out of Blurred/.
  2 · DEDUP  global perceptual-hash clustering (FastBatchDeduplicator) — burst
             sequences collapse to their sharpest frame; optional auto-move of
             duplicates to Duplicates/.
  3 · RANK   the advanced engine (photo_ranking_v3) with LIVE weight sliders,
             per-photo radar + sub-score breakdown, and non-destructive
             Remove/Restore from both the grid and the lightbox.

Steps feed each other: Rank uses Dedup survivors if present, else Cull
survivors, else the whole folder.

Pure cv2 / numpy / Pillow + Flask. Fully offline. Port 5001.
"""

import io
import json
import time
import shutil
import hashlib
import logging
import threading
import subprocess
from pathlib import Path
from dataclasses import dataclass
from urllib.parse import quote

import cv2
import numpy as np
from flask import Flask, render_template_string, request, jsonify, send_file, abort
from PIL import Image, ImageOps

from photo_ranking_v3 import AdvancedPhotoAnalyzer
from photo_dedup_batch import FastBatchDeduplicator
from photo_file_organizer import PhotoOrganizer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

IMG_EXTS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.webp'}
RECENTS_FILE = Path.home() / '.photo_curator_recents.json'
THUMB_DIR = Path('/tmp/photocurator_thumbs')
THUMB_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_WEIGHTS = {'aesthetic': 30, 'composition': 22, 'technical': 20,
                   'sharpness': 16, 'color': 12}
CATEGORIES = ['composition', 'technical', 'sharpness', 'color', 'aesthetic']


def _blank():
    return {'running': False, 'progress': 0, 'status': 'Idle', 'photos': []}


state = {
    'folder': None,
    'weights': dict(DEFAULT_WEIGHTS),
    'topn': 50,
    'excluded': set(),
    'phone_bg': set(),   # paths flagged "suitable as phone wallpaper"
    'auto': {'cull': False, 'dedup': False},
    'cull':  {**_blank(), 'sharp': 0, 'soft': 0, 'blurry': 0, 'sharp_paths': []},
    'dedup': {**_blank(), 'groups': 0, 'kept_paths': []},
    'rank':  {**_blank(), 'scores': [], 'total': 0, 'analyzed': 0},
}


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def list_images(folder):
    p = Path(folder)
    if not p.is_dir():
        return []
    return sorted(f for f in p.iterdir()
                  if f.is_file() and f.suffix.lower() in IMG_EXTS
                  # Skip macOS AppleDouble sidecars (._foo.jpg) and hidden files —
                  # they aren't real images and break decoding/thumbnails.
                  and not f.name.startswith('._')
                  and not f.name.startswith('.'))


def _thumb_cache_path(image_path):
    p = Path(image_path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0
    # The trailing version tag invalidates old cached thumbnails when the
    # thumbnail logic changes (v2 = EXIF orientation applied).
    key = hashlib.md5(f"{image_path}:{mtime}:v2".encode()).hexdigest()
    return THUMB_DIR / f"{key}.jpg"


def make_thumb_file(image_path, size=300):
    out = _thumb_cache_path(image_path)
    if out.exists():
        return out
    try:
        img = Image.open(image_path)
        img.draft('RGB', (size * 2, size * 2))
        # Honor the EXIF orientation flag so portrait photos aren't shown
        # sideways in thumbnails (the full-size view already auto-rotates).
        img = ImageOps.exif_transpose(img)
        img = img.convert('RGB')
        img.thumbnail((size, size), Image.Resampling.BILINEAR)
        img.save(out, format='JPEG', quality=80)
        return out
    except Exception as e:
        logger.warning(f"thumb fail {image_path}: {e}")
        return None


def thumb_url(image_path):
    # The &v tag busts the BROWSER's HTTP cache when thumbnail logic changes
    # (v2 = EXIF orientation applied). Without it, the browser keeps serving the
    # previously-cached (sideways) thumbnail for the same URL.
    return '/api/thumb?path=' + quote(str(image_path)) + '&v=2'


def load_recents():
    try:
        if RECENTS_FILE.exists():
            return json.loads(RECENTS_FILE.read_text())
    except Exception:
        pass
    return []


def save_recent(folder):
    recents = [r for r in load_recents() if r != folder]
    recents.insert(0, folder)
    try:
        RECENTS_FILE.write_text(json.dumps(recents[:8]))
    except Exception as e:
        logger.warning(f"save recents fail: {e}")


def detect_sd_cards():
    found = []
    volumes = Path('/Volumes')
    if volumes.is_dir():
        for vol in volumes.iterdir():
            dcim = vol / 'DCIM'
            if dcim.is_dir():
                subdirs = [d for d in dcim.iterdir() if d.is_dir()]
                for d in sorted(subdirs):
                    found.append(str(d))
                if not subdirs:
                    found.append(str(dcim))
    return found


def native_folder_dialog(prompt="Select a folder"):
    try:
        script = f'POSIX path of (choose folder with prompt "{prompt}")'
        out = subprocess.run(['osascript', '-e', script],
                             capture_output=True, text=True, timeout=120)
        path = out.stdout.strip()
        if path:
            return path.rstrip('/')
    except Exception as e:
        logger.warning(f"folder dialog fail: {e}")
    return None


# --------------------------------------------------------------------------- #
#  Sharpness (shared by Cull, and as the dedup quality key)
# --------------------------------------------------------------------------- #
def sharpness_score(gray):
    """Whole-frame contrast-normalized focus measure (used by Dedup's quality
    key). Haze/low contrast does NOT read as blur."""
    h, w = gray.shape[:2]
    long_edge = max(h, w)
    if long_edge > 1024:
        sc = 1024.0 / long_edge
        gray = cv2.resize(gray, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    img_var = float(gray.astype('float32').var()) + 1e-6
    return lap_var / img_var * 1000.0


def region_sharpness(gray, tiles=8):
    """REGION-based sharpness for Cull: tile the frame, score each tile's
    contrast-normalized focus, and return a high percentile. This measures the
    SHARPEST meaningful region, so a tack-sharp subject against soft bokeh/sky
    is correctly recognized as in-focus instead of being dragged down by the
    smooth background (the key v3.1 accuracy fix)."""
    h, w = gray.shape[:2]
    long_edge = max(h, w)
    if long_edge > 1024:
        sc = 1024.0 / long_edge
        gray = cv2.resize(gray, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
    lap = cv2.Laplacian(gray, cv2.CV_64F)          # uint8 -> CV_64F (supported)
    g = gray.astype('float32')
    H, W = g.shape
    th, tw = max(1, H // tiles), max(1, W // tiles)
    scores = []
    for y in range(0, H, th):
        for x in range(0, W, tw):
            tg = g[y:y + th, x:x + tw]
            if tg.size < 64:
                continue
            v = float(tg.var()) + 1e-6
            scores.append(float(lap[y:y + th, x:x + tw].var()) / v * 1000.0)
    if not scores:
        return 0.0
    return float(np.percentile(np.array(scores), 90))


def quick_quality(bgr, gray):
    """Lightweight 0–100 'is this an interesting, detailed frame' proxy used
    only to RESCUE soft-but-well-composed shots from culling. Cheap on purpose;
    the real aesthetic judgement happens in the Rank step."""
    edges = cv2.Canny(gray, 60, 160)
    ed = float(np.count_nonzero(edges)) / edges.size
    contrast = float(gray.std())
    b, gg, r = (bgr[:, :, i].astype('float32') for i in range(3))
    rg, yb = r - gg, 0.5 * (r + gg) - b
    cf = (rg.std() ** 2 + yb.std() ** 2) ** 0.5 + 0.3 * (rg.mean() ** 2 + yb.mean() ** 2) ** 0.5
    q = 0.5 * min(1, ed / 0.12) + 0.25 * min(1, contrast / 70) + 0.25 * min(1, cf / 80)
    return float(max(0.0, min(1.0, q)) * 100)


def classify_sharpness(region_s, q, blur_lo, sharp_hi, q_rescue, rescue_on):
    """Three tiers + quality rescue. Returns (tier, rescued)."""
    if region_s >= sharp_hi:
        return 'sharp', False
    if region_s >= blur_lo:
        return 'soft', bool(rescue_on and q >= q_rescue)   # ★ if well-composed
    # below blur floor — normally Blurry, but rescue a near-miss that's gorgeous
    if rescue_on and q >= q_rescue and region_s >= blur_lo * 0.6:
        return 'soft', True
    return 'blurry', False


@dataclass
class _LiteScore:
    """Minimal score object for the deduplicator (needs path/filename/quality)."""
    path: str
    filename: str
    overall_score: float
    focus: float


# --------------------------------------------------------------------------- #
#  CULL
# --------------------------------------------------------------------------- #
BASE_BLUR, BASE_SHARP = 90.0, 230.0   # region_s baselines when not adaptive


def _badge_for(tier, star):
    if tier == 'sharp':
        return 'Sharp', 'good'
    if tier == 'soft':
        return ('Soft ★' if star else 'Soft'), 'soft'
    return 'Blurry', 'bad'


def run_cull(folder, strictness, adaptive, rescue_on):
    s = state['cull']
    s.update({'running': True, 'cancel': False, 'progress': 0, 'status': 'Scanning…',
              'photos': [], 'sharp': 0, 'soft': 0, 'blurry': 0, 'sharp_paths': [],
              # complete=True only when cull runs to the end; a stopped cull must
              # not feed its partial survivor list into Dedup/Rank.
              'complete': False, 'src_folder': str(folder)})
    try:
        images = list_images(folder)
        total = len(images) or 1
        items = []   # {name, path, region_s, q}

        def thresholds():
            if adaptive and items:
                M = float(np.median([it['region_s'] for it in items]))
                # Sharpness is ~log-distributed; keep the blur floor LOW and the
                # Soft band WIDE so slightly-soft (Topaz-recoverable) frames are
                # kept rather than culled. Only clearly-soft frames fall below.
                blur_lo = max(30.0, M * 0.25)
                sharp_hi = max(blur_lo * 1.5, M * 0.70)
            else:
                blur_lo, sharp_hi = BASE_BLUR, BASE_SHARP
            blur_lo *= strictness
            sharp_hi *= strictness
            qs = [it['q'] for it in items]
            q_rescue = float(np.percentile(qs, 70)) if qs else 65.0
            return blur_lo, sharp_hi, q_rescue

        def classify_all():
            blur_lo, sharp_hi, q_rescue = thresholds()
            photos, kept = [], []
            sharp = soft = blurry = 0
            for it in items:
                tier, star = classify_sharpness(it['region_s'], it['q'],
                                                blur_lo, sharp_hi, q_rescue, rescue_on)
                if tier == 'sharp':
                    sharp += 1
                elif tier == 'soft':
                    soft += 1
                else:
                    blurry += 1
                if tier != 'blurry':
                    kept.append(it['path'])
                badge, bt = _badge_for(tier, star)
                photos.append({'name': it['name'], 'path': it['path'],
                               'thumb': thumb_url(it['path']), 'score': f"{it['region_s']:.0f}",
                               'badge': badge, 'badgeType': bt, 'tier': tier,
                               'kept': tier != 'blurry', 'rejected': tier == 'blurry'})
            # Newest-processed first in the live grid (no scrolling to bottom).
            # Only the display order is reversed; `kept` stays in capture order
            # so Dedup/Rank still receive survivors in their natural sequence.
            s['photos'] = photos[::-1]
            s['sharp'], s['soft'], s['blurry'] = sharp, soft, blurry
            s['sharp_paths'] = kept   # kept = sharp + soft (flows to Dedup/Rank)

        t0 = time.time()

        def _fmt(sec):
            sec = int(max(0, sec)); h, r = divmod(sec, 3600); m, s_ = divmod(r, 60)
            return f"{h}h{m:02d}m" if h else (f"{m}m{s_:02d}s" if m else f"{s_}s")

        def _tiers(done):
            k = (s['sharp'] + s['soft'] + s['blurry']) or 1
            return (f"Sharp {s['sharp']} ({s['sharp']/k*100:.0f}%) / "
                    f"Soft {s['soft']} ({s['soft']/k*100:.0f}%) / "
                    f"Blurry {s['blurry']} ({s['blurry']/k*100:.0f}%)")

        for idx, p in enumerate(images):
            if s.get('cancel'):
                classify_all()
                s['status'] = (f"Stopped at {idx}/{total} · {_tiers(idx)} · "
                               f"elapsed {_fmt(time.time()-t0)}")
                return
            done = idx + 1
            s['progress'] = int(done / total * 100)
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            s['status'] = (f"Culling {p.name} ({done}/{total}, {done/total*100:.0f}%) · "
                           f"{_tiers(done)} · elapsed {_fmt(elapsed)} · ETA {_fmt(eta)}")
            bgr = cv2.imread(str(p), cv2.IMREAD_REDUCED_COLOR_2)
            if bgr is None:
                bgr = cv2.imread(str(p), cv2.IMREAD_COLOR)
            if bgr is None:
                continue
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            items.append({'name': p.name, 'path': str(p),
                          'region_s': region_sharpness(gray),
                          'q': quick_quality(bgr, gray)})
            if idx % 5 == 0 or idx == len(images) - 1:
                classify_all()
        classify_all()

        # Blurry photos are NOT moved automatically — they stay in place so you
        # can review them first, then move them with the "Move blurry → Blurred/"
        # button (mirrors the TOP-N export flow).
        s['progress'] = 100
        s['complete'] = True   # full pass finished — survivors are safe to chain
        s['status'] = (f"Done in {_fmt(time.time()-t0)} · {_tiers(total)}"
                       + (" · review, then Move blurry → Blurred/" if s['blurry'] else ""))
    except Exception as e:
        logger.error(f"cull failed: {e}", exc_info=True)
        s['status'] = f"Error: {e}"
    finally:
        s['running'] = False


def _relocate_for_status(path, now_kept):
    """Keep disk in sync: kept (Sharp/Soft) live in the folder, Blurry go to
    Blurred/. Soft is treated as kept — never auto-moved."""
    folder = state.get('folder')
    if not folder or not Path(folder).is_dir():
        return path
    folder = Path(folder)
    blurred = folder / "Blurred"
    name = Path(path).name
    cur = None
    for cand in (Path(path), folder / name, blurred / name):
        if cand.exists():
            cur = cand
            break
    if cur is None:
        return path
    if now_kept:
        target = folder
    else:
        if not (state['auto']['cull'] or blurred.is_dir()):
            return str(cur)
        target = blurred
    if cur.parent == target:
        return str(cur)
    try:
        target.mkdir(parents=True, exist_ok=True)
        dst = target / name
        if dst.exists():
            dst = target / f"{cur.stem}_{int(time.time())}{cur.suffix}"
        shutil.move(str(cur), str(dst))
        logger.info(f"Moved {name} → {target.name}/ (status change)")
        return str(dst)
    except Exception as e:
        logger.warning(f"relocate fail {name}: {e}")
        return str(cur)


# --------------------------------------------------------------------------- #
#  DEDUP
# --------------------------------------------------------------------------- #
def run_dedup(folder, threshold):
    s = state['dedup']
    s.update({'running': True, 'cancel': False, 'progress': 0, 'status': 'Preparing…',
              'photos': [], 'groups': 0, 'kept_paths': [],
              'complete': False, 'src_folder': str(folder)})
    try:
        # Chain off Cull's survivors ONLY if Cull finished a full pass on THIS
        # folder. A stopped/partial cull (or a cull of a different folder) must
        # not silently shrink Dedup's input — fall back to the whole folder.
        cull = state['cull']
        chain_ok = (cull.get('complete') and cull.get('sharp_paths')
                    and cull.get('src_folder') == str(folder))
        if chain_ok:
            paths = [Path(p) for p in cull['sharp_paths']]
            logger.info(f"Dedup: chaining {len(paths)} Cull survivors")
        else:
            paths = list_images(folder)
            logger.info(f"Dedup: scanning full folder ({len(paths)} images) "
                        f"— no completed Cull for this folder")
        if not paths:
            s['status'] = 'No images'
            return
        dd = FastBatchDeduplicator(threshold=threshold)
        # Persist perceptual signatures so a repeat run on this folder is fast.
        try:
            dd.enable_disk_cache(Path(paths[0]).parent / '.dedup_sig_cache.json')
        except Exception:
            pass
        dd.reset()
        total = len(paths)

        # Live-grid limits: rebuilding + shipping the full survivor list (and the
        # browser re-rendering every thumbnail) gets expensive past a few
        # thousand uniques. Cap the displayed thumbnails to the most recent
        # GRID_CAP and refresh on a time interval, not every Nth photo.
        GRID_CAP = 400
        REFRESH_SECS = 1.0
        last_refresh = [0.0]

        def refresh(final=False):
            # During the live run, cap to the most-recent GRID_CAP so the browser
            # stays responsive. When finished, show ALL survivors so every kept
            # photo can be reviewed (rendered once, with lazy-loading images).
            # Iterate clusters (not just reps) so each card knows how many frames
            # collapsed into it → "Best of N" / "N similar hidden".
            clusters = dd.clusters
            shown = clusters if final else clusters[-GRID_CAP:]
            photos = []
            for c in reversed(shown):   # most-recently-kept first
                sc = c.rep
                photos.append({'name': sc.filename, 'path': sc.path,
                               'thumb': thumb_url(sc.path), 'score': f"{sc.overall_score:.0f}",
                               'group': len(c.members),
                               'badge': 'KEPT', 'badgeType': 'good', 'kept': True})
            s['photos'] = photos
            s['groups'] = len(dd.clusters)
            last_refresh[0] = time.time()

        t0 = time.time()

        def _fmt(sec):
            sec = int(max(0, sec)); h, r = divmod(sec, 3600); m, s_ = divmod(r, 60)
            return f"{h}h{m:02d}m" if h else (f"{m}m{s_:02d}s" if m else f"{s_}s")

        for idx, p in enumerate(paths):
            if s.get('cancel'):
                refresh(final=True)
                el = time.time() - t0
                s['status'] = (f"Stopped · {len(dd.clusters)} unique of {idx} "
                               f"({(len(dd.clusters)/idx*100) if idx else 0:.0f}%) · "
                               f"elapsed {_fmt(el)}")
                state['dedup']['kept_paths'] = [sc.path for sc in dd.current_survivors()]
                return
            done = idx + 1
            s['progress'] = int(done / total * 100)
            uniq = len(dd.clusters)
            pct_uniq = (uniq / done * 100) if done else 0
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0          # photos/sec
            eta = (total - done) / rate if rate > 0 else 0
            s['status'] = (f"Deduping {p.name} ({done}/{total}) · "
                           f"{uniq} unique ({pct_uniq:.0f}%) · "
                           f"elapsed {_fmt(elapsed)} · ETA {_fmt(eta)}")
            gray = cv2.imread(str(p), cv2.IMREAD_REDUCED_GRAYSCALE_2)
            if gray is None:
                gray = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            sharp = sharpness_score(gray) if gray is not None else 0.0
            dd.add_photo(_LiteScore(str(p), p.name, sharp, sharp))
            if time.time() - last_refresh[0] >= REFRESH_SECS or idx == total - 1:
                refresh()
        dd.save_disk_cache()
        refresh(final=True)
        kept = [sc.path for sc in dd.current_survivors()]
        s['kept_paths'] = kept
        s['complete'] = True   # full pass finished — survivors safe to chain
        removed = total - len(kept)
        took = _fmt(time.time() - t0)
        pct_uniq = (len(kept) / total * 100) if total else 0
        if state['auto']['dedup'] and removed:
            try:
                org = PhotoOrganizer(folder)
                kept_set = set(kept)
                dups = [str(p) for p in paths if str(p) not in kept_set]
                r = org.move_duplicate_photos(dups)
                s['status'] = (f"Done in {took} · {len(kept)} unique of {total} "
                               f"({pct_uniq:.0f}%) · moved {r.get('moved', 0)} → Duplicates/")
            except Exception as e:
                logger.warning(f"dedup auto-move failed: {e}")
                s['status'] = (f"Done in {took} · {len(kept)} unique of {total} "
                               f"({pct_uniq:.0f}%) · {removed} dupes")
        else:
            s['status'] = (f"Done in {took} · {len(kept)} unique of {total} "
                           f"({pct_uniq:.0f}%) · {removed} dupes")
        s['progress'] = 100
    except Exception as e:
        logger.error(f"dedup failed: {e}", exc_info=True)
        s['status'] = f"Error: {e}"
    finally:
        s['running'] = False


# --------------------------------------------------------------------------- #
#  RANK
# --------------------------------------------------------------------------- #
def weighted_overall(score, weights):
    wsum = sum(max(0, v) for v in weights.values()) or 1.0
    return sum(max(0, weights.get(k, 0)) * getattr(score, k, 0.0)
               for k in CATEGORIES) / wsum


def build_topn(weights=None, topn=None):
    weights = weights or state['weights']
    topn = topn or state['topn']
    excluded = state['excluded']
    scores = [s for s in state['rank']['scores'] if s.path not in excluded]
    ranked = sorted(scores, key=lambda s: weighted_overall(s, weights), reverse=True)[:topn]
    out = []
    for rank, s in enumerate(ranked, 1):
        ov = weighted_overall(s, weights)
        out.append({
            'name': s.filename, 'path': s.path, 'thumb': thumb_url(s.path),
            'rank': rank, 'score': f"{ov:.1f}",
            'phonebg': s.path in state['phone_bg'],
            'scores': {'composition': round(s.composition), 'technical': round(s.technical),
                       'sharpness': round(s.sharpness), 'color': round(s.color),
                       'aesthetic': round(s.aesthetic)},
            'detail': {'Rule of thirds': round(s.rule_of_thirds), 'Horizon level': round(s.horizon_level),
                       'Balance': round(s.balance), 'Exposure': round(s.exposure),
                       'Dynamic range': round(s.dynamic_range), 'Tonal range': round(s.tonal),
                       'White balance': round(s.white_balance), 'Noise (clean)': round(s.noise),
                       'Colorfulness': round(s.colorfulness), 'Color harmony': round(s.harmony)},
        })
    return out


def run_rank(folder):
    s = state['rank']
    s.update({'running': True, 'cancel': False, 'progress': 0, 'status': 'Preparing…',
              'scores': [], 'total': 0, 'analyzed': 0})
    state['excluded'] = set()
    try:
        # Prefer a COMPLETED Dedup on this folder, then a COMPLETED Cull on this
        # folder; otherwise rank the whole folder. Never chain off a partial run.
        dd, cull = state['dedup'], state['cull']
        if dd.get('complete') and dd.get('kept_paths') and dd.get('src_folder') == str(folder):
            paths = [Path(p) for p in dd['kept_paths']]
            chain = 'dedup survivors'
        elif cull.get('complete') and cull.get('sharp_paths') and cull.get('src_folder') == str(folder):
            paths = [Path(p) for p in cull['sharp_paths']]
            chain = 'sharp photos'
        else:
            paths = list_images(folder)
            chain = 'all photos'
        if not paths:
            s['status'] = 'No images to rank'
            return
        total = len(paths)
        s['total'] = total
        analyzer = AdvancedPhotoAnalyzer()

        t0 = time.time()

        def _fmt(sec):
            sec = int(max(0, sec)); h, r = divmod(sec, 3600); m, s_ = divmod(r, 60)
            return f"{h}h{m:02d}m" if h else (f"{m}m{s_:02d}s" if m else f"{s_}s")

        for idx, p in enumerate(paths):
            if s.get('cancel'):
                s['status'] = (f"Stopped at {idx}/{total} · ranked {len(s['scores'])} so far · "
                               f"elapsed {_fmt(time.time()-t0)}")
                return
            done = idx + 1
            s['progress'] = int(done / total * 100)
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            s['status'] = (f"Ranking {p.name} ({done}/{total}, {done/total*100:.0f}%) · "
                           f"elapsed {_fmt(elapsed)} · ETA {_fmt(eta)}")
            sc = analyzer.analyze_image(str(p))
            if sc:
                s['scores'].append(sc)
            s['analyzed'] = len(s['scores'])
        s['progress'] = 100
        s['status'] = f"Done · ranked {len(s['scores'])} {chain} · elapsed {_fmt(time.time()-t0)}"
    except Exception as e:
        logger.error(f"rank failed: {e}", exc_info=True)
        s['status'] = f"Error: {e}"
    finally:
        s['running'] = False


# --------------------------------------------------------------------------- #
#  HTML
# --------------------------------------------------------------------------- #
HTML = r'''<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Photo Curator v3.7</title>
<style>
  :root{--bg:#f4f6fb;--panel:#fff;--panel2:#eef1f7;--text:#1c2330;--muted:#6b7280;
        --accent:#2563eb;--good:#16a34a;--warn:#d97706;--bad:#dc2626;--border:#dde3ec;--shadow:rgba(20,40,80,.10);color-scheme:light}
  [data-theme=dark]{--bg:#0f141c;--panel:#161d28;--panel2:#1d2633;--text:#e8edf5;--muted:#9aa6b6;
        --accent:#3b82f6;--border:#27313f;--shadow:rgba(0,0,0,.5);color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text)}
  .top{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:linear-gradient(90deg,#1e40af,#2563eb);color:#fff}
  .brand{font-size:17px;font-weight:700}.brand small{font-weight:400;opacity:.8;font-size:12px}
  .steps{display:flex;gap:8px}
  .step{padding:7px 16px;background:rgba(255,255,255,.18);border:2px solid transparent;border-radius:9px;cursor:pointer;font-weight:600;font-size:13px;color:#fff}
  .step:hover{background:rgba(255,255,255,.3)} .step.active{background:#fff;color:var(--accent)}
  .theme{background:rgba(255,255,255,.18);border:none;color:#fff;width:38px;height:32px;border-radius:8px;cursor:pointer}
  .viewport{display:flex;height:calc(100vh - 58px)}
  .sidebar{width:300px;flex:0 0 300px;background:var(--panel);border-right:1px solid var(--border);padding:16px;overflow:hidden;display:flex;flex-direction:column}
  /* Scrollable region holds folder + settings + stats; the action footer below
     is pinned so Start / Export / Move blurry stay above the fold. */
  .sidebar-scroll{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:12px;padding-right:4px}
  .sidebar-actions{flex:0 0 auto;display:flex;flex-direction:column;gap:8px;padding-top:10px;margin-top:6px;border-top:1px solid var(--border)}
  #shortcuts{display:flex;flex-direction:column}
  .folder-row{display:flex;gap:8px;align-items:stretch}
  .folder-row input{flex:1;min-width:0}
  .folder-row .btn{width:auto;flex:0 0 auto;white-space:nowrap;padding:11px 16px}
  .main{flex:1;overflow-y:auto;padding:14px 18px}
  .sidebar-title{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
  input[type=text],input[type=number]{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:var(--panel2);color:var(--text);-webkit-text-fill-color:var(--text)}
  input[type=text]::placeholder,input[type=number]::placeholder{color:var(--muted);-webkit-text-fill-color:var(--muted)}
  .btn{width:100%;padding:11px;border:none;border-radius:9px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer;font-size:14px}
  .btn:hover{filter:brightness(1.07)}
  .btn.stopping{background:var(--bad)}
  .btn-ghost{width:100%;padding:9px;border:1px solid var(--border);border-radius:9px;background:var(--panel);color:var(--text);cursor:pointer;font-weight:500}
  /* Muted Start when a contextual primary action (e.g. Move blurry) takes over */
  .btn.secondary{background:var(--panel2);color:var(--muted);border:1px solid var(--border)}
  .btn.secondary:hover{filter:none;border-color:var(--accent)}
  /* Emphasised contextual call-to-action */
  .btn.cta{box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 30%,transparent)}
  .btn.god{background:linear-gradient(90deg,#f59e0b,#d946ef);font-weight:700}
  .btn.god.stopping{background:var(--bad)}
  .shortcut{display:flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:var(--panel);cursor:pointer;font-size:13px;margin-top:4px;width:100%;text-align:left}
  .shortcut:hover{border-color:var(--accent)}
  .tag{font-size:9px;font-weight:700;padding:2px 5px;border-radius:4px;color:#fff}
  .tag.sd{background:var(--good)} .tag.recent{background:var(--warn)}
  .wgroup{margin-bottom:6px}
  .wgroup label{display:flex;justify-content:space-between;font-size:12px;font-weight:600;margin-bottom:3px}
  .wgroup label b{color:var(--accent)} .wgroup input[type=range]{width:100%}
  .slider-value{font-size:11px;color:var(--muted)}
  .check{display:flex;align-items:center;gap:7px;font-size:12px;cursor:pointer;padding:8px;background:var(--panel2);border-radius:8px}
  .stat-row{display:flex;justify-content:space-between;font-size:13px;padding:3px 0}.stat-row .v{font-weight:700;color:var(--accent)}
  .panel-box{background:var(--panel2);border-radius:10px;padding:12px}
  .progress-wrap{margin-bottom:12px;display:none}
  .progress-bar{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden}
  .progress-fill{height:100%;width:0;background:var(--accent);transition:width .25s}
  .progress-text{font-size:12px;color:var(--muted);margin-top:5px}
  .gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(165px,1fr));gap:12px}
  .empty{grid-column:1/-1;text-align:center;color:var(--muted);padding:60px 0}.empty .icon{font-size:44px}
  .empty .title{font-size:19px;font-weight:700;color:var(--text);margin:12px 0 4px}
  .empty p{margin:0 0 16px;font-size:14px}
  .empty .lines{display:inline-flex;flex-direction:column;gap:9px;text-align:left;font-size:14px;line-height:1.4}
  .empty .lines b{color:var(--accent)}
  .photo-card{position:relative;border-radius:9px;overflow:hidden;background:var(--panel2);border:2px solid transparent;cursor:pointer}
  .photo-card:hover{border-color:var(--accent);box-shadow:0 4px 12px var(--shadow)}
  .photo-card.kept{border-color:var(--good)} .photo-card.rejected{opacity:.5}
  .photo-card.soft{border-color:var(--warn)}
  .badge.soft{background:var(--warn)}
  .filter-bar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
  .pager{display:flex;align-items:center;gap:14px;justify-content:center;margin:4px 0 14px;font-size:13px}
  .pager button{padding:6px 14px;border:1px solid var(--border);border-radius:6px;background:var(--panel);
                cursor:pointer;font-weight:600}
  .pager button:disabled{opacity:.4;cursor:not-allowed}
  .chip{padding:5px 12px;border:1px solid var(--border);border-radius:16px;background:var(--panel);color:var(--text);cursor:pointer;font-size:12px;font-weight:600}
  .chip:hover{border-color:var(--accent)} .chip.active{background:var(--accent);color:#fff;border-color:var(--accent)}
  .rank-num{position:absolute;top:6px;left:6px;background:var(--accent);color:#fff;min-width:24px;height:24px;padding:0 6px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;z-index:5}
  .badge{position:absolute;top:6px;right:6px;color:#fff;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:700;z-index:5}
  .badge.good{background:var(--good)} .badge.bad{background:var(--bad)}
  .status-toggle{position:absolute;bottom:34px;right:6px;z-index:6;border:none;border-radius:5px;padding:4px 8px;font-size:10px;font-weight:700;cursor:pointer;background:rgba(0,0,0,.62);color:#fff}
  .status-toggle:hover{background:rgba(0,0,0,.85)}
  .photo-card.pbg{border-color:#e8632a!important;box-shadow:0 0 0 2px #e8632a}
  .pbg-toggle{position:absolute;top:6px;right:6px;z-index:7;border:none;border-radius:50%;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:14px;cursor:pointer;background:rgba(0,0,0,.55);color:#fff;backdrop-filter:blur(2px);transition:background .15s,transform .15s}
  .pbg-toggle:hover{background:rgba(0,0,0,.8);transform:scale(1.08)}
  .pbg-toggle.on{background:#e8632a;box-shadow:0 1px 5px rgba(232,99,42,.6)}
  .lb-btn.pbg{background:rgba(232,99,42,.85)} .lb-btn.pbg:hover{background:#e8632a} .lb-btn.pbg.on{background:#e8632a}
  .photo-img{width:100%;aspect-ratio:3/2;object-fit:cover;display:block}
  .photo-info{padding:6px 8px}.pi-row{display:flex;align-items:center;gap:6px}
  .photo-name{flex:1;font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .photo-score{font-size:15px;font-weight:700;color:var(--accent)}
  .remove-btn{flex:0 0 auto;border:none;background:rgba(220,38,38,.12);color:#dc2626;border-radius:5px;font-size:10px;font-weight:700;padding:2px 7px;cursor:pointer;line-height:1.5}
  .remove-btn:hover{background:#dc2626;color:#fff}
  #removedBox{font-size:12px;color:var(--muted);margin-top:2px}#removedBox a{color:var(--accent);cursor:pointer;text-decoration:underline}
  /* lightbox */
  .lightbox{position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:200;display:none;flex-direction:column;align-items:center;justify-content:center}
  .lightbox.open{display:flex}
  .lb-img{max-width:62vw;max-height:80vh;object-fit:contain;border-radius:6px}
  .lb-bar{position:absolute;top:0;left:0;right:320px;display:flex;justify-content:space-between;align-items:center;padding:14px 22px;color:#fff;z-index:20;background:linear-gradient(180deg,rgba(0,0,0,.6),transparent)}
  .lb-close{background:rgba(255,255,255,.2);border:none;color:#fff;font-size:22px;width:42px;height:42px;border-radius:50%;cursor:pointer;z-index:30}
  .lb-close:hover{background:rgba(255,255,255,.4)}
  .lb-actions{display:flex;gap:10px;align-items:center;z-index:30}
  .lb-btn{border:none;border-radius:9px;height:38px;padding:0 14px;font-size:13px;line-height:1;font-weight:700;cursor:pointer;color:#fff;display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box}
  .lb-btn.remove{background:rgba(220,38,38,.85)} .lb-btn.remove:hover{background:#dc2626}
  .lb-btn.restore{background:rgba(255,255,255,.22)} .lb-btn.restore:hover{background:rgba(255,255,255,.4)}
  .lb-btn.toggle{background:rgba(37,99,235,.85)} .lb-btn.toggle:hover{background:#2563eb}
  .lb-nav{position:absolute;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.15);border:none;color:#fff;font-size:30px;width:54px;height:54px;border-radius:50%;cursor:pointer;z-index:20}
  .lb-prev{left:18px}.lb-next{right:338px}
  .lb-side{position:absolute;right:0;top:0;bottom:0;width:300px;background:rgba(15,20,28,.95);color:#fff;padding:22px 20px 28px;overflow-y:auto;z-index:10}
  .lb-side h3{margin:16px 0 6px;font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;opacity:.65;display:flex;justify-content:space-between;align-items:baseline}
  .lb-side h3 span{font-size:12px;opacity:.9;color:#93c5fd}
  .bar{display:flex;align-items:center;gap:8px;margin:6px 0;font-size:11px;cursor:help}
  .bar .lab{flex:0 0 104px;opacity:.85}
  .bar .track{flex:1;display:block;height:8px;background:rgba(255,255,255,.14);border-radius:4px;overflow:hidden}
  .bar .fill{display:block;height:100%;border-radius:4px;transition:width .25s}
  .bar .num{flex:0 0 26px;text-align:right;font-weight:700}.bar.cat .lab{font-weight:700;opacity:1}
  .exrow{display:grid;grid-template-columns:74px 1fr;gap:10px;align-items:baseline;margin:7px 0;font-size:12px}
  .exrow .lab{opacity:.55;font-size:11px}
  .exrow .val{text-align:right;line-height:1.35;overflow-wrap:break-word}
  .exrow .val a{color:#60a5fa;text-decoration:none}.exrow .val a:hover{text-decoration:underline}
  .exmap{display:block;margin:8px 0 4px;text-decoration:none}
  .exmap .tilewrap{position:relative;display:block;width:256px;max-width:100%;height:256px;overflow:hidden;border-radius:8px;border:1px solid rgba(255,255,255,.12)}
  .exmap .tilewrap img{display:block;width:256px;height:256px}
  .exmap .pin{position:absolute;width:12px;height:12px;border-radius:50%;background:#ef4444;border:2px solid #fff;box-shadow:0 0 4px rgba(0,0,0,.6);transform:translate(-50%,-50%);pointer-events:none}
  .exmap .cred{display:block;font-size:9px;opacity:.45;margin-top:3px}
  .top-right{display:flex;align-items:center;gap:10px}
  .kofi-btn{display:block;line-height:0;transition:transform .12s}
  .kofi-btn:hover{transform:translateY(-2px)} .kofi-btn img{display:block;border-radius:8px;box-shadow:0 3px 12px var(--shadow)}
  /* toasts */
  .toast-wrap{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);z-index:400;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none}
  .toast{background:var(--panel);color:var(--text);border:1px solid var(--border);border-left:4px solid var(--accent);
         box-shadow:0 10px 34px var(--shadow);border-radius:10px;padding:12px 18px;font-size:13px;max-width:460px;
         opacity:0;transform:translateY(12px);transition:opacity .25s,transform .25s;white-space:pre-line;text-align:center}
  .toast.show{opacity:1;transform:translateY(0)}
  .toast.good{border-left-color:var(--good)} .toast.bad{border-left-color:var(--bad)} .toast.info{border-left-color:var(--accent)}
</style></head><body>
<div class="top">
  <div class="brand">🎞️ Photo Curator <small>v3.7</small></div>
  <div class="steps">
    <div class="step active" data-step="cull">1 · Cull</div>
    <div class="step" data-step="dedup">2 · Dedup</div>
    <div class="step" data-step="rank">3 · Rank</div>
  </div>
  <div class="top-right">
    <a class="kofi-btn" href='https://ko-fi.com/B3S720JCU6' target='_blank' rel='noopener'><img height='32' style='border:0;height:32px' src='https://storage.ko-fi.com/cdn/kofi6.png?v=6' alt='Buy Me a Coffee at ko-fi.com'></a>
    <button class="theme" id="themeToggle">🌙</button>
  </div>
</div>
<div class="viewport">
  <div class="sidebar">
    <div class="sidebar-scroll">
      <div class="sidebar-title">📁 Folder</div>
      <div class="folder-row">
        <input type="text" id="folderInput" placeholder="/path/to/photos">
        <button class="btn" id="browseBtn">Browse…</button>
      </div>
      <div id="shortcuts"></div>

      <div class="sidebar-title" style="margin-top:6px" id="settingsTitle">⚙️ Settings</div>
      <div id="settingsPanel"></div>

      <div class="panel-box">
        <div class="stat-row" data-steps="cull dedup rank"><span>Images</span><span class="v" id="sImages">0</span></div>
        <div class="stat-row" data-steps="cull"><span>Sharp</span><span class="v" id="sSharp">0</span></div>
        <div class="stat-row" data-steps="cull"><span>Soft (recoverable)</span><span class="v" id="sSoft" style="color:var(--warn)">0</span></div>
        <div class="stat-row" data-steps="cull"><span>Blurry</span><span class="v" id="sBlurry">0</span></div>
        <div class="stat-row" data-steps="dedup"><span>Unique (dedup)</span><span class="v" id="sGroups">0</span></div>
        <div class="stat-row" data-steps="cull dedup rank"><span>Showing</span><span class="v" id="sShowing">0</span></div>
        <div id="removedBox" style="display:none">Removed <b id="removedN">0</b> · <a id="restoreAll">restore all</a></div>
      </div>
    </div>

    <!-- Pinned action footer: always visible regardless of scroll / window height -->
    <div class="sidebar-actions">
      <button class="btn-ghost" id="exportBtn" style="display:none">⬇ Export TOP photos…</button>
      <button class="btn-ghost" id="exportPbgBtn" style="display:none">📱 Export Phone BG…</button>
      <button class="btn-ghost" id="moveBlurryBtn" style="display:none">🗂️ Move blurry → Blurred/</button>
      <button class="btn" id="startBtn">🚀 Start</button>
      <button class="btn god" id="godBtn" title="Run Cull → Dedup → Rank automatically">⚡ God Mode · Run All</button>
    </div>
  </div>
  <div class="main">
    <div class="progress-wrap" id="progressWrap">
      <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
      <div class="progress-text" id="progressText">…</div>
    </div>
    <div class="filter-bar" id="filterBar" style="display:none"></div>
    <div class="pager" id="pager" style="display:none"></div>
    <div class="gallery" id="gallery"><div class="empty"><div class="icon">🎞️</div><div>Pick a folder, then Start</div></div></div>
  </div>
</div>

<div class="lightbox" id="lightbox">
  <div class="lb-bar">
    <div><div id="lbName">—</div><div style="font-size:12px;opacity:.7" id="lbCount"></div></div>
    <div class="lb-actions">
      <button class="lb-btn pbg" id="lbPhoneBg" style="display:none">📱 Phone BG</button>
      <button class="lb-btn toggle" id="lbToggle" style="display:none">→ Blurry</button>
      <button class="lb-btn restore" id="lbRestore" style="display:none">↺ Restore all</button>
      <button class="lb-btn remove" id="lbRemove" style="display:none">✕ Remove</button>
      <button class="lb-close" id="lbClose">✕</button>
    </div>
  </div>
  <button class="lb-nav lb-prev" id="lbPrev">‹</button>
  <img class="lb-img" id="lbImg" src="">
  <button class="lb-nav lb-next" id="lbNext">›</button>
  <div class="lb-side" id="lbSide"></div>
</div>

<div class="toast-wrap" id="toastWrap"></div>

<script>
function toast(msg,type){
  const w=document.getElementById('toastWrap');
  const el=document.createElement('div');el.className='toast '+(type||'good');el.textContent=msg;
  w.appendChild(el);requestAnimationFrame(()=>el.classList.add('show'));
  setTimeout(()=>{el.classList.remove('show');setTimeout(()=>el.remove(),300);},3600);
}
let folder=null, photos=[], lbList=[], lbIndex=0, currentStep='cull';
let lastRankSig='', renderedCount=0, photoIdx=0, lastStep=null, weightTimer=null, removedCount=0;
const CATS=[['aesthetic','Aesthetic'],['composition','Composition'],['technical','Technical'],['sharpness','Sharpness'],['color','Color']];
const DEFAULTS={aesthetic:30,composition:22,technical:20,sharpness:16,color:12};
let weights={...DEFAULTS};

/* theme */
const tt=document.getElementById('themeToggle');
tt.onclick=()=>{const d=document.documentElement.getAttribute('data-theme')==='dark';
  document.documentElement.setAttribute('data-theme',d?'light':'dark');tt.textContent=d?'🌙':'☀️';};

/* settings panels per step */
function settingsHTML(step){
  if(step==='cull') return `<div class="wgroup"><label>Strictness <b id="optVal">1.00</b></label>
      <input type="range" id="opt" min="0.6" max="1.6" step="0.05" value="1.0">
      <div class="slider-value">Lower = keep more · higher = stricter</div></div>
      <label class="check"><input type="checkbox" id="cAdaptive" checked> Adaptive thresholds (per folder)</label>
      <label class="check" style="margin-top:6px"><input type="checkbox" id="cRescue" checked> Quality rescue (protect soft but well-composed)</label>`;
  if(step==='dedup') return `<div class="wgroup"><label>Similarity threshold</label>
      <input type="range" id="opt" min="0.5" max="0.95" step="0.05" value="0.8">
      <div class="slider-value">Group when similarity ≥ <b id="optVal">0.80</b> · lower = more aggressive</div></div>
      <label class="check"><input type="checkbox" id="autoOrg"> Auto-move duplicates → Duplicates/</label>`;
  // rank
  return `<div class="sidebar-title" style="margin-bottom:4px">⚖️ Scoring weights</div>
    <div class="panel-box" id="weightPanel"></div>
    <button class="btn-ghost" id="resetWeights" style="margin-top:8px">↺ Recommended defaults</button>
    <div class="wgroup" style="margin-top:10px"><label>Keep TOP N</label><input type="number" id="topn" min="1" max="500" value="50"></div>`;
}
function renderWeights(){
  const wp=document.getElementById('weightPanel'); if(!wp)return;
  wp.innerHTML=CATS.map(([k,lab])=>`<div class="wgroup"><label>${lab} <b id="wv_${k}">${weights[k]}</b></label>
     <input type="range" min="0" max="50" value="${weights[k]}" id="w_${k}"></div>`).join('');
  CATS.forEach(([k])=>{const el=document.getElementById('w_'+k);
    el.oninput=()=>{weights[k]=parseInt(el.value);document.getElementById('wv_'+k).textContent=el.value;scheduleReweight();};});
}
function scheduleReweight(){clearTimeout(weightTimer);weightTimer=setTimeout(()=>{
  fetch('/api/weights',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({weights,topn:parseInt((document.getElementById('topn')||{}).value)||50})})
    .then(r=>r.json()).then(d=>renderRank(d.photos||[]));},140);}
function applyStepStats(){
  // Show only the stat rows relevant to the current step so irrelevant zeros
  // (e.g. Sharp/Blurry/Unique while Ranking) don't look like errors.
  document.querySelectorAll('.stat-row[data-steps]').forEach(row=>{
    row.style.display=row.dataset.steps.split(' ').includes(currentStep)?'':'none';
  });
}
function renderSettings(){
  applyStepStats();
  document.getElementById('settingsPanel').innerHTML=settingsHTML(currentStep);
  const opt=document.getElementById('opt'),val=document.getElementById('optVal');
  if(opt&&val)opt.oninput=()=>{val.textContent=(currentStep==='dedup'||currentStep==='cull')?parseFloat(opt.value).toFixed(2):opt.value;};
  const ao=document.getElementById('autoOrg');
  if(ao)ao.onchange=()=>fetch('/api/set-auto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({step:currentStep,enabled:ao.checked})}).catch(()=>{});
  if(currentStep==='rank'){renderWeights();
    const rw=document.getElementById('resetWeights');if(rw)rw.onclick=()=>{weights={...DEFAULTS};renderWeights();scheduleReweight();};}
}

/* Switch the visible step (used by tab clicks AND God mode). */
function activateStep(step){
  currentStep=step;
  document.querySelectorAll('.step').forEach(x=>x.classList.toggle('active',x.dataset.step===step));
  renderSettings();
  document.getElementById('exportBtn').style.display='none';
  document.getElementById('exportPbgBtn').style.display='none';
  {const mb=document.getElementById('moveBlurryBtn');mb.style.display='none';mb.classList.add('btn-ghost');mb.classList.remove('btn','cta');startBtn.classList.remove('secondary');}
  document.getElementById('progressWrap').style.display='none';  // clear stale summary
  document.getElementById('gallery').innerHTML=emptyHTML(currentStep);
  lastRankSig='';renderedCount=0;photoIdx=0;lastStep=null;
  gPage=0;lastGallerySig='';gItems=[];document.getElementById('pager').style.display='none';
  setupFilterBar();
}
/* step tabs (blocked while a step is running) */
document.querySelectorAll('.step').forEach(t=>t.onclick=()=>{
  if(isRunning){toast('Stop the current step first.','bad');return;}
  activateStep(t.dataset.step);
});
renderSettings();

/* cull filter chips */
let cullFilter='all', rankFilter='all';
function setupFilterBar(){
  const bar=document.getElementById('filterBar');
  if(currentStep==='cull'){
    const opts=[['all','All'],['sharp','Sharp'],['soft','Soft ★'],['blurry','Blurry']];
    bar.style.display='flex';
    bar.innerHTML=opts.map(([k,l])=>`<button class="chip${k===cullFilter?' active':''}" data-f="${k}">${l}</button>`).join('');
    bar.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{cullFilter=c.dataset.f;
      bar.querySelectorAll('.chip').forEach(x=>x.classList.toggle('active',x.dataset.f===cullFilter));
      renderCullStep(photos);});
    return;
  }
  if(currentStep==='rank'){
    if(!photos.length){bar.style.display='none';return;}
    bar.style.display='flex';
    bar.innerHTML=`<button class="chip${rankFilter==='all'?' active':''}" data-f="all">All TOP photos</button>`
      +`<button class="chip${rankFilter==='pbg'?' active':''}" data-f="pbg">📱 Phone BG (<span id="pbgChipCount">0</span>)</button>`;
    bar.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{rankFilter=c.dataset.f;
      bar.querySelectorAll('.chip').forEach(x=>x.classList.toggle('active',x.dataset.f===rankFilter));
      lastRankSig='';renderRank(photos);});
    return;
  }
  bar.style.display='none';
}
setupFilterBar();
document.getElementById('gallery').innerHTML=emptyHTML(currentStep);  // step explainer on load

/* shortcuts */
function loadShortcuts(){fetch('/api/shortcuts').then(r=>r.json()).then(d=>{
  let h='';(d.sd||[]).forEach(p=>h+=`<button class="shortcut" data-p="${p}"><span class="tag sd">SD</span>${p.split('/').slice(-2).join('/')}</button>`);
  (d.recent||[]).slice(0,4).forEach(p=>h+=`<button class="shortcut" data-p="${p}"><span class="tag recent">RECENT</span>${p.split('/').slice(-2).join('/')}</button>`);
  document.getElementById('shortcuts').innerHTML=h;
  document.querySelectorAll('.shortcut').forEach(b=>b.onclick=()=>{folder=b.dataset.p;document.getElementById('folderInput').value=folder;});});}
loadShortcuts();
document.getElementById('folderInput').oninput=e=>folder=e.target.value.trim();
document.getElementById('browseBtn').onclick=()=>fetch('/api/browse',{method:'POST'}).then(r=>r.json()).then(d=>{
  if(d.folder){folder=d.folder;document.getElementById('folderInput').value=folder;loadShortcuts();}});

/* start / stop (the same button toggles) */
let isRunning=false, runningStep=null;
const startBtn=document.getElementById('startBtn');
function setStartBtn(running){
  isRunning=running;
  startBtn.textContent=running?'■ Stop':'🚀 Start';
  startBtn.classList.toggle('stopping',running);
}
function startStep(step){
  runningStep=step;
  document.getElementById('progressWrap').style.display='block';
  document.getElementById('gallery').innerHTML='';
  lastRankSig='';renderedCount=0;photoIdx=0;lastStep=step;
  gPage=0;lastGallerySig='';document.getElementById('pager').style.display='none';
  document.getElementById('exportBtn').style.display='none';
  document.getElementById('exportPbgBtn').style.display='none';
  {const mb=document.getElementById('moveBlurryBtn');mb.style.display='none';mb.classList.add('btn-ghost');mb.classList.remove('btn','cta');startBtn.classList.remove('secondary');}setRemoved(0);
  setStartBtn(true);
  // Settings come from the active step's panel (activateStep switched it first).
  const opt=document.getElementById('opt');
  const ad=document.getElementById('cAdaptive'),rs=document.getElementById('cRescue');
  fetch('/api/run/'+step,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({folder,opt:opt?parseFloat(opt.value):0,
      adaptive:ad?ad.checked:true, rescue:rs?rs.checked:true,
      topn:parseInt((document.getElementById('topn')||{}).value)||50})});
  setTimeout(()=>poll(step),200);
}
function doStart(){
  if(!folder){toast('Pick a folder first','bad');return;}
  startStep(currentStep);
}
function doStop(){
  if(!runningStep)return;
  startBtn.textContent='Stopping…';startBtn.disabled=true;
  fetch('/api/stop/'+runningStep,{method:'POST'}).finally(()=>{startBtn.disabled=false;});
}
startBtn.onclick=()=>{ isRunning?doStop():doStart(); };

/* ---- God mode: run Cull → Dedup → Rank back-to-back ---- */
let godMode=false, godAbort=false, godResolve=null;
const godBtn=document.getElementById('godBtn');
function setGodBtn(on){ godBtn.textContent=on?'■ Stop God Mode':'⚡ God Mode · Run All'; godBtn.classList.toggle('stopping',on); }
async function godRun(){
  if(!folder){toast('Pick a folder first','bad');return;}
  if(isRunning){toast('Stop the current step first.','bad');return;}
  godMode=true;godAbort=false;setGodBtn(true);startBtn.disabled=true;
  try{
    for(const step of ['cull','dedup','rank']){
      if(godAbort)break;
      activateStep(step);
      // Lock the auto-move checkbox during a God-mode run (only present on the
      // Dedup step) so it can't be toggled mid-pipeline.
      {const ao=document.getElementById('autoOrg'); if(ao)ao.disabled=true;}
      await new Promise(res=>{ godResolve=res; startStep(step); });
    }
    if(!godAbort)toast('✨ God mode complete — your TOP photos are ranked','good');
  } finally {
    godMode=false;setGodBtn(false);startBtn.disabled=false;
    const ao=document.getElementById('autoOrg'); if(ao)ao.disabled=false;  // re-enable
  }
}
godBtn.onclick=()=>{
  if(godMode){ godAbort=true; doStop(); toast('Stopping God mode…','bad'); }
  else godRun();
};
function poll(step){
  fetch('/api/progress/'+step).then(r=>r.json()).then(d=>{
    document.getElementById('progressFill').style.width=d.progress+'%';
    document.getElementById('progressText').textContent=d.status;
    const st=d.stats||{};
    if('images'in st)document.getElementById('sImages').textContent=st.images;
    if('sharp'in st)document.getElementById('sSharp').textContent=st.sharp;
    if('blurry'in st)document.getElementById('sBlurry').textContent=st.blurry;
    if('soft'in st)document.getElementById('sSoft').textContent=st.soft;
    if('groups'in st)document.getElementById('sGroups').textContent=st.groups;
    if(step==='rank')renderRank(d.photos||[]);
    else if(step==='cull')renderCullStep(d.photos||[]);
    else renderGallery(d.photos||[]);
    if(d.running)setTimeout(()=>poll(step),300);
    else{
      // Finished: keep the summary line visible (full bar + "Done in … · Sharp
      // N (x%) / Soft … / Blurry …") instead of hiding it.
      document.getElementById('progressFill').style.width='100%';
      setStartBtn(false);runningStep=null;
      if(step==='rank'&&photos.length)document.getElementById('exportBtn').style.display='block';
      // After Cull, the obvious next action is moving the blurry shots: make
      // that the primary (blue, emphasised) button and mute Start. Suppressed
      // during God mode (the pipeline moves straight on to Dedup).
      if(!godMode&&step==='cull'&&(st.blurry||0)>0){
        const mb=document.getElementById('moveBlurryBtn');
        mb.style.display='block';mb.classList.remove('btn-ghost');mb.classList.add('btn','cta');
        startBtn.classList.add('secondary');
      }
      // God mode: let the orchestrator advance to the next step.
      if(godResolve){const r=godResolve;godResolve=null;r();}}
  });
}

/* ---- radar ---- */
function radarSVG(metrics,size=210){
  const cx=size/2,cy=size/2,R=size/2-30,n=metrics.length;
  const ang=i=>-Math.PI/2+i*2*Math.PI/n,pt=(i,r)=>[cx+Math.cos(ang(i))*r,cy+Math.sin(ang(i))*r];
  let grid='';[0.25,0.5,0.75,1].forEach(f=>{const p=metrics.map((m,i)=>pt(i,R*f).map(v=>v.toFixed(1)).join(',')).join(' ');grid+=`<polygon points="${p}" fill="none" stroke="rgba(255,255,255,.18)"/>`;});
  let sp='',lb='';metrics.forEach((m,i)=>{const[x,y]=pt(i,R);sp+=`<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="rgba(255,255,255,.18)"/>`;
    const[lx,ly]=pt(i,R+15);lb+=`<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" font-size="9.5" fill="rgba(255,255,255,.85)" text-anchor="middle" dominant-baseline="middle">${m.label}</text>`;});
  const dp=metrics.map((m,i)=>pt(i,R*Math.max(0,Math.min(1,(m.value||0)/100))).map(v=>v.toFixed(1)).join(',')).join(' ');
  return `<svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}">${grid}${sp}<polygon points="${dp}" fill="rgba(96,165,250,.35)" stroke="#60a5fa" stroke-width="2"/>${lb}</svg>`;
}

/* ---- renderers ---- */
const EMPTY='<div class="empty"><div class="icon">🎞️</div><div>No results</div></div>';
function emptyHTML(step){
  const C={
    cull:['✂️','Step 1 · Cull','Drop the out-of-focus shots before anything else.',
      ['🔍 Measures real sharpness — haze &amp; night skies aren’t mistaken for blur',
       '🟢 Sharp &nbsp;·&nbsp; 🟠 Soft (recoverable) &nbsp;·&nbsp; 🔴 Blurry',
       '📁 Pick a folder, then press <b>Start</b>']],
    dedup:['🪢','Step 2 · Dedup','Collapse burst sequences down to a single best frame.',
      ['📸 Near-identical shots are grouped automatically',
       '⭐ The sharpest frame wins — labelled “Best of N”',
       '🚀 Press <b>Start</b> — uses your Cull keepers, or the whole folder']],
    rank:['🏆','Step 3 · Rank','Surface your very best photos.',
      ['🎯 Scores composition, lighting, focus, color &amp; contrast',
       '🥇 Shows your TOP N with a per-photo radar chart',
       '⬇️ Press <b>Start</b>, then export the keepers']]
  };
  const c=C[step]||C.cull;
  return `<div class="empty"><div class="icon">${c[0]}</div>
    <div class="title">${c[1]}</div><p>${c[2]}</p>
    <div class="lines">${c[3].map(l=>`<div>${l}</div>`).join('')}</div></div>`;
}
function cullCard(p){
  const i=photoIdx++;const path=String(p.path).replace(/"/g,'&quot;');
  const isDedup=currentStep==='dedup';
  // Dedup: the badge tells you this frame won a burst ("Best of N"); the info
  // line says how many near-duplicates were set aside. The raw sharpness number
  // (used only to pick the winner) is no longer shown — it wasn't meaningful.
  const g=p.group||1;
  const badge=isDedup
    ? `<div class="badge good">${g>1?('★ Best of '+g):'KEPT'}</div>`
    : (p.badge?`<div class="badge ${p.badgeType}">${p.badge}</div>`:'');
  const toggle=currentStep==='cull'?`<button class="status-toggle" data-path="${path}">${p.kept?'→ Blurry':'✓ Keep'}</button>`:'';
  const cls=p.kept?'kept':(p.rejected?'rejected':'');
  const info=isDedup
    ? `<div class="photo-score" style="font-weight:500;opacity:.75">${g>1?((g-1)+' similar set aside'):'Original'}</div>`
    : `<div class="photo-score">${p.score}</div>`;
  return `<div class="photo-card ${cls}" data-i="${i}" data-path="${path}">${badge}${toggle}
    <img class="photo-img" src="${p.thumb}" loading="lazy" decoding="async">
    <div class="photo-info"><div class="pi-row"><span class="photo-name">${p.name}</span></div>${info}</div></div>`;
}
let lastGallerySig='', gPage=0, gItems=[];
const PAGE_SIZE=400;
function renderGallery(items){   /* dedup: paginated + reconciling (order-stable) */
  gItems=items;photos=items;const g=document.getElementById('gallery');
  if(lastStep!==currentStep){g.innerHTML='';lastGallerySig='';lastStep=currentStep;gPage=0;}
  if(!items.length){g.innerHTML=EMPTY;lastGallerySig='';renderedCount=0;updatePager();document.getElementById('sShowing').textContent=0;return;}
  const pages=Math.max(1,Math.ceil(items.length/PAGE_SIZE));
  if(gPage>=pages)gPage=pages-1;if(gPage<0)gPage=0;
  const start=gPage*PAGE_SIZE,end=Math.min(items.length,start+PAGE_SIZE);
  const slice=items.slice(start,end);
  // Signature includes the page + group size so paging and growing clusters
  // ("Best of N") always re-render; reconcile within.
  const sig=gPage+'#'+slice.map(p=>p.path+':'+(p.group||1)).join('|');
  if(sig===lastGallerySig){updatePager();document.getElementById('sShowing').textContent=items.length;return;}
  lastGallerySig=sig;
  const emp=g.querySelector('.empty');if(emp)emp.remove();
  // Reuse existing card nodes by path so reordering/paging never duplicates
  // thumbnails or reloads images. data-i keeps the GLOBAL index (lightbox).
  const existing={};g.querySelectorAll('.photo-card').forEach(n=>existing[n.dataset.path]=n);
  const frag=document.createDocumentFragment();
  slice.forEach((p,k)=>{const i=start+k;const key=String(p.path);let node=existing[key];
    if(node){
      // Keep reused cards in sync. For Dedup show "Best of N"/"no duplicates"
      // (never the raw sharpness number); the badge updates as clusters grow.
      const g=p.group||1;
      const b=node.querySelector('.badge');
      const sc=node.querySelector('.photo-score');
      if(b)b.textContent=(g>1?('★ Best of '+g):'KEPT');
      if(sc)sc.textContent=(g>1?((g-1)+' similar set aside'):'Original');
      node.dataset.i=i;delete existing[key];}
    else{const w=document.createElement('div');w.innerHTML=cullCard(p);node=w.firstElementChild;node.dataset.i=i;}
    frag.appendChild(node);});
  Object.values(existing).forEach(n=>n.remove());g.appendChild(frag);
  renderedCount=slice.length;updatePager();
  document.getElementById('sShowing').textContent=items.length;
}
function updatePager(){
  const pager=document.getElementById('pager');if(!pager)return;
  const total=gItems.length,pages=Math.max(1,Math.ceil(total/PAGE_SIZE));
  if(currentStep!=='dedup'||total<=PAGE_SIZE){pager.style.display='none';return;}
  const start=gPage*PAGE_SIZE+1,end=Math.min(total,(gPage+1)*PAGE_SIZE);
  pager.style.display='flex';
  pager.innerHTML=`<button id="pgPrev" ${gPage===0?'disabled':''}>← Prev</button>`
    +`<span>Page ${gPage+1} / ${pages} · ${start}–${end} of ${total}</span>`
    +`<button id="pgNext" ${gPage>=pages-1?'disabled':''}>Next →</button>`;
  document.getElementById('pgPrev').onclick=()=>{if(gPage>0){gPage--;lastGallerySig='';renderGallery(gItems);window.scrollTo(0,0);}};
  document.getElementById('pgNext').onclick=()=>{if(gPage<pages-1){gPage++;lastGallerySig='';renderGallery(gItems);window.scrollTo(0,0);}};
}
function rankCard(p,idx){const path=String(p.path).replace(/"/g,'&quot;');
  const on=p.phonebg?' on':'';
  return `<div class="photo-card kept${p.phonebg?' pbg':''}" data-i="${idx}" data-path="${path}"><div class="rank-num">${p.rank!=null?p.rank:idx+1}</div>
    <button class="pbg-toggle${on}" data-path="${path}" title="${p.phonebg?'Phone wallpaper ✓ — click to remove':'Set as phone wallpaper'}">📱</button>
    <img class="photo-img" src="${p.thumb}" loading="lazy" decoding="async">
    <div class="photo-info"><div class="pi-row"><span class="photo-name">${p.name}</span>
      <button class="remove-btn" data-path="${path}" title="Remove from ranking (does not delete the file)">✕ Remove</button></div>
      <div class="photo-score">${p.score}</div></div></div>`;}
function renderRank(items){
  photos=items;const g=document.getElementById('gallery');
  if(lastStep!==currentStep){g.innerHTML='';lastRankSig='';lastStep=currentStep;}
  const fbar=document.getElementById('filterBar');
  if(!items.length){fbar.style.display='none';}
  else if(fbar.style.display==='none'||!fbar.querySelector('.chip')){setupFilterBar();}
  const view=(rankFilter==='pbg')?items.filter(p=>p.phonebg):items;
  const pbgN=items.filter(p=>p.phonebg).length;
  const pbgChip=document.getElementById('pbgChipCount');if(pbgChip)pbgChip.textContent=pbgN;
  document.getElementById('exportPbgBtn').style.display=(currentStep==='rank'&&pbgN>0)?'block':'none';
  if(!view.length){g.innerHTML=(items.length&&rankFilter==='pbg')?'<div class="empty"><div class="icon">📱</div><div>No photos flagged as phone wallpaper yet.<br>Tap the 📱 button on a top photo.</div></div>':EMPTY;lastRankSig='';return;}
  const sig=rankFilter+'|'+view.map(p=>p.rank+':'+p.path+':'+(p.phonebg?1:0)).join('|');if(sig===lastRankSig)return;lastRankSig=sig;
  const emp=g.querySelector('.empty');if(emp)emp.remove();
  const existing={};g.querySelectorAll('.photo-card').forEach(n=>existing[n.dataset.path]=n);
  const frag=document.createDocumentFragment();
  view.forEach((p,idx)=>{let node=existing[p.path];
    if(node&&node.classList.contains('pbg')===!!p.phonebg){const rn=node.querySelector('.rank-num');if(rn)rn.textContent=p.rank!=null?p.rank:idx+1;
      const sc=node.querySelector('.photo-score');if(sc)sc.textContent=p.score;node.dataset.i=idx;delete existing[p.path];}
    else{if(node)node.remove();const w=document.createElement('div');w.innerHTML=rankCard(p,idx);node=w.firstElementChild;}
    frag.appendChild(node);});
  Object.values(existing).forEach(n=>n.remove());g.appendChild(frag);
  document.getElementById('sShowing').textContent=view.length;
}
function togglePhoneBg(path){
  fetch('/api/toggle-phonebg',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})}).then(r=>r.json()).then(d=>{
    if(d.error){toast(d.error,'bad');return;}
    const pp=photos.find(x=>x.path===path);if(pp)pp.phonebg=d.phonebg;
    lastRankSig='';renderRank(photos);
    if(document.getElementById('lightbox').classList.contains('open')){lbList=(rankFilter==='pbg')?photos.filter(p=>p.phonebg):photos.slice();if(lbIndex>=lbList.length)lbIndex=Math.max(0,lbList.length-1);if(lbList.length)showLb();else closeLb();}
    toast(d.phonebg?'📱 Added to Phone BG ('+d.count+')':'Removed from Phone BG ('+d.count+')','good');});
}

/* ---- cull (3-tier, reconciling, filterable) ---- */
let cullView=[], lastCullSig='';
const TIER_NAME={sharp:'Sharp',soft:'Soft',blurry:'Blurry'};
const NEXT_TIER={sharp:'soft',soft:'blurry',blurry:'sharp'};
function cullCardHtml(p,idx){const path=String(p.path).replace(/"/g,'&quot;');
  const cls=p.tier==='sharp'?'kept':p.tier==='soft'?'soft':'rejected';
  return `<div class="photo-card ${cls}" data-i="${idx}" data-path="${path}" data-tier="${p.tier}">
    <div class="badge ${p.badgeType}">${p.badge}</div>
    <button class="status-toggle" data-path="${path}" data-tier="${p.tier}">⇄ ${TIER_NAME[p.tier]}</button>
    <img class="photo-img" src="${p.thumb}" loading="lazy" decoding="async">
    <div class="photo-info"><div class="pi-row"><span class="photo-name">${p.name}</span></div><div class="photo-score">${p.score}</div></div></div>`;}
function renderCullStep(items){
  photos=items;
  cullView=items.filter(p=>cullFilter==='all'?true:p.tier===cullFilter);
  const g=document.getElementById('gallery');
  if(!cullView.length){g.innerHTML=EMPTY;lastCullSig='';lastStep=currentStep;document.getElementById('sShowing').textContent=0;return;}
  const sig=cullView.map(p=>p.path+':'+p.tier).join('|');
  if(sig===lastCullSig&&lastStep===currentStep)return;
  lastCullSig=sig;lastStep=currentStep;
  const emp=g.querySelector('.empty');if(emp)emp.remove();
  const existing={};g.querySelectorAll('.photo-card').forEach(n=>existing[n.dataset.path]=n);
  const frag=document.createDocumentFragment();
  cullView.forEach((p,idx)=>{let node=existing[p.path];
    if(node&&node.dataset.tier===p.tier){node.dataset.i=idx;delete existing[p.path];}
    else{if(node)node.remove();const w=document.createElement('div');w.innerHTML=cullCardHtml(p,idx);node=w.firstElementChild;}
    frag.appendChild(node);});
  Object.values(existing).forEach(n=>n.remove());g.appendChild(frag);
  document.getElementById('sShowing').textContent=cullView.length;
}
function cullSetTier(path,tier){
  fetch('/api/toggle-status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path,tier})}).then(r=>r.json()).then(d=>{
    if(d.error){toast(d.error,'bad');return;}
    document.getElementById('sSharp').textContent=d.sharp;document.getElementById('sSoft').textContent=d.soft;document.getElementById('sBlurry').textContent=d.blurry;
    const pp=photos.find(x=>x.path===path||x.path===d.path);
    if(pp){pp.tier=d.tier;pp.badge=d.badge;pp.badgeType=d.badgeType;pp.kept=d.kept;pp.rejected=!d.kept;if(d.path)pp.path=d.path;if(d.thumb)pp.thumb=d.thumb;}
    lastCullSig='';renderCullStep(photos);});
}

/* ---- remove / restore (rank) ---- */
function setRemoved(n){removedCount=n;document.getElementById('removedN').textContent=n;
  document.getElementById('removedBox').style.display=n>0?'block':'none';
  document.getElementById('lbRestore').style.display=(n>0&&currentStep==='rank')?'inline-block':'none';}
function removePhoto(path){fetch('/api/exclude',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})}).then(r=>r.json()).then(d=>{renderRank(d.photos||[]);setRemoved(d.removed);});}
function restoreAll(syncLb){fetch('/api/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all:true})}).then(r=>r.json()).then(d=>{renderRank(d.photos||[]);setRemoved(d.removed);
  if(syncLb&&document.getElementById('lightbox').classList.contains('open')){lbList=photos.slice();if(lbIndex>=lbList.length)lbIndex=lbList.length-1;if(lbList.length)showLb();else closeLb();}});}
document.getElementById('restoreAll').onclick=()=>restoreAll(false);

/* ---- cull status toggle ---- */
function toggleStatus(path,btn,cb){
  fetch('/api/toggle-status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})}).then(r=>r.json()).then(d=>{
    if(d.error){toast(d.error,'bad');return;}
    document.getElementById('sSharp').textContent=d.sharp;document.getElementById('sBlurry').textContent=d.blurry;
    if(cb)cb(d);});
}

/* ---- gallery clicks ---- */
document.getElementById('gallery').addEventListener('click',e=>{
  const rm=e.target.closest('.remove-btn');if(rm){e.stopPropagation();removePhoto(rm.dataset.path);return;}
  const pb=e.target.closest('.pbg-toggle');
  if(pb){e.stopPropagation();togglePhoneBg(pb.dataset.path);return;}
  const tg=e.target.closest('.status-toggle');
  if(tg){e.stopPropagation();cullSetTier(tg.dataset.path,NEXT_TIER[tg.dataset.tier||'sharp']);return;}
  const c=e.target.closest('.photo-card');if(!c)return;
  lbList=(currentStep==='cull')?cullView.slice():(currentStep==='rank'&&rankFilter==='pbg')?photos.filter(p=>p.phonebg):photos.slice();
  openLb(parseInt(c.dataset.i));});

/* ---- lightbox ---- */
function openLb(i){lbIndex=i;showLb();document.getElementById('lightbox').classList.add('open');}
function closeLb(){document.getElementById('lightbox').classList.remove('open');}
const CATINFO={aesthetic:'Overall "magazine appeal" — a transparent blend of composition, color, sharpness, dynamic range and exposure.',
  composition:'Subject placement, horizon leveling and visual balance.',technical:'Exposure, dynamic range, tonal spread, white balance and noise.',
  sharpness:'Contrast-normalized focus. High = crisp; haze does NOT count as blur.',color:'Vividness plus how well the hues relate.'};
const SUBINFO={'Rule of thirds':'Closeness of the main subject to a rule-of-thirds / golden-ratio point.','Horizon level':'How level the dominant straight line is (100=straight, ~60=no clear horizon).',
  'Balance':'Even spread of visual weight left vs right.','Exposure':'Freedom from clipped blacks/whites.','Dynamic range':'Spread between deepest shadow and brightest highlight.',
  'Tonal range':'How richly tones fill the histogram (entropy).','White balance':'Neutrality of color cast (artistic warm/cool lowers it).','Noise (clean)':'Cleanliness in flat areas (high=clean).',
  'Colorfulness':'Saturation & color variety.','Color harmony':'How well dominant hues relate (analogous/complementary).'};
const GROUPS=[['composition','Composition',['Rule of thirds','Horizon level','Balance']],
  ['technical','Technical',['Exposure','Dynamic range','Tonal range','White balance','Noise (clean)']],['color','Color',['Colorfulness','Color harmony']]];
function barColor(v){return v>=70?'#22c55e':v>=45?'#f59e0b':'#ef4444';}
function barRow(label,v,info,cat){const t=(info||'').replace(/"/g,'&quot;');
  return `<div class="bar${cat?' cat':''}" title="${label}: ${t}"><span class="lab">${label}</span><span class="track"><span class="fill" style="width:${v}%;background:${barColor(v)}"></span></span><span class="num">${v}</span></div>`;}
function showLb(){
  const p=lbList[lbIndex];if(!p)return;
  document.getElementById('lbImg').src='/api/image?path='+encodeURIComponent(p.path);
  document.getElementById('lbName').textContent=(p.rank!=null?'#'+p.rank+'  ':'')+p.name;
  const extra=(currentStep==='dedup')
    ? ((p.group>1)?('   ·   Best of '+p.group+' ('+(p.group-1)+' set aside)'):'   ·   Original')
    : (p.score!=null?'   ·   '+p.score:'');
  document.getElementById('lbCount').textContent=(lbIndex+1)+' / '+lbList.length+extra;
  const rm=document.getElementById('lbRemove'),rs=document.getElementById('lbRestore'),tg=document.getElementById('lbToggle');
  rm.style.display=currentStep==='rank'?'inline-block':'none';
  rs.style.display=(currentStep==='rank'&&removedCount>0)?'inline-block':'none';
  tg.style.display=currentStep==='cull'?'inline-block':'none';
  if(currentStep==='cull')tg.textContent='⇄ '+(TIER_NAME[p.tier]||'Sharp')+' → '+(TIER_NAME[NEXT_TIER[p.tier||'sharp']]);
  const pbg=document.getElementById('lbPhoneBg');
  pbg.style.display=currentStep==='rank'?'inline-block':'none';
  if(currentStep==='rank'){pbg.classList.toggle('on',!!p.phonebg);pbg.textContent=p.phonebg?'📱 Phone BG ✓':'📱 Phone BG';}
  const side=document.getElementById('lbSide');
  if(currentStep==='rank'&&p.scores){
    const metrics=CATS.map(([k,lab])=>({label:lab,value:(p.scores&&p.scores[k])||0}));
    let html=`<div style="text-align:center">${radarSVG(metrics,230)}</div><h3>Category scores</h3>`;
    CATS.forEach(([k,lab])=>html+=barRow(lab,(p.scores&&p.scores[k])||0,CATINFO[k],true));
    const d=p.detail||{};GROUPS.forEach(([k,lab,keys])=>{const cv=(p.scores&&p.scores[k]);html+=`<h3>${lab}<span>${cv!=null?cv:''}</span></h3>`;keys.forEach(key=>{if(key in d)html+=barRow(key,d[key],SUBINFO[key]);});});
    html+=`<div style="font-size:10px;opacity:.5;margin-top:14px">Hover any row for what it measures.</div>`;
    side.style.display='block';side.innerHTML=html;
  }else{side.style.display='block';side.innerHTML=`<h3>${currentStep==='cull'?'Sharpness':'Photo'}</h3><div style="font-size:13px;opacity:.85">${p.name}</div><div style="font-size:26px;font-weight:700;margin-top:8px">${p.score!=null?p.score:''}</div>`;}
  loadExif(p.path,side);
}
function exifRow(label,val){return `<div class="exrow"><span class="lab">${label}</span><span class="val">${val}</span></div>`;}
function loadExif(path,side){
  const token=path;side.dataset.exifToken=token;
  fetch('/api/exif?path='+encodeURIComponent(path)).then(r=>r.json()).then(e=>{
    if(side.dataset.exifToken!==token)return; // user moved on
    let rows='';
    if(e.date)rows+=exifRow('Date',e.date);
    if(e.time)rows+=exifRow('Time',(e.time||'').split('+')[0]);
    if(e.camera)rows+=exifRow('Camera',e.camera);
    if(e.lens)rows+=exifRow('Lens',e.lens);
    const settings=[e.focal,e.aperture,e.shutter,e.iso].filter(Boolean)
      .map(s=>`<span style="white-space:nowrap">${s}</span>`).join(' · ');
    if(settings)rows+=exifRow('Settings',settings);
    if(e.lat!=null&&e.lon!=null){
      const c=e.lat.toFixed(5)+',&nbsp;'+e.lon.toFixed(5);
      rows+=exifRow('Location',`<a href="https://www.google.com/maps?q=${e.lat},${e.lon}" target="_blank" style="white-space:nowrap">${c}</a>`);
      const z=13,n=Math.pow(2,z),latRad=e.lat*Math.PI/180;
      const xt=(e.lon+180)/360*n, yt=(1-Math.log(Math.tan(latRad)+1/Math.cos(latRad))/Math.PI)/2*n;
      const xtile=Math.floor(xt), ytile=Math.floor(yt);
      const px=Math.round((xt-xtile)*256), py=Math.round((yt-ytile)*256);
      const tile='https://tile.openstreetmap.org/'+z+'/'+xtile+'/'+ytile+'.png';
      rows+=`<a href="https://www.openstreetmap.org/?mlat=${e.lat}&mlon=${e.lon}#map=15/${e.lat}/${e.lon}" target="_blank" class="exmap"><span class="tilewrap"><img src="${tile}" alt="Map" loading="lazy" onerror="this.closest('.exmap').style.display='none'"><span class="pin" style="left:${px}px;top:${py}px"></span></span><span class="cred">© OpenStreetMap contributors</span></a>`;
    }
    if(!rows)rows=`<div style="font-size:12px;opacity:.5">No EXIF metadata.</div>`;
    side.insertAdjacentHTML('beforeend',`<h3>Details</h3>${rows}`);
  }).catch(()=>{});
}
document.getElementById('lbClose').onclick=closeLb;
document.getElementById('lightbox').addEventListener('click',e=>{if(e.target.id==='lightbox')closeLb();});
document.getElementById('lbPrev').onclick=()=>{lbIndex=(lbIndex-1+lbList.length)%lbList.length;showLb();};
document.getElementById('lbNext').onclick=()=>{lbIndex=(lbIndex+1)%lbList.length;showLb();};
function lbRemoveCurrent(){const p=lbList[lbIndex];if(!p)return;
  fetch('/api/exclude',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:p.path})}).then(r=>r.json()).then(d=>{
    renderRank(d.photos||[]);setRemoved(d.removed);lbList=photos.slice();
    if(!lbList.length){closeLb();return;}if(lbIndex>=lbList.length)lbIndex=lbList.length-1;showLb();});}
document.getElementById('lbRemove').onclick=lbRemoveCurrent;
document.getElementById('lbRestore').onclick=()=>restoreAll(true);
document.getElementById('lbToggle').onclick=()=>{const p=lbList[lbIndex];if(!p)return;
  const next=NEXT_TIER[p.tier||'sharp'];
  fetch('/api/toggle-status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:p.path,tier:next})}).then(r=>r.json()).then(d=>{
    if(d.error){toast(d.error,'bad');return;}
    document.getElementById('sSharp').textContent=d.sharp;document.getElementById('sSoft').textContent=d.soft;document.getElementById('sBlurry').textContent=d.blurry;
    const pp=photos.find(x=>x.path===p.path||x.path===d.path);
    if(pp){pp.tier=d.tier;pp.badge=d.badge;pp.badgeType=d.badgeType;pp.kept=d.kept;pp.rejected=!d.kept;if(d.path)pp.path=d.path;if(d.thumb)pp.thumb=d.thumb;}
    p.tier=d.tier;if(d.path)p.path=d.path;
    lastCullSig='';renderCullStep(photos);showLb();});};
document.getElementById('lbPhoneBg').onclick=()=>{const p=lbList[lbIndex];if(p)togglePhoneBg(p.path);};
document.addEventListener('keydown',e=>{
  if(!document.getElementById('lightbox').classList.contains('open'))return;
  if(e.key==='Escape')closeLb();
  if(e.key==='ArrowLeft')document.getElementById('lbPrev').click();
  if(e.key==='ArrowRight')document.getElementById('lbNext').click();
  if(currentStep==='rank'&&(e.key==='b'||e.key==='B')){e.preventDefault();const p=lbList[lbIndex];if(p)togglePhoneBg(p.path);}
  if(currentStep==='rank'&&(e.key==='x'||e.key==='X'||e.key==='Delete'||e.key==='Backspace')){e.preventDefault();lbRemoveCurrent();}});

/* export */
document.getElementById('exportBtn').onclick=function(){
  this.disabled=true;this.textContent='Exporting…';
  fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({topn:parseInt((document.getElementById('topn')||{}).value)||50})})
    .then(r=>r.json()).then(d=>{this.disabled=false;this.textContent='⬇ Export TOP photos…';
      toast(d.error?('Export failed: '+d.error):('✓ Copied '+d.copied+' photos to\n'+d.dest), d.error?'bad':'good');});
};
document.getElementById('exportPbgBtn').onclick=function(){
  this.disabled=true;this.textContent='Exporting…';
  fetch('/api/export-phonebg',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})})
    .then(r=>r.json()).then(d=>{this.disabled=false;this.textContent='📱 Export Phone BG…';
      if(d.error){toast('Export failed: '+d.error,'bad');return;}
      if(!d.copied&&!d.cropped){toast(d.note||'Nothing flagged as Phone BG.','bad');return;}
      toast('✓ '+d.copied+' originals + '+d.cropped+' wallpapers (1290×2796) to\n'+d.dest,'good');});
};
document.getElementById('moveBlurryBtn').onclick=function(){
  const b=document.querySelectorAll('.photo-card[data-tier="blurry"]').length;
  if(!confirm('Move blurry photos to a Blurred/ subfolder?\n\nThey are moved (not deleted) — you can move them back anytime.'))return;
  this.disabled=true;this.textContent='Moving…';
  fetch('/api/move-blurry',{method:'POST'})
    .then(r=>r.json()).then(d=>{this.disabled=false;this.textContent='🗂️ Move blurry → Blurred/';
      if(d.error){toast('Move failed: '+d.error,'bad');return;}
      toast('✓ Moved '+d.moved+' blurry photos to\n'+d.dest,'good');
      this.style.display='none';this.classList.remove('cta');startBtn.classList.remove('secondary');});
};
</script></body></html>'''


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #
@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/api/shortcuts')
def api_shortcuts():
    return jsonify({'sd': detect_sd_cards(), 'recent': load_recents()})


@app.route('/api/browse', methods=['POST'])
def api_browse():
    folder = native_folder_dialog("Select your photo folder")
    if folder and Path(folder).is_dir():
        state['folder'] = folder
        save_recent(folder)
        return jsonify({'folder': folder})
    return jsonify({'folder': None})


@app.route('/api/thumb')
def api_thumb():
    path = request.args.get('path', '')
    p = Path(path)
    if not p.is_file() or p.suffix.lower() not in IMG_EXTS:
        abort(404)
    f = make_thumb_file(path)
    if not f:
        abort(404)
    resp = send_file(str(f))
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/api/image')
def api_image():
    path = request.args.get('path', '')
    p = Path(path)
    if not p.is_file() or p.suffix.lower() not in IMG_EXTS:
        abort(404)
    return send_file(str(p))


def _ratio(v):
    try:
        return float(v)
    except Exception:
        try:
            return v.numerator / v.denominator
        except Exception:
            return None


def _gps_to_deg(val, ref):
    try:
        d = _ratio(val[0]); m = _ratio(val[1]); s = _ratio(val[2])
        deg = d + m / 60.0 + s / 3600.0
        if ref in ('S', 'W'):
            deg = -deg
        return round(deg, 6)
    except Exception:
        return None


def extract_exif(path):
    """Pull human-friendly capture details from a photo's EXIF."""
    from PIL.ExifTags import TAGS, GPSTAGS
    out = {}
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return out
            tags = {TAGS.get(k, k): v for k, v in exif.items()}
            # Date / time
            dt = tags.get('DateTimeOriginal') or tags.get('DateTime')
            if isinstance(dt, str) and ' ' in dt:
                d, t = dt.split(' ', 1)
                out['date'] = d.replace(':', '-')
                out['time'] = t
            # Camera / lens
            make = (tags.get('Make') or '').strip()
            model = (tags.get('Model') or '').strip()
            if make or model:
                out['camera'] = (make + ' ' + model).strip() if model and not model.startswith(make) else (model or make)
            lens = tags.get('LensModel')
            if lens:
                out['lens'] = str(lens).strip()
            # Shooting settings (live in the Exif sub-IFD)
            try:
                sub = exif.get_ifd(0x8769)
                subtags = {TAGS.get(k, k): v for k, v in sub.items()}
            except Exception:
                subtags = {}
            dto = subtags.get('DateTimeOriginal')
            if isinstance(dto, str) and ' ' in dto and 'date' not in out:
                d, t = dto.split(' ', 1)
                out['date'] = d.replace(':', '-'); out['time'] = t
            fnum = _ratio(subtags.get('FNumber'))
            if fnum:
                out['aperture'] = 'f/' + (str(int(fnum)) if fnum == int(fnum) else str(round(fnum, 1)))
            exp = subtags.get('ExposureTime')
            if exp is not None:
                er = _ratio(exp)
                if er and er < 1:
                    out['shutter'] = '1/' + str(int(round(1 / er))) + 's'
                elif er:
                    out['shutter'] = str(round(er, 1)) + 's'
            iso = subtags.get('ISOSpeedRatings') or subtags.get('PhotographicSensitivity')
            if iso:
                out['iso'] = 'ISO ' + str(iso if not isinstance(iso, (list, tuple)) else iso[0])
            fl = _ratio(subtags.get('FocalLength'))
            if fl:
                out['focal'] = str(int(round(fl))) + 'mm'
            if not subtags.get('LensModel') and subtags.get('LensModel') is None and 'lens' not in out:
                lm = subtags.get('LensModel')
                if lm:
                    out['lens'] = str(lm).strip()
            # GPS
            try:
                gps = exif.get_ifd(0x8825)
            except Exception:
                gps = None
            if gps:
                g = {GPSTAGS.get(k, k): v for k, v in gps.items()}
                lat = _gps_to_deg(g.get('GPSLatitude'), g.get('GPSLatitudeRef'))
                lon = _gps_to_deg(g.get('GPSLongitude'), g.get('GPSLongitudeRef'))
                if lat is not None and lon is not None:
                    out['lat'] = lat; out['lon'] = lon
    except Exception:
        pass
    return out


@app.route('/api/exif')
def api_exif():
    path = request.args.get('path', '')
    p = Path(path)
    if not p.is_file() or p.suffix.lower() not in IMG_EXTS:
        abort(404)
    return jsonify(extract_exif(str(p)))


@app.route('/api/set-auto', methods=['POST'])
def api_set_auto():
    data = request.get_json() or {}
    step = data.get('step')
    if step in state['auto']:
        state['auto'][step] = bool(data.get('enabled'))
    return jsonify({'ok': True})


@app.route('/api/run/<step>', methods=['POST'])
def api_run(step):
    data = request.get_json() or {}
    folder = data.get('folder', '')
    state['folder'] = folder
    if folder and Path(folder).is_dir():
        save_recent(folder)
    if step == 'cull':
        strictness = float(data.get('opt') or 1.0)
        adaptive = bool(data.get('adaptive', True))
        rescue_on = bool(data.get('rescue', True))
        threading.Thread(target=run_cull, args=(folder, strictness, adaptive, rescue_on),
                         daemon=True).start()
    elif step == 'dedup':
        threading.Thread(target=run_dedup, args=(folder, float(data.get('opt') or 0.8)), daemon=True).start()
    elif step == 'rank':
        state['topn'] = int(data.get('topn', 50))
        threading.Thread(target=run_rank, args=(folder,), daemon=True).start()
    else:
        return jsonify({'error': 'bad step'}), 404
    return jsonify({'ok': True})


@app.route('/api/stop/<step>', methods=['POST'])
def api_stop(step):
    """Signal a running step to cancel at its next iteration (e.g. wrong folder)."""
    if step in ('cull', 'dedup', 'rank'):
        state[step]['cancel'] = True
        return jsonify({'ok': True})
    abort(404)


@app.route('/api/progress/<step>')
def api_progress(step):
    if step == 'cull':
        s = state['cull']
        return jsonify({'running': s['running'], 'progress': s['progress'], 'status': s['status'],
                        'photos': s['photos'],
                        'stats': {'images': len(s['photos']), 'sharp': s['sharp'],
                                  'soft': s['soft'], 'blurry': s['blurry']}})
    if step == 'dedup':
        s = state['dedup']
        return jsonify({'running': s['running'], 'progress': s['progress'], 'status': s['status'],
                        'photos': s['photos'], 'stats': {'groups': s['groups']}})
    if step == 'rank':
        s = state['rank']
        return jsonify({'running': s['running'], 'progress': s['progress'], 'status': s['status'],
                        'photos': build_topn(), 'stats': {'images': s['total']}})
    abort(404)


@app.route('/api/weights', methods=['POST'])
def api_weights():
    data = request.get_json() or {}
    w = data.get('weights') or {}
    state['weights'] = {k: float(w.get(k, DEFAULT_WEIGHTS[k])) for k in CATEGORIES}
    state['topn'] = int(data.get('topn', state['topn']))
    return jsonify({'ok': True, 'photos': build_topn()})


@app.route('/api/exclude', methods=['POST'])
def api_exclude():
    data = request.get_json() or {}
    if data.get('path'):
        state['excluded'].add(data['path'])
    return jsonify({'ok': True, 'removed': len(state['excluded']), 'photos': build_topn()})


@app.route('/api/restore', methods=['POST'])
def api_restore():
    data = request.get_json() or {}
    if data.get('all'):
        state['excluded'].clear()
    elif data.get('path'):
        state['excluded'].discard(data['path'])
    return jsonify({'ok': True, 'removed': len(state['excluded']), 'photos': build_topn()})


@app.route('/api/toggle-status', methods=['POST'])
def api_toggle_status():
    """Manually set a photo's tier (client cycles Sharp→Soft→Blurry). Moves the
    file to/from Blurred/ to match (Soft stays in the folder)."""
    data = request.get_json() or {}
    path = data.get('path', '')
    tier = data.get('tier', 'sharp')
    if tier not in ('sharp', 'soft', 'blurry'):
        tier = 'sharp'
    s = state['cull']
    photo = next((p for p in s['photos'] if p.get('path') == path), None)
    if not photo:
        return jsonify({'error': 'photo not found'}), 404
    now_kept = tier != 'blurry'
    new_path = _relocate_for_status(path, now_kept)
    badge, bt = _badge_for(tier, False)
    photo.update({'path': new_path, 'thumb': thumb_url(new_path), 'tier': tier,
                  'kept': now_kept, 'rejected': not now_kept,
                  'badge': badge, 'badgeType': bt})
    sp = s['sharp_paths']
    for old in (path, new_path):
        if old in sp:
            sp.remove(old)
    if now_kept:
        sp.append(new_path)
    s['sharp'] = sum(1 for p in s['photos'] if p['tier'] == 'sharp')
    s['soft'] = sum(1 for p in s['photos'] if p['tier'] == 'soft')
    s['blurry'] = sum(1 for p in s['photos'] if p['tier'] == 'blurry')
    return jsonify({'ok': True, 'tier': tier, 'kept': now_kept, 'badge': badge,
                    'badgeType': bt, 'path': new_path, 'thumb': photo['thumb'],
                    'sharp': s['sharp'], 'soft': s['soft'], 'blurry': s['blurry']})


@app.route('/api/move-blurry', methods=['POST'])
def api_move_blurry():
    """Move the current Blurry-tier photos into a Blurred/ subfolder. Done on
    demand (after review) rather than automatically during Cull."""
    folder = state.get('folder')
    if not folder or not Path(folder).is_dir():
        return jsonify({'error': 'No valid folder'}), 400
    blurry = [pp['path'] for pp in state['cull'].get('photos', [])
              if pp.get('tier') == 'blurry']
    if not blurry:
        return jsonify({'ok': True, 'moved': 0, 'dest': str(Path(folder) / 'Blurred')})
    try:
        org = PhotoOrganizer(folder)
        res = org.move_blurry_photos(blurry)
    except Exception as e:
        logger.error(f"move-blurry failed: {e}")
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'moved': res.get('moved', 0),
                    'dest': str(Path(folder) / 'Blurred')})


@app.route('/api/export', methods=['POST'])
def api_export():
    data = request.get_json() or {}
    topn = int(data.get('topn', state['topn']))
    folder = state.get('folder')
    if not folder or not Path(folder).is_dir():
        return jsonify({'error': 'No valid folder'}), 400
    top = build_topn(topn=topn)
    dest = Path(folder) / f"TOP_{topn}"
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for item in top:
        try:
            src = Path(item['path'])
            if src.is_file():
                shutil.copy2(str(src), str(dest / f"{item['rank']:03d}_{src.name}"))
                copied += 1
        except Exception as e:
            logger.warning(f"export fail {item['path']}: {e}")
    return jsonify({'ok': True, 'copied': copied, 'dest': str(dest)})


# --------------------------------------------------------------------------- #
#  Phone Background selector
#  Flag TOP photos as "suitable as phone wallpaper", then export both the
#  original and a universal phone-cropped (1290 x 2796, 19.5:9) version.
#  19.5:9 covers iPhones pixel-perfect and nearly all Android (phones zoom to
#  fill, so 20:9 screens crop only a hair). One ratio, no device picker.
# --------------------------------------------------------------------------- #
WALLPAPER_W, WALLPAPER_H = 1290, 2796  # universal 19.5:9 portrait


@app.route('/api/toggle-phonebg', methods=['POST'])
def api_toggle_phonebg():
    """Flag / unflag a photo as suitable for a phone wallpaper."""
    data = request.get_json() or {}
    path = data.get('path', '')
    if not path:
        return jsonify({'error': 'no path'}), 400
    if path in state['phone_bg']:
        state['phone_bg'].discard(path)
        on = False
    else:
        state['phone_bg'].add(path)
        on = True
    return jsonify({'ok': True, 'phonebg': on, 'count': len(state['phone_bg'])})


def crop_to_phone(img, target_w=WALLPAPER_W, target_h=WALLPAPER_H):
    """Center-crop a PIL image to the phone's aspect ratio, then resize to the
    native resolution. Honors EXIF orientation first."""
    img = ImageOps.exif_transpose(img).convert('RGB')
    w, h = img.size
    target_ar = target_w / target_h            # ~0.4615 (portrait)
    src_ar = w / h
    if src_ar > target_ar:
        # Source is too wide -> crop the sides (center).
        new_w = int(round(h * target_ar))
        left = (w - new_w) // 2
        box = (left, 0, left + new_w, h)
    else:
        # Source is too tall -> crop top/bottom (center).
        new_h = int(round(w / target_ar))
        top = (h - new_h) // 2
        box = (0, top, w, top + new_h)
    img = img.crop(box)
    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
    return img


@app.route('/api/export-phonebg', methods=['POST'])
def api_export_phonebg():
    """Export flagged wallpapers: an Original/ full-res copy and a Wallpaper/
    1290x2796 (universal 19.5:9) center-cropped version, into a PhoneBG/ folder."""
    folder = state.get('folder')
    if not folder or not Path(folder).is_dir():
        return jsonify({'error': 'No valid folder'}), 400
    # Only export flagged photos that are still in the current TOP N.
    top = build_topn()
    flagged = [item for item in top if item['path'] in state['phone_bg']]
    if not flagged:
        return jsonify({'ok': True, 'copied': 0, 'cropped': 0,
                        'dest': str(Path(folder) / 'PhoneBG'),
                        'note': 'No photos flagged as Phone BG yet.'})
    dest = Path(folder) / 'PhoneBG'
    orig_dir = dest / 'Original'
    crop_dir = dest / 'Wallpaper_19.5x9'
    orig_dir.mkdir(parents=True, exist_ok=True)
    crop_dir.mkdir(parents=True, exist_ok=True)
    copied = cropped = 0
    for item in flagged:
        src = Path(item['path'])
        if not src.is_file():
            continue
        stem = f"{item['rank']:03d}_{src.stem}"
        try:
            shutil.copy2(str(src), str(orig_dir / f"{stem}{src.suffix}"))
            copied += 1
        except Exception as e:
            logger.warning(f"phonebg original fail {src}: {e}")
        try:
            with Image.open(src) as im:
                crop_to_phone(im).save(str(crop_dir / f"{stem}.jpg"),
                                       format='JPEG', quality=92)
            cropped += 1
        except Exception as e:
            logger.warning(f"phonebg crop fail {src}: {e}")
    return jsonify({'ok': True, 'copied': copied, 'cropped': cropped,
                    'dest': str(dest)})


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=True)
