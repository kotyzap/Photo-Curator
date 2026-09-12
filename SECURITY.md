# Security Policy & Threat Model

Photo Curator is a **100% local** desktop tool. It runs a small Flask server bound
to `127.0.0.1` and opens a browser tab against it. No data leaves your machine:
no uploads, no telemetry, no external APIs (the only outbound requests are
optional OpenFreeMap vector map tiles in the GPS view and a Ko-fi donate badge,
both loaded by your browser, not the app; the map library itself is bundled, not
loaded from a CDN).

This document describes the threat model and the mitigations in place.

## Threat model

The app reads, copies, moves, and serves image files from a folder you select.
The realistic threats for a localhost tool are therefore:

1. **A malicious web page reaching the local server.** Any site you visit can
   make your browser send requests to `http://127.0.0.1:5014`. Via a
   DNS-rebinding attack a remote site can even make those requests appear
   same-origin. If the server served arbitrary files, such a page could read
   your photos — or any file — off disk.
2. **Path traversal / arbitrary file read.** A crafted `?path=` parameter
   (`../../etc/passwd`, an absolute path, or a symlink) could escape the
   selected folder.
3. **Network exposure.** If the server bound to `0.0.0.0` it would be reachable
   by other devices on your LAN.
4. **Supply-chain / dependency vulnerabilities.** A compromised or outdated
   third-party package.
5. **Accidental data loss.** A bug or mistaken click destroying originals.

Out of scope: an attacker who already has local code-execution or filesystem
access on your machine (they don't need this app), and multi-user/hostile-LAN
server hardening (the app is single-user and loopback-only).

## Mitigations

| Threat | Mitigation |
|--------|------------|
| Malicious page / DNS rebinding (1) | Every request is rejected unless its `Host` header is a loopback address (`127.0.0.1`/`localhost`), and any cross-site `Origin` is rejected (HTTP 403). A rebound attacker domain fails the `Host` check. |
| Path traversal / arbitrary read (2) | The image, thumbnail, and EXIF endpoints resolve the requested path with `os.path.realpath()` (collapsing `..` and following symlinks) and serve it only if it is a real image file **inside** the currently selected or a recently used folder. Everything else returns 404. |
| Network exposure (3) | The server binds explicitly to `127.0.0.1`, never `0.0.0.0`. It is unreachable from the network. |
| Dependency vulnerabilities (4) | Dependencies are pinned to exact versions in `requirements.txt`. Scan them with `pip-audit -r requirements.txt`. |
| Accidental data loss (5) | Actions are reversible and explicit: nothing is deleted. Blurry photos move to a `Blurred/` subfolder, exports **copy** originals into `TOP_N/` and `PhoneBG/`. Originals are never modified in place. |
| Response hardening | Security headers on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a Content-Security-Policy. |

## Verifying your download

Each release ships a SHA-256 checksum (see `CHECKSUMS.txt`). Verify before
running:

```bash
# macOS
shasum -a 256 -c CHECKSUMS.txt
# Linux
sha256sum -c CHECKSUMS.txt
```

If the checksum does not match, do not run the file — re-download from the
official source.

## Reporting a vulnerability

This is an MIT-licensed hobby project. Please open an issue (or, for anything
sensitive, contact the maintainer privately) describing the problem and how to
reproduce it. Since the app is local-only and stores no credentials, the
blast radius of most issues is limited to the machine running it.
