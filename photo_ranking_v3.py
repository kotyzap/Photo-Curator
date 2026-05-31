#!/usr/bin/env python3
"""
Photo Curator v3 — Advanced Ranking Engine
=============================================================================
A pure-CV (cv2 / numpy / Pillow only — no ML model, fully offline) photo
quality estimator that approximates the criteria a photography magazine's
judging panel actually names in a critique:

  COMPOSITION   rule-of-thirds / golden-ratio placement of the salient
                subject, horizon leveling, and left/right balance.
  TECHNICAL     highlight & shadow clipping, dynamic range, tonal
                distribution (zone-system style), white-balance neutrality,
                and noise.
  SHARPNESS     contrast-normalized focus (same measure used by v3 Cull, so
                scores are consistent across steps).
  COLOR         colorfulness (Hasler–Süsstrunk) and hue harmony.
  AESTHETIC     a transparent weighted blend of the above — an explainable
                stand-in for a learned "taste" score (NIMA can slot in later
                as an optional bonus without changing this API).

Every sub-score is 0–100 and exposed on the result object, so the UI can show
a full breakdown / radar. `overall_score` is a weighted combination.

The analyzer also fills the v2-compatible fields (composition, lighting,
focus, color, contrast) so it can drop into the existing app/radar.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np


# --------------------------------------------------------------------------- #
#  Result container
# --------------------------------------------------------------------------- #
@dataclass
class PhotoScoreV3:
    filename: str
    path: str
    overall_score: float = 0.0

    # top-level categories (0–100)
    composition: float = 0.0
    technical: float = 0.0
    sharpness: float = 0.0
    color: float = 0.0
    aesthetic: float = 0.0
    dynamic_range: float = 0.0

    # composition sub-scores
    rule_of_thirds: float = 0.0
    horizon_level: float = 0.0
    balance: float = 0.0

    # technical sub-scores
    exposure: float = 0.0
    tonal: float = 0.0
    white_balance: float = 0.0
    noise: float = 0.0

    # color sub-scores
    colorfulness: float = 0.0
    harmony: float = 0.0

    # v2-compatible aliases (so the existing radar/app keep working)
    lighting: float = 0.0
    contrast: float = 0.0
    focus: float = 0.0

    timestamp: float = None
    meta: dict = field(default_factory=dict)


def _clip01(x):
    return float(max(0.0, min(1.0, x)))


# --------------------------------------------------------------------------- #
#  Analyzer
# --------------------------------------------------------------------------- #
class AdvancedPhotoAnalyzer:
    """Pure-CV magazine-style photo quality estimator."""

    # weights for the overall score (sum need not be 1; normalized internally)
    WEIGHTS = {
        'aesthetic': 0.30,
        'composition': 0.22,
        'technical': 0.20,
        'sharpness': 0.16,
        'color': 0.12,
    }

    def __init__(self, work_long_edge=1024):
        self.work = work_long_edge

    # ---------------------------------------------------------------- helpers
    def _load(self, image_path):
        """Return (bgr, gray) downscaled to a fixed long edge for consistent,
        resolution-independent metrics. Uses a reduced-resolution decode for
        speed on big Canon JPEGs."""
        bgr = cv2.imread(str(image_path), cv2.IMREAD_REDUCED_COLOR_2)
        if bgr is None:
            bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if bgr is None:
            return None, None
        h, w = bgr.shape[:2]
        long_edge = max(h, w)
        if long_edge > self.work:
            sc = self.work / long_edge
            bgr = cv2.resize(bgr, (int(w * sc), int(h * sc)),
                             interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        return bgr, gray

    # ------------------------------------------------------------- saliency
    @staticmethod
    def _saliency(gray):
        """Spectral-residual saliency map (Hou & Zhang 2007), implemented with
        numpy FFT so no opencv-contrib is required. Returns a float map in
        [0,1] at 64x64."""
        small = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA)
        f = np.fft.fft2(small.astype(np.float32))
        amp = np.abs(f)
        phase = np.angle(f)
        log_amp = np.log(amp + 1e-8)
        # average filter to get the "expected" spectrum
        kernel = np.ones((3, 3), np.float32) / 9.0
        smooth = cv2.filter2D(log_amp, -1, kernel)
        residual = log_amp - smooth
        recon = np.fft.ifft2(np.exp(residual + 1j * phase))
        sal = np.abs(recon) ** 2
        sal = cv2.GaussianBlur(sal, (0, 0), 2.5)
        mn, mx = float(sal.min()), float(sal.max())
        if mx - mn < 1e-8:
            return np.zeros_like(sal)
        return (sal - mn) / (mx - mn)

    # ----------------------------------------------------------- composition
    def _composition(self, gray):
        sal = self._saliency(gray)
        H, W = sal.shape

        # --- subject centroid from the PEAK saliency only ---
        # Using the full weighted centroid lets diffuse structure (e.g. a wide
        # central horizon) drag the centroid to the middle and hide the real
        # subject. Keep only the most-salient pixels so the centroid locks onto
        # the actual point of interest.
        thr = np.percentile(sal, 88)
        peak = np.where(sal >= max(thr, 1e-6), sal, 0.0)
        total = float(peak.sum())
        if total < 1e-6:
            peak, total = sal, float(sal.sum()) + 1e-8
        ys, xs = np.mgrid[0:H, 0:W]
        cx = float((peak * xs).sum() / total) / (W - 1)
        cy = float((peak * ys).sum() / total) / (H - 1)

        # rule-of-thirds + golden-ratio power points (normalized 0..1)
        pts = [1 / 3, 2 / 3, 0.382, 0.618]
        power = [(px, py) for px in pts for py in pts]
        dmin = min(((cx - px) ** 2 + (cy - py) ** 2) ** 0.5 for px, py in power)
        # nearest power point ~0 dist => 1.0; ~0.18 away => ~0. (0.18 ≈ a third
        # of the way to center from a power point)
        rot = _clip01(1.0 - dmin / 0.18)

        # --- horizon leveling ---
        horizon_level = self._horizon_level(gray)

        # --- left/right balance of visual weight ---
        left = float(sal[:, :W // 2].sum())
        right = float(sal[:, W - W // 2:].sum())
        bal = 1.0 - abs(left - right) / (left + right + 1e-8)
        balance = _clip01(bal)

        composition = 100.0 * (0.5 * rot + 0.25 * horizon_level + 0.25 * balance)
        return (composition, 100.0 * rot, 100.0 * horizon_level,
                100.0 * balance)

    @staticmethod
    def _horizon_level(gray):
        """1.0 if the dominant straight line is perfectly horizontal/vertical,
        lower as it tilts. Returns 0.6 (neutral) when no strong line is found —
        not every good photo has a horizon."""
        edges = cv2.Canny(gray, 60, 180)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80,
                                minLineLength=gray.shape[1] // 3, maxLineGap=20)
        if lines is None:
            return 0.6
        tilts = []
        weights = []
        for l in lines[:200]:
            x1, y1, x2, y2 = l[0]
            length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            ang = np.degrees(np.arctan2(y2 - y1, x2 - x1))  # -180..180
            ang = abs(ang) % 180
            # distance to the nearest of horizontal(0/180) or vertical(90)
            d = min(ang, abs(ang - 90), abs(ang - 180))
            tilts.append(d)
            weights.append(length)
        if not tilts:
            return 0.6
        tilts = np.array(tilts)
        weights = np.array(weights)
        # weight toward the longest lines (the real structural edges)
        med_tilt = float(np.average(tilts, weights=weights))
        # 0° tilt => 1.0 ; 6° => ~0  (a 6° tilt is glaringly crooked)
        return _clip01(1.0 - med_tilt / 6.0)

    # ------------------------------------------------------------- technical
    def _technical(self, bgr, gray):
        g = gray.astype(np.float32)
        hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten()
        hist_n = hist / (hist.sum() + 1e-8)

        # clipping: fraction of pixels crushed to black / blown to white
        shadow_clip = float(hist_n[:3].sum())
        highlight_clip = float(hist_n[253:].sum())
        clip_penalty = _clip01((shadow_clip + highlight_clip) / 0.06)  # 6%→max
        exposure = 100.0 * (1.0 - clip_penalty)

        # dynamic range: spread between the 1st and 99th percentile
        p1, p99 = np.percentile(g, [1, 99])
        dynamic_range = _clip01((p99 - p1) / 220.0) * 100.0

        # tonal distribution: histogram entropy (well-spread tones score high)
        nz = hist_n[hist_n > 0]
        entropy = float(-(nz * np.log2(nz)).sum())          # 0..8 bits
        tonal = _clip01(entropy / 7.0) * 100.0

        # white balance: gray-world neutrality (mild — artistic casts allowed)
        b, gch, r = [float(bgr[:, :, i].mean()) for i in range(3)]
        mean_all = (b + gch + r) / 3.0 + 1e-8
        cast = (abs(r - mean_all) + abs(gch - mean_all) + abs(b - mean_all)) / mean_all
        white_balance = _clip01(1.0 - cast / 0.6) * 100.0

        # noise: high-frequency energy in the FLATTEST regions (low local var)
        noise = self._noise_score(g)

        technical = (0.34 * exposure + 0.24 * dynamic_range +
                     0.18 * tonal + 0.12 * white_balance + 0.12 * noise)
        return (technical, exposure, dynamic_range, tonal, white_balance, noise)

    @staticmethod
    def _noise_score(g):
        """Estimate sensor/ISO noise from smooth areas. We look at the local
        std in 16x16 tiles, take the low-percentile tiles (flat sky etc.) and
        read their residual high-freq energy. Less residual => cleaner."""
        small = cv2.resize(g, (256, 256), interpolation=cv2.INTER_AREA)
        blur = cv2.GaussianBlur(small, (0, 0), 1.2)
        hf = small - blur
        # local variance of the original to find flat tiles
        m = cv2.boxFilter(small, -1, (16, 16))
        loc_var = cv2.boxFilter(small * small, -1, (16, 16)) - m * m
        flat = loc_var < np.percentile(loc_var, 25)
        if flat.sum() < 50:
            resid = float(hf.std())
        else:
            resid = float(hf[flat].std())
        # resid ~0 => clean (100); resid >=8 => very noisy (0)
        return _clip01(1.0 - resid / 8.0) * 100.0

    # ----------------------------------------------------------------- color
    def _color(self, bgr):
        b, gch, r = bgr[:, :, 0].astype(np.float32), \
            bgr[:, :, 1].astype(np.float32), bgr[:, :, 2].astype(np.float32)
        # Hasler–Süsstrunk colorfulness
        rg = r - gch
        yb = 0.5 * (r + gch) - b
        std_rgyb = (rg.std() ** 2 + yb.std() ** 2) ** 0.5
        mean_rgyb = (rg.mean() ** 2 + yb.mean() ** 2) ** 0.5
        cf = std_rgyb + 0.3 * mean_rgyb
        colorfulness = _clip01(cf / 110.0) * 100.0   # ~110 is very colorful

        harmony = self._harmony(bgr)
        color = 0.6 * colorfulness + 0.4 * harmony
        return color, colorfulness, harmony

    @staticmethod
    def _harmony(bgr):
        """Reward images whose dominant hues sit in a recognizable relationship
        (analogous = clustered, or complementary = ~180° apart). Penalize muddy
        scattered hues. Returns 0..100."""
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        h = hsv[:, :, 0].astype(np.float32) * 2.0      # 0..360
        s = hsv[:, :, 1].astype(np.float32) / 255.0
        mask = s > 0.15                                 # ignore desaturated px
        if mask.sum() < 100:
            return 50.0                                 # near-monochrome: neutral
        hh = h[mask]
        hist, _ = np.histogram(hh, bins=36, range=(0, 360))
        hist = hist / (hist.sum() + 1e-8)
        # circular concentration: vector strength of the hue distribution
        ang = np.deg2rad(np.arange(0, 360, 10) + 5)
        cx = float((hist * np.cos(ang)).sum())
        cy = float((hist * np.sin(ang)).sum())
        concentration = (cx * cx + cy * cy) ** 0.5      # 1=single hue, 0=uniform
        # complementary bonus: energy ~180° from the dominant hue
        dom = int(np.argmax(hist))
        comp = hist[(dom + 18) % 36]
        comp_bonus = _clip01(comp / 0.10)
        score = 0.65 * concentration + 0.35 * comp_bonus
        return _clip01(score) * 100.0

    # --------------------------------------------------------------- sharpness
    @staticmethod
    def _sharpness(gray):
        """Contrast-normalized focus measure (matches v3 Cull). Mapped to a
        0–100 perceptual-ish scale."""
        lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        img_var = float(gray.astype(np.float32).var()) + 1e-6
        norm = lap_var / img_var * 1000.0
        # ~120 = borderline sharp, ~600+ = crisp
        return _clip01(norm / 600.0) * 100.0

    # ------------------------------------------------------------------ public
    def analyze_image(self, image_path) -> Optional[PhotoScoreV3]:
        bgr, gray = self._load(image_path)
        if bgr is None:
            return None
        try:
            composition, rot, horizon, balance = self._composition(gray)
            (technical, exposure, dr, tonal, wb, noise) = self._technical(bgr, gray)
            sharpness = self._sharpness(gray)
            color, colorfulness, harmony = self._color(bgr)

            # transparent "aesthetic" blend — an explainable taste proxy
            aesthetic = (0.30 * composition + 0.22 * color +
                         0.20 * sharpness + 0.16 * dr + 0.12 * exposure)

            w = self.WEIGHTS
            wsum = sum(w.values())
            overall = (w['aesthetic'] * aesthetic +
                       w['composition'] * composition +
                       w['technical'] * technical +
                       w['sharpness'] * sharpness +
                       w['color'] * color) / wsum

            return PhotoScoreV3(
                filename=Path(image_path).name, path=str(image_path),
                overall_score=float(overall),
                composition=float(composition), technical=float(technical),
                sharpness=float(sharpness), color=float(color),
                aesthetic=float(aesthetic), dynamic_range=float(dr),
                rule_of_thirds=float(rot), horizon_level=float(horizon),
                balance=float(balance),
                exposure=float(exposure), tonal=float(tonal),
                white_balance=float(wb), noise=float(noise),
                colorfulness=float(colorfulness), harmony=float(harmony),
                # v2-compatible aliases
                lighting=float(exposure), contrast=float(dr),
                focus=float(sharpness),
                timestamp=time.time(),
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"v3 analyze failed {image_path}: {e}")
            return None


# Backwards-friendly alias so callers can `from photo_ranking_v3 import PhotoAnalyzer`
PhotoAnalyzer = AdvancedPhotoAnalyzer
