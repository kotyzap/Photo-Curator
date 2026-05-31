#!/usr/bin/env python3
"""
Global Perceptual-Hash Deduplicator
=========================================================================
Replaces the old batch-local approach. Key properties:

  • GLOBAL clustering — every photo is compared against the running set of
    cluster representatives, not just the other photos in its 20-photo
    batch. Burst shots that are far apart in the list (or that straddle a
    batch boundary) are now caught.
  • Robust signature — combines average-hash + horizontal/vertical
    difference-hash (192 bits total). Dependency-free (PIL + numpy only);
    optionally augmented by ORB confirmation on borderline pairs.
  • EXIF burst awareness — photos taken within `burst_seconds` of each
    other use a relaxed similarity bar, so a fast burst with subject
    motion still collapses to one frame.
  • Keeps the BEST frame per group (highest overall_score, then sharpest),
    not just the first one encountered.

Backwards compatible: `deduplicate_batch()` still exists and now simply
delegates to the global `deduplicate_all()`.
"""

import os
import json
import numpy as np
from pathlib import Path
from typing import List, Optional, Callable
from dataclasses import dataclass, field
from PIL import Image
import logging

try:
    import cv2  # optional, only for borderline ORB confirmation
    _HAVE_CV2 = True
except Exception:
    _HAVE_CV2 = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Lookup table: number of set bits in each byte value 0..255. Used to compute
# Hamming distance between packed signatures with a single vectorized indexing
# + sum, instead of a Python loop over clusters.
_POPCOUNT = np.array([bin(i).count('1') for i in range(256)], dtype=np.uint16)


@dataclass
class SimilarityGroup:
    """UI-compatible group: one group per cluster, best frame first."""
    group_id: int
    photos: list          # list of {'filename', 'path', 'score'}, best first
    best_photo: dict
    similarity_score: float


@dataclass
class _Cluster:
    rep: object                       # PhotoScore kept as representative (best)
    members: list = field(default_factory=list)
    sig: object = None                # signature of the representative
    ts: Optional[float] = None        # EXIF capture time of representative


class FastBatchDeduplicator:
    """Global perceptual-hash deduplicator (name kept for compatibility)."""

    def __init__(self, threshold=0.80, group_size=10, burst_seconds=3.0,
                 use_orb_confirm=True, orb_confirm=0.30):
        # threshold is on a 0..1 similarity scale (1.0 == identical)
        self.threshold = float(threshold)
        self.group_size = group_size          # unused; kept for API compat
        self.burst_seconds = burst_seconds
        # ORB inlier ratio required to CONFIRM a medium-similarity merge.
        # Protects distinct scenes that merely share a low-res hash.
        self.orb_confirm = orb_confirm
        self.use_orb_confirm = use_orb_confirm and _HAVE_CV2
        self._orb = cv2.ORB_create(nfeatures=300) if self.use_orb_confirm else None
        self._sig_cache = {}
        # In-memory ORB descriptor cache (keyed by path), so a representative's
        # features are computed at most once even if confirmed repeatedly.
        self._orb_desc_cache = {}
        # EXIF capture-time cache: avoids re-opening every image per add_photo.
        self._ts_cache = {}
        # Live/incremental clustering state (see add_photo / current_survivors).
        self.clusters: List[_Cluster] = []
        # Vectorized matching state, parallel to self.clusters[:self._count]:
        #   _rep_bits  (cap, pb_len) uint8  — packed 192-bit rep signatures
        #   _rep_ts    (cap,)        float64 — capture time (nan if unknown)
        #   _rep_valid (cap,)        bool    — False where the rep had no sig
        self._rep_bits = None
        self._rep_ts = None
        self._rep_valid = None
        self._count = 0
        self._pb_len = 24   # 192-bit signature → 24 packed bytes (see reset())
        # Persistent on-disk signature cache, keyed by path+mtime+size, so
        # re-running dedup on the same album is near-instant. Disable by
        # passing disk_cache=None.
        self._disk_cache_path = None
        self._disk_cache = {}

    def enable_disk_cache(self, cache_path):
        """Load (and later persist) signatures from a JSON file on disk."""
        self._disk_cache_path = Path(cache_path)
        try:
            if self._disk_cache_path.exists():
                with open(self._disk_cache_path) as f:
                    self._disk_cache = json.load(f)
                logger.info(f"Loaded {len(self._disk_cache)} cached signatures")
        except Exception as e:
            logger.warning(f"Could not load signature cache: {e}")
            self._disk_cache = {}

    def save_disk_cache(self):
        if self._disk_cache_path is None:
            return
        try:
            with open(self._disk_cache_path, 'w') as f:
                json.dump(self._disk_cache, f)
        except Exception as e:
            logger.warning(f"Could not save signature cache: {e}")

    @staticmethod
    def _cache_key(image_path: str):
        try:
            st = os.stat(image_path)
            return f"{image_path}|{int(st.st_mtime)}|{st.st_size}"
        except Exception:
            return None

    # ----------------------------------------------------------------- signatures
    def _signature(self, image_path: str):
        """192-bit perceptual signature: ahash(64) + dhash_h(64) + dhash_v(64)."""
        if image_path in self._sig_cache:
            return self._sig_cache[image_path]
        # On-disk cache hit (survives across runs).
        ckey = self._cache_key(image_path)
        if ckey is not None and ckey in self._disk_cache:
            try:
                sig = np.array(self._disk_cache[ckey], dtype=bool)
                self._sig_cache[image_path] = sig
                return sig
            except Exception:
                pass
        try:
            img = Image.open(image_path).convert('L')
            # average hash: 8x8
            a = np.asarray(img.resize((8, 8), Image.BILINEAR), dtype=np.float32)
            ahash = (a > a.mean()).flatten()
            # difference hash horizontal: 9x8 -> compare adjacent cols
            dh = np.asarray(img.resize((9, 8), Image.BILINEAR), dtype=np.float32)
            dhash_h = (dh[:, 1:] > dh[:, :-1]).flatten()
            # difference hash vertical: 8x9 -> compare adjacent rows
            dv = np.asarray(img.resize((8, 9), Image.BILINEAR), dtype=np.float32)
            dhash_v = (dv[1:, :] > dv[:-1, :]).flatten()
            sig = np.concatenate([ahash, dhash_h, dhash_v]).astype(bool)
        except Exception as e:
            logger.warning(f"Signature failed for {image_path}: {e}")
            sig = None
        self._sig_cache[image_path] = sig
        if ckey is not None and sig is not None:
            self._disk_cache[ckey] = sig.astype(int).tolist()
        return sig

    @staticmethod
    def _sig_similarity(s1, s2) -> float:
        if s1 is None or s2 is None:
            return 0.0
        # 1 - normalized hamming distance
        return 1.0 - float(np.count_nonzero(s1 != s2)) / s1.size

    def _capture_time(self, image_path: str) -> Optional[float]:
        """REAL EXIF capture time (DateTimeOriginal) as epoch seconds, or None.

        Deliberately does NOT fall back to file mtime: mtime reflects when the
        file was copied/written, so copied folders share near-identical mtimes
        and would be wrongly treated as one giant burst. Burst detection must
        rely only on genuine capture time."""
        if image_path in self._ts_cache:
            return self._ts_cache[image_path]
        result = None
        try:
            img = Image.open(image_path)
            exif = img.getexif()
            # 36867 = DateTimeOriginal, 306 = DateTime
            for tag in (36867, 306):
                val = exif.get(tag)
                if val:
                    import time as _t
                    result = _t.mktime(_t.strptime(str(val), "%Y:%m:%d %H:%M:%S"))
                    break
        except Exception:
            result = None
        self._ts_cache[image_path] = result
        return result

    def _sort_key_time(self, image_path: str) -> float:
        """Ordering only (capture time, else mtime). Never used for bursts."""
        t = self._capture_time(image_path)
        if t is not None:
            return t
        try:
            return os.path.getmtime(image_path)
        except Exception:
            return 0.0

    def _orb_descriptors(self, path: str):
        """ORB keypoint-count + descriptors for an image, cached by path so a
        representative's features are computed at most once."""
        cached = self._orb_desc_cache.get(path)
        if cached is not None:
            return cached
        result = (0, None)
        try:
            img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if img is not None:
                h = img.shape[0]
                if h > 480:
                    img = cv2.resize(img, None, fx=480 / h, fy=480 / h)
                k, d = self._orb.detectAndCompute(img, None)
                result = (len(k) if k is not None else 0, d)
        except Exception:
            result = (0, None)
        self._orb_desc_cache[path] = result
        return result

    def _orb_similarity(self, p1: str, p2: str) -> float:
        """Borderline confirmation only. Returns inlier ratio 0..1.
        Descriptors are cached per path (see _orb_descriptors)."""
        if not self.use_orb_confirm:
            return 0.0
        try:
            n1, d1 = self._orb_descriptors(p1)
            n2, d2 = self._orb_descriptors(p2)
            if d1 is None or d2 is None or len(d1) == 0 or len(d2) == 0:
                return 0.0
            bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
            matches = bf.match(d1, d2)
            if not matches:
                return 0.0
            good = sum(1 for m in matches if m.distance < 40)
            return good / max(min(n1, n2), 1)
        except Exception:
            return 0.0

    # ----------------------------------------------------------------- scoring
    @staticmethod
    def _quality(score) -> float:
        """Higher == better frame to keep. Ranking-driven: the photo's overall
        rank score decides the survivor, with sharpness only as a tiebreaker."""
        overall = getattr(score, 'overall_score', 0.0) or 0.0
        focus = getattr(score, 'focus', 0.0) or 0.0
        return overall * 1.0 + focus * 0.05

    # ----------------------------------------------------------------- core
    def reset(self):
        """Clear incremental clustering state before a new run."""
        self.clusters = []
        self._rep_bits = None
        self._rep_ts = None
        self._rep_valid = None
        self._count = 0
        # Signature is a fixed 192-bit vector → 24 packed bytes. Setting this
        # up front (rather than lazily from the first image) keeps the matching
        # arrays correctly sized even when the very first photo has no signature
        # (e.g. a corrupt/non-image file), which previously caused a zero-width
        # array and a broadcast crash on the next valid photo.
        self._pb_len = 24

    def _ensure_capacity(self):
        """Grow the parallel rep arrays (amortized doubling) if full."""
        cap = 0 if self._rep_bits is None else self._rep_bits.shape[0]
        if self._count < cap:
            return
        new_cap = max(64, cap * 2)
        nb = np.zeros((new_cap, self._pb_len), dtype=np.uint8)
        nt = np.full(new_cap, np.nan, dtype=np.float64)
        nv = np.zeros(new_cap, dtype=bool)
        if cap:
            nb[:cap] = self._rep_bits
            nt[:cap] = self._rep_ts
            nv[:cap] = self._rep_valid
        self._rep_bits, self._rep_ts, self._rep_valid = nb, nt, nv

    def _new_cluster(self, score, sig, ts, packed):
        """Append a fresh cluster and its parallel matching row."""
        i = self._count
        self._ensure_capacity()
        if packed is not None:
            self._rep_bits[i] = packed
            self._rep_valid[i] = True
        else:
            self._rep_valid[i] = False
        self._rep_ts[i] = ts if ts is not None else np.nan
        self.clusters.append(_Cluster(rep=score, members=[score],
                                      sig=sig, ts=ts))
        self._count += 1

    def add_photo(self, score) -> bool:
        """Assign ONE photo to the global cluster set (incremental, live).

        Compares against every existing cluster representative — not a local
        window — so duplicates are caught no matter how far apart they arrive.
        The comparison is vectorized (one NumPy XOR + popcount against ALL
        cluster reps at once), so cost grows ~linearly with cluster count
        instead of the old Python per-cluster loop.

        Returns True if this photo started a new cluster (i.e. it's currently
        a unique survivor), False if it was folded into an existing one.
        """
        sig = self._signature(score.path)
        ts = self._capture_time(score.path)   # None unless real EXIF present
        burst_thresh = max(0.62, self.threshold - 0.12)
        # At/above this hash similarity the frames are effectively identical;
        # merge with no further checks. Below it, a merge must be justified.
        hard_merge = max(0.92, self.threshold)

        packed = None
        if sig is not None:
            packed = np.packbits(sig)
            # Defensive: if the packed width ever disagrees with the matching
            # matrix, don't risk a broadcast error — treat as a new cluster.
            if packed.shape[0] != self._pb_len:
                self._new_cluster(score, sig, ts, None)
                return True

        # No clusters yet, or this photo has no signature → it can't match.
        if self._count == 0 or packed is None:
            self._new_cluster(score, sig, ts, packed)
            return True

        n = self._count
        bits = self._rep_bits[:n]
        # Vectorized Hamming distance: XOR packed bytes, popcount, sum per row.
        dist = _POPCOUNT[bits ^ packed].sum(axis=1)
        sims = 1.0 - dist / float(sig.size)

        # Per-cluster bar: relaxed for EXIF-timed bursts, strict otherwise.
        if ts is not None:
            time_close = (~np.isnan(self._rep_ts[:n])) & \
                         (np.abs(self._rep_ts[:n] - ts) <= self.burst_seconds)
        else:
            time_close = np.zeros(n, dtype=bool)
        bar = np.where(time_close, burst_thresh, self.threshold)
        cand = (sims >= bar) & self._rep_valid[:n]

        if not cand.any():
            self._new_cluster(score, sig, ts, packed)
            return True

        # Best candidate by hash similarity.
        cand_idx = np.nonzero(cand)[0]
        best = int(cand_idx[np.argmax(sims[cand_idx])])
        best_sim = float(sims[best])

        if best_sim >= hard_merge or bool(time_close[best]):
            merge = True                       # near-identical, or a timed burst
        elif self.use_orb_confirm:
            # Medium hash similarity from a non-burst pair is exactly the case
            # that wrongly dropped beautiful, distinct scenes. Confirm ONLY the
            # single best candidate with ORB (instead of every candidate).
            merge = (self._orb_similarity(score.path, self.clusters[best].rep.path)
                     >= self.orb_confirm)
        else:
            merge = True                       # no cv2 available -> old behavior

        if not merge:
            self._new_cluster(score, sig, ts, packed)
            return True

        c = self.clusters[best]
        c.members.append(score)
        if self._quality(score) > self._quality(c.rep):
            c.rep = score                      # promote the sharper/better frame
            c.sig = sig
            c.ts = ts
            self._rep_bits[best] = packed
            self._rep_ts[best] = ts if ts is not None else np.nan
        return False

    def current_survivors(self) -> List:
        """One best frame per cluster, in the order clusters were created."""
        return [c.rep for c in self.clusters]

    def deduplicate_all(self, photo_scores: List,
                        progress_callback: Optional[Callable] = None) -> List:
        """Cluster the ENTIRE set globally; return one best frame per cluster."""
        if not photo_scores:
            return []

        # Process in capture order so bursts are adjacent (helps the EXIF rule).
        items = list(photo_scores)
        try:
            items.sort(key=lambda s: (self._sort_key_time(s.path), s.path))
        except Exception:
            pass

        self.reset()
        total = len(items)
        for i, score in enumerate(items):
            if progress_callback and (i % 5 == 0 or i == total - 1):
                progress_callback(int(i / total * 100),
                                  f"Comparing {i + 1}/{total} · "
                                  f"{len(self.clusters)} unique so far")
            self.add_photo(score)

        survivors = self.current_survivors()
        removed = total - len(survivors)
        logger.info(f"Global dedup: {total} → {len(survivors)} unique "
                    f"({removed} duplicates removed across {len(self.clusters)} clusters)")
        self.last_clusters = self.clusters
        return survivors

    def deduplicate_batch(self, photo_scores: List) -> List:
        """Backwards-compatible shim — now delegates to the global pass."""
        return self.deduplicate_all(photo_scores)

    def cluster_similar_photos(self, photo_scores: List,
                               progress_callback: Optional[Callable] = None
                               ) -> List[SimilarityGroup]:
        """UI-compatible adapter. Runs the global dedup, then returns one
        SimilarityGroup per cluster (best frame first), matching the shape the
        old SimilarityDetector produced so photo_dedup_ui.py needs no rework."""
        self.deduplicate_all(photo_scores, progress_callback)

        groups: List[SimilarityGroup] = []
        for cluster in getattr(self, 'last_clusters', []):
            members = sorted(
                cluster.members,
                key=lambda s: self._quality(s),
                reverse=True,
            )
            photos = [{
                'filename': getattr(m, 'filename', Path(m.path).name),
                'path': m.path,
                'score': round(getattr(m, 'overall_score', 0.0) or 0.0, 1),
            } for m in members]
            groups.append(SimilarityGroup(
                group_id=len(groups),
                photos=photos,
                best_photo=photos[0],
                similarity_score=1.0,
            ))

        self.save_disk_cache()
        return groups
