# Photo Curator v7.0 — how to publish

This folder is a clone of `PaoloCortezCZ/Photo-Curator` with the v7.0 commit
already made locally. Nothing has been pushed.

## 1. Review

    cd "/Volumes/External SSD/Claude/Projects/Photo Curator/github-push"
    git show --stat HEAD
    git show HEAD -- raw_loader.py

## 2. Push

    git push origin main

(Uses your own credentials; this folder has none configured.)

## 3. Create the v7.0 release with the two offline bundles

Bundles are NOT in the repo — GitHub warns above 50 MB and they would bloat
every clone forever. Attach them to a release instead:

    cd "/Volumes/External SSD/Claude/Projects/Photo Curator"
    gh release create v7.0 \
      PhotoCurator-Mac-AppleSilicon-Offline-v7.0.zip \
      PhotoCurator-Windows-x64-Offline-v7.0.zip \
      --title "v7.0 — HEIC / iPhone photo support" \
      --notes-file github-push/RELEASE-NOTES.md

Or drag both zips onto https://github.com/PaoloCortezCZ/Photo-Curator/releases/new

## 4. Fill in the two placeholder links  ← REQUIRED

After the release exists, its Windows asset URL will be:

    https://github.com/PaoloCortezCZ/Photo-Curator/releases/download/v7.0/PhotoCurator-Windows-x64-Offline-v7.0.zip

Replace `TODO-WINDOWS-DOWNLOAD-URL` with it in **both** files, then commit again:

  - `docs/index.html`  (Windows download card)
  - `README.md`        (Offline package for Windows section)

Also worth updating: the Mac download button still points at the Proton Drive
URL holding the **v6.0** bundle. Either upload the v7.0 zip there, or point it
at the release asset alongside Windows.

## 5. Verify the published bundles

`CHECKSUMS-bundles.txt` (committed) holds the SHA-256 of both zips. After
downloading from the release page:

    shasum -a 256 -c CHECKSUMS-bundles.txt

## 6. Clean up

This whole `github-push/` folder is disposable once pushed — delete it.
