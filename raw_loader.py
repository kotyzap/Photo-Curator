"""raw_loader.py — RAW + HEIC photo support for Photo Curator (v7.0).

Decodes camera RAW files (Canon CR2/CR3, Nikon NEF, Sony ARW, DNG, ...)
via rawpy/LibRaw.

Fast path: instead of demosaicing the sensor data, the full-size JPEG
preview that every camera embeds inside the RAW is extracted — roughly
50x faster on big files, and it carries the original EXIF (date, lens,
GPS, orientation). When no usable embedded preview exists, falls back
to a half-size rawpy demosaic.

HEIC/HEIF (iPhone, and Android phones that shoot HEIF) is handled by
pillow-heif, which registers itself as a Pillow plugin — so once imported,
Image.open() reads .heic like any other format, EXIF included. OpenCV
cannot decode HEIC, so the cv2 paths below route HEIC through Pillow.

If rawpy / pillow-heif aren't installed the rest of the app keeps working
for JPEGs; those files simply fail to load (the callers already tolerate
None / exceptions per file).
"""
import io
import logging
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

try:
    import rawpy
    HAS_RAWPY = True
except ImportError:          # pragma: no cover
    rawpy = None
    HAS_RAWPY = False

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HAS_HEIF = True
except ImportError:          # pragma: no cover
    pillow_heif = None
    HAS_HEIF = False

# Extensions handled by LibRaw. Kept lowercase; callers compare with .lower().
RAW_EXTS = {'.cr2', '.cr3', '.crw',          # Canon
            '.nef', '.nrw',                  # Nikon
            '.arw', '.srf', '.sr2',         # Sony
            '.dng',                          # Adobe / phones
            '.orf',                          # Olympus
            '.rw2',                          # Panasonic
            '.raf',                          # Fujifilm
            '.pef',                          # Pentax
            '.srw',                          # Samsung
            '.rwl', '.3fr', '.erf', '.mos'}  # Leica, Hasselblad, Epson, Leaf

# HEIF container formats — decoded via pillow-heif/libheif if installed.
# .heic/.heif = iPhone & Android stills, .hif = Canon/Sony HEIF.
HEIF_EXTS = {'.heic', '.heif', '.hif'}

# Previews smaller than this are tiny index thumbs — not worth using for
# sharpness/quality analysis; demosaic instead.
_MIN_PREVIEW_BYTES = 50_000


def is_raw(path) -> bool:
    return Path(str(path)).suffix.lower() in RAW_EXTS


def is_heif(path) -> bool:
    return Path(str(path)).suffix.lower() in HEIF_EXTS


def needs_jpeg_preview(path) -> bool:
    """True for formats no browser can render inline (RAW, and HEIC outside
    Safari) — the app serves a transcoded JPEG for these."""
    return is_raw(path) or is_heif(path)


def _embedded_jpeg(path):
    """JPEG bytes of the camera-embedded preview, or None."""
    if not HAS_RAWPY:
        return None
    try:
        with rawpy.imread(str(path)) as raw:
            thumb = raw.extract_thumb()
        if thumb.format == rawpy.ThumbFormat.JPEG and len(thumb.data) >= _MIN_PREVIEW_BYTES:
            return thumb.data
    except Exception as e:
        logger.debug(f"raw preview extract failed {path}: {e}")
    return None


def _demosaic_rgb(path, half=True):
    """Full RAW develop via LibRaw → RGB uint8 ndarray, or None."""
    if not HAS_RAWPY:
        return None
    try:
        with rawpy.imread(str(path)) as raw:
            return raw.postprocess(half_size=half, use_camera_wb=True,
                                   output_bps=8)
    except Exception as e:
        logger.warning(f"raw decode failed {path}: {e}")
        return None


def open_image_pil(path):
    """RAW-aware replacement for PIL Image.open().

    For RAW files returns a PIL Image built from the embedded JPEG preview
    (EXIF preserved, so getexif()/exif_transpose keep working) or, failing
    that, a demosaiced render. Raises OSError on failure, matching
    Image.open() semantics so existing except blocks behave the same.
    """
    if is_heif(path) and not HAS_HEIF:
        raise OSError(f"pillow-heif not installed — cannot open HEIC file {path}")
    if not is_raw(path):
        return Image.open(path)   # pillow-heif has registered .heic by now
    if not HAS_RAWPY:
        raise OSError(f"rawpy not installed — cannot open RAW file {path}")
    data = _embedded_jpeg(path)
    if data is not None:
        try:
            return Image.open(io.BytesIO(data))
        except Exception:
            pass
    rgb = _demosaic_rgb(path)
    if rgb is None:
        raise OSError(f"could not decode RAW file {path}")
    return Image.fromarray(rgb)


def imread_bgr(path, reduced=True):
    """RAW-aware replacement for cv2.imread(..., COLOR). Returns BGR or None.

    reduced=True decodes at ~1/2 resolution (matches IMREAD_REDUCED_COLOR_2)
    for speed; analysis code downscales further anyway.
    """
    if is_raw(path):
        data = _embedded_jpeg(path)
        if data is not None:
            flag = cv2.IMREAD_REDUCED_COLOR_2 if reduced else cv2.IMREAD_COLOR
            bgr = cv2.imdecode(np.frombuffer(data, np.uint8), flag)
            if bgr is not None:
                return bgr
        rgb = _demosaic_rgb(path, half=reduced)
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR) if rgb is not None else None
    if is_heif(path):
        # cv2 has no HEIF decoder; go through Pillow and hand back BGR.
        try:
            img = Image.open(path)
            if reduced:
                img.thumbnail((img.width // 2, img.height // 2),
                              Image.Resampling.BILINEAR)
            return cv2.cvtColor(np.asarray(img.convert('RGB')),
                                cv2.COLOR_RGB2BGR)
        except Exception as e:
            logger.warning(f"heif decode failed {path}: {e}")
            return None
    if reduced:
        bgr = cv2.imread(str(path), cv2.IMREAD_REDUCED_COLOR_2)
        if bgr is not None:
            return bgr
    return cv2.imread(str(path), cv2.IMREAD_COLOR)


def imread_gray(path, reduced=True):
    """RAW-aware replacement for cv2.imread(..., GRAYSCALE)."""
    if is_raw(path) or is_heif(path):
        bgr = imread_bgr(path, reduced=reduced)
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr is not None else None
    if reduced:
        gray = cv2.imread(str(path), cv2.IMREAD_REDUCED_GRAYSCALE_2)
        if gray is not None:
            return gray
    return cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
