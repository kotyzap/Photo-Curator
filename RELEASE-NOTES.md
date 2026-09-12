## 🆕 HEIC / iPhone photos

Photo Curator now reads **HEIC/HEIF** straight from an iPhone import — no
conversion step, no detour through Photos.

- **Formats** — `.heic`, `.heif`, `.hif` (iPhone and Android stills, plus
  Canon/Sony HEIF), decoded by [pillow-heif](https://pypi.org/project/pillow-heif/)
  (libheif). EXIF — date, lens, GPS, orientation — comes through exactly as it
  does for JPEG, so the map view and burst timing work unchanged.
- **HEIC filter chip** in Cull, beside *All types · RAW only · JPG only*.
- **Real format tags** — every card now shows its actual format (HEIC, CR2,
  NEF, PNG, TIFF…) instead of a two-state RAW/JPG label.
- **Browser-safe display** — only Safari renders HEIC natively, so the
  full-size view is served as a cached JPEG transcode. Your `.heic` files are
  never modified, and exports always copy the untouched original.
- **Mixed folders just work** — JPEG + PNG + HEIC + RAW in one pass.

Full format list: JPEG · PNG · HEIC/HEIF/HIF · TIFF · BMP · WebP · every RAW
format LibRaw reads.

## 📦 Downloads

| Build | Notes |
|---|---|
| `PhotoCurator-Mac-AppleSilicon-Offline-v7.0.zip` | Apple Silicon M1–M6 · macOS 11+ · fully offline |
| `PhotoCurator-Windows-x64-Offline-v7.0.zip` | Windows 10/11 x64 · private Python 3.11 · fully offline |
| `photo-curator-7.zip` (in repo) | Run from source · Python 3.9+ |

Both offline bundles ship pillow-heif 1.1.1 and rawpy, so HEIC and RAW work
with nothing else installed. Verify a download against `CHECKSUMS-bundles.txt`.

## 🛠️ Running from source

    pip install -r requirements.txt

HEIC needs `pillow-heif`, RAW needs `rawpy` — both are in `requirements.txt`.
Without either, the app keeps working on the remaining formats and says so in
the sidebar.
