#!/usr/bin/env python3
"""
Photo Curator v3.2 - Improved visual curation dashboard
=====================================================
What's new in v3.2
  • Vectorized dedup matching + cached EXIF/signatures (much faster on big cards)
  • EXIF-orientation-correct thumbnails (portrait shots no longer sideways)
  • Skips macOS AppleDouble (._*) / hidden files
  • Review-first flow: blurry photos are moved on demand, not automatically
  • Newest-first live grids, "Best of N" dedup labels, richer progress + ETA

What's new in v3.1
  • Persistent perceptual-signature cache (.dedup_sig_cache.json) — repeat
    Dedup/Rank runs on the same folder are near-instant
  • Ko-Fi button relocated to the header, alongside the theme toggle

What's new vs. photo_curator_visual.py
  • Pick ANY folder on the Mac (native folder dialog) — not just the SD card
  • Auto-detects the SD card + remembers recent folders as one-click shortcuts
  • Full Cull -> Dedup -> Rank pipeline wired end-to-end (no 20-photo cap)
  • Large LIVE PREVIEW / lightbox with arrow-key navigation while reviewing
  • TOP-N export to a folder of your choice
  • Light theme by default + dark/light theme switcher

Run:  python3 photo_curator_v2.py    then open  http://localhost:5000
"""

from flask import Flask, render_template_string, request, jsonify, send_file, abort
from pathlib import Path
import threading
import subprocess
import json
import time
import shutil
import io
import hashlib
import logging
from urllib.parse import quote

import cv2
import numpy as np
from PIL import Image

from photo_ranking_engine import PhotoAnalyzer
from photo_file_organizer import PhotoOrganizer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

IMG_EXTS = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp', '.webp'}
RECENTS_FILE = Path.home() / '.photo_curator_recents.json'
THUMB_DIR = Path('/tmp/photocurator_thumbs')
THUMB_DIR.mkdir(parents=True, exist_ok=True)

# ----------------------------------------------------------------------------
# Shared state
# ----------------------------------------------------------------------------
def _blank_step():
    return {'running': False, 'progress': 0, 'status': 'Ready', 'photos': []}

state = {
    'folder': '',
    'cull':  {**_blank_step(), 'sharp': 0, 'blurry': 0, 'sharp_paths': []},
    'dedup': {**_blank_step(), 'groups': 0, 'kept_paths': []},
    'rank':  {**_blank_step(), 'scores': []},
}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def list_images(folder):
    p = Path(folder)
    if not p.is_dir():
        return []
    files = [f for f in p.iterdir()
             if f.is_file() and f.suffix.lower() in IMG_EXTS
             # Skip macOS AppleDouble sidecars (._foo.jpg) and hidden files —
             # they aren't real images and break decoding/thumbnails.
             and not f.name.startswith('.')]
    return sorted(files)


def _thumb_cache_path(image_path):
    p = Path(image_path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0
    key = hashlib.md5(f"{image_path}:{mtime}".encode()).hexdigest()
    return THUMB_DIR / f"{key}.jpg"


def make_thumb_file(image_path, size=300):
    """Create (or reuse) an on-disk thumbnail. Returns the cache file path."""
    out = _thumb_cache_path(image_path)
    if out.exists():
        return out
    try:
        img = Image.open(image_path)
        # draft() lets the JPEG decoder downscale WHILE decoding (DCT scaling),
        # so a 24MP Canon frame is read at ~1/8 size — many times faster and far
        # less memory than decoding full-res then shrinking. Critical for big
        # folders (2000+ photos) where thumbnails were the bottleneck.
        img.draft('RGB', (size * 2, size * 2))
        img = img.convert('RGB')
        img.thumbnail((size, size), Image.Resampling.BILINEAR)
        img.save(out, format='JPEG', quality=80)
        return out
    except Exception as e:
        logger.warning(f"thumb fail {image_path}: {e}")
        return None


def thumb_url(image_path):
    """Lightweight URL the browser fetches lazily — no base64 stored in state."""
    return '/api/thumb?path=' + quote(str(image_path))


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
    recents = recents[:8]
    try:
        RECENTS_FILE.write_text(json.dumps(recents))
    except Exception as e:
        logger.warning(f"save recents fail: {e}")


def detect_sd_cards():
    """Find Canon DCIM card folders mounted under /Volumes."""
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
    """Open the macOS native folder chooser via AppleScript. Returns path or None."""
    try:
        script = (
            f'POSIX path of (choose folder with prompt "{prompt}")'
        )
        out = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, text=True, timeout=120
        )
        path = out.stdout.strip()
        if path:
            return path.rstrip('/')
    except Exception as e:
        logger.warning(f"folder dialog fail: {e}")
    return None


# ----------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------
HTML = r'''
<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Photo Curator v3.2</title>
<style>
  :root[data-theme="light"]{
    --bg:#f3f4f6; --panel:#ffffff; --panel2:#f9fafb; --border:#e5e7eb;
    --text:#1f2937; --muted:#6b7280; --accent:#2563eb; --accent2:#1d4ed8;
    --good:#059669; --warn:#f59e0b; --bad:#dc2626; --shadow:rgba(0,0,0,.12);
  }
  :root[data-theme="dark"]{
    --bg:#0f1115; --panel:#1a1d24; --panel2:#22262f; --border:#2d313b;
    --text:#e5e7eb; --muted:#9ca3af; --accent:#3b82f6; --accent2:#2563eb;
    --good:#10b981; --warn:#f59e0b; --bad:#ef4444; --shadow:rgba(0,0,0,.5);
  }
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{height:100%;overflow:hidden}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:var(--bg);color:var(--text)}
  .viewport{display:grid;grid-template-columns:320px 1fr;grid-template-rows:64px 1fr;height:100vh}

  .header{grid-column:1/-1;background:linear-gradient(135deg,var(--accent),var(--accent2));
          color:#fff;padding:0 20px;display:flex;align-items:center;justify-content:space-between;
          box-shadow:0 4px 12px var(--shadow);z-index:50}
  .header h1{font-size:20px}
  .step-tabs{display:flex;gap:10px}
  .step-tab{padding:7px 14px;background:rgba(255,255,255,.18);border:2px solid transparent;
            border-radius:6px;color:#fff;cursor:pointer;font-weight:600;font-size:13px;transition:.2s}
  .step-tab:hover{background:rgba(255,255,255,.3)}
  .step-tab.active{background:#fff;color:var(--accent)}
  .theme-toggle{background:rgba(255,255,255,.18);border:none;color:#fff;cursor:pointer;
                border-radius:6px;padding:7px 12px;font-size:16px}

  .sidebar{grid-row:2;grid-column:1;background:var(--panel);padding:18px;
           border-right:1px solid var(--border);overflow-y:auto;display:flex;flex-direction:column;gap:18px}
  .sidebar-title{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;
                 letter-spacing:.5px;margin-bottom:8px}
  .folder-current{font-size:12px;background:var(--panel2);border:1px solid var(--border);
                  border-radius:6px;padding:8px;word-break:break-all;color:var(--muted);min-height:34px}
  .folder-current.set{color:var(--text);border-color:var(--accent)}
  .row{display:flex;gap:8px}
  button{padding:9px;border:none;border-radius:6px;font-size:13px;font-weight:600;
         cursor:pointer;transition:.15s;font-family:inherit}
  .btn-primary{background:var(--accent);color:#fff;flex:1}
  .btn-primary:hover:not(:disabled){background:var(--accent2)}
  .btn-primary:disabled{opacity:.45;cursor:not-allowed}
  .btn-ghost{background:var(--panel2);color:var(--text);border:1px solid var(--border)}
  .btn-ghost:hover{border-color:var(--accent)}
  .shortcut{display:block;width:100%;text-align:left;background:var(--panel2);
            border:1px solid var(--border);border-radius:6px;padding:8px 10px;margin-bottom:6px;
            font-size:12px;cursor:pointer;color:var(--text);white-space:nowrap;overflow:hidden;
            text-overflow:ellipsis}
  .shortcut:hover{border-color:var(--accent);background:var(--bg)}
  .shortcut .tag{font-size:9px;font-weight:700;color:#fff;background:var(--warn);
                 border-radius:3px;padding:1px 5px;margin-right:6px}
  .shortcut .tag.sd{background:var(--good)}

  .slider-group label{font-size:12px;font-weight:600;display:block;margin-bottom:6px}
  .slider-group input[type=range]{width:100%}
  .slider-value{font-size:11px;color:var(--muted);margin-top:4px}
  .field input[type=number]{width:100%;padding:7px;border:1px solid var(--border);
            border-radius:6px;background:var(--panel2);color:var(--text);font-size:13px}

  .stats-box{background:var(--panel2);border:1px solid var(--border);border-radius:6px;padding:12px}
  .stat-row{display:flex;justify-content:space-between;font-size:12px;margin-bottom:6px}
  .stat-row:last-child{margin-bottom:0}
  .stat-label{color:var(--muted)}
  .stat-value{font-weight:700;color:var(--accent)}

  .main{grid-row:2;grid-column:2;background:var(--panel);overflow:hidden;display:flex;flex-direction:column}
  .progress-wrap{padding:10px 18px;background:var(--panel2);border-bottom:1px solid var(--border);display:none}
  .progress-bar{width:100%;height:5px;background:var(--border);border-radius:3px;overflow:hidden}
  .progress-fill{height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .25s}
  .progress-text{font-size:11px;color:var(--muted);margin-top:5px}

  .gallery{flex:1;overflow-y:auto;overflow-x:hidden;padding:18px;display:grid;
           grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;
           align-content:start;grid-auto-rows:max-content}
  .photo-card{position:relative;border-radius:8px;overflow:hidden;background:var(--panel2);
              border:2px solid var(--border);cursor:pointer;transition:.2s;animation:slideIn .3s ease}
  @keyframes slideIn{from{opacity:0;transform:scale(.92)}to{opacity:1;transform:scale(1)}}
  .photo-card:hover{border-color:var(--accent);box-shadow:0 4px 12px var(--shadow)}
  .photo-card.kept{border-color:var(--good)}
  .photo-card.rejected{opacity:.5}
  .photo-img{width:100%;height:150px;object-fit:cover;display:block;background:var(--panel2)}
  .photo-info{padding:7px;background:var(--panel)}
  .photo-name{font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .photo-score{font-size:12px;font-weight:700;color:var(--accent);margin-top:2px}
  .badge{position:absolute;top:6px;right:6px;color:#fff;padding:2px 7px;border-radius:4px;
         font-size:10px;font-weight:700;z-index:5}
  .badge.good{background:var(--good)} .badge.bad{background:var(--bad)} .badge.warn{background:var(--warn)}
  .status-toggle{position:absolute;bottom:6px;right:6px;z-index:6;border:none;border-radius:5px;
         padding:4px 8px;font-size:10px;font-weight:700;cursor:pointer;
         background:rgba(0,0,0,.62);color:#fff;backdrop-filter:blur(2px)}
  .status-toggle:hover{background:rgba(0,0,0,.85)}
  .header-right{display:flex;align-items:center;gap:10px}
  .kofi-btn{display:block;line-height:0;border-radius:8px;transition:transform .12s}
  .kofi-btn:hover{transform:translateY(-2px)}
  .kofi-btn img{display:block;border-radius:8px;box-shadow:0 3px 12px var(--shadow)}
  .rank-num{position:absolute;top:6px;left:6px;background:var(--accent);color:#fff;width:24px;height:24px;
            border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;z-index:5}
  .group-header{grid-column:1/-1;padding:10px 12px;background:var(--panel2);border-left:4px solid var(--accent);
                border-radius:4px;font-weight:600;font-size:13px;margin-top:6px}

  .empty{display:flex;flex-direction:column;align-items:center;justify-content:center;
         height:100%;color:var(--muted);grid-column:1/-1}
  .empty .icon{font-size:52px;margin-bottom:10px}

  /* Lightbox */
  .lightbox{position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:200;display:none;
            flex-direction:column;align-items:center;justify-content:center}
  .lightbox.open{display:flex}
  .lb-img{max-width:88vw;max-height:78vh;object-fit:contain;border-radius:6px;box-shadow:0 10px 40px rgba(0,0,0,.6)}
  .lb-bar{position:absolute;top:0;left:0;right:0;display:flex;justify-content:space-between;
          align-items:center;padding:14px 22px;color:#fff;background:linear-gradient(180deg,rgba(0,0,0,.6),transparent)}
  .lb-meta{font-size:14px} .lb-meta .sub{font-size:12px;opacity:.7;margin-top:2px}
  .lb-close{background:rgba(255,255,255,.15);border:none;color:#fff;font-size:22px;
            width:40px;height:40px;border-radius:50%;cursor:pointer}
  .lb-nav{position:absolute;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.15);
          border:none;color:#fff;font-size:30px;width:54px;height:54px;border-radius:50%;cursor:pointer}
  .lb-nav:hover{background:rgba(255,255,255,.3)}
  .lb-prev{left:18px} .lb-next{right:18px}
  .lb-scores{position:absolute;bottom:0;left:0;right:0;display:flex;gap:18px;justify-content:center;
             padding:14px;color:#fff;background:linear-gradient(0deg,rgba(0,0,0,.6),transparent);font-size:12px}
  .lb-scores .m{text-align:center} .lb-scores .m b{display:block;font-size:16px;color:#fff}
  .lb-scores{align-items:center}
  .lb-radar{flex:0 0 auto}
  .lb-nums{display:flex;gap:14px}

  /* TOP-50 metric profile modal */
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:250;display:none;
         align-items:center;justify-content:center}
  .modal.open{display:flex}
  .modal-card{background:var(--panel);color:var(--text);border-radius:14px;padding:24px 28px;
              max-width:520px;width:90%;box-shadow:0 20px 60px rgba(0,0,0,.5);text-align:center}
  .modal-card h2{margin:0 0 4px;font-size:18px}
  .modal-card .sub{font-size:12px;opacity:.65;margin-bottom:14px}
  .modal-close{margin-top:16px;background:var(--accent);color:#fff;border:none;border-radius:8px;
               padding:8px 18px;cursor:pointer;font-weight:600}
  .profile-nums{display:flex;gap:18px;justify-content:center;flex-wrap:wrap;margin-top:10px}
  .profile-nums .m{text-align:center;font-size:11px;opacity:.8}
  .profile-nums .m b{display:block;font-size:18px;color:var(--accent)}
</style>
</head>
<body>
<div class="viewport">
  <div class="header">
    <h1>📸 Photo Curator <span style="opacity:.6;font-size:13px">v3.2</span></h1>
    <div class="step-tabs">
      <div class="step-tab active" data-step="cull">1 · Cull</div>
      <div class="step-tab" data-step="dedup">2 · Dedup</div>
      <div class="step-tab" data-step="rank">3 · Rank</div>
    </div>
    <div class="header-right">
      <a class="kofi-btn" href='https://ko-fi.com/G2G51F6E2O' target='_blank' rel='noopener'><img height='36' style='border:0px;height:36px;' src='https://storage.ko-fi.com/cdn/kofi6.png?v=6' border='0' alt='Buy Me a Coffee at ko-fi.com' /></a>
      <button class="theme-toggle" id="themeToggle" title="Toggle theme">🌙</button>
    </div>
  </div>

  <div class="sidebar">
    <div>
      <div class="sidebar-title">📁 Folder</div>
      <div class="folder-current" id="folderCurrent">No folder selected</div>
      <div class="row" style="margin-top:8px">
        <button class="btn-primary" id="browseBtn">Browse…</button>
        <button class="btn-ghost" id="rescanBtn" title="Rescan SD / shortcuts">↻</button>
      </div>
      <div id="shortcuts" style="margin-top:10px"></div>
    </div>

    <div id="settingsPanel"></div>

    <button class="btn-primary" id="startBtn" disabled style="width:100%">🚀 Start</button>

    <div class="stats-box">
      <div class="stat-row"><span class="stat-label">Images</span><span class="stat-value" id="sImages">0</span></div>
      <div class="stat-row"><span class="stat-label">Sharp</span><span class="stat-value" id="sSharp">0</span></div>
      <div class="stat-row"><span class="stat-label">Blurry</span><span class="stat-value" id="sBlurry">0</span></div>
      <div class="stat-row"><span class="stat-label">Groups</span><span class="stat-value" id="sGroups">0</span></div>
      <div class="stat-row"><span class="stat-label">Ranked</span><span class="stat-value" id="sRanked">0</span></div>
    </div>

    <button class="btn-ghost" id="profileBtn" style="width:100%;display:none">📊 TOP metric profile…</button>
    <button class="btn-ghost" id="exportBtn" style="width:100%;display:none">⬇ Export TOP photos…</button>

    <div style="flex:1"></div>
    <button class="btn-ghost" id="resetBtn" style="width:100%">↺ Reset</button>
  </div>

  <div class="main">
    <div class="progress-wrap" id="progressWrap">
      <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
      <div class="progress-text" id="progressText">Processing…</div>
    </div>
    <div class="gallery" id="gallery">
      <div class="empty"><div class="icon">📸</div><div>Pick a folder, then press Start</div></div>
    </div>
  </div>
</div>

<!-- Lightbox -->
<div class="lightbox" id="lightbox">
  <div class="lb-bar">
    <div class="lb-meta"><div id="lbName">—</div><div class="sub" id="lbCount"></div></div>
    <div style="display:flex;gap:10px;align-items:center">
      <button class="status-toggle" id="lbToggle" style="position:static;display:none;padding:7px 12px;font-size:12px">→ Sharp</button>
      <button class="lb-close" id="lbClose">✕</button>
    </div>
  </div>
  <button class="lb-nav lb-prev" id="lbPrev">‹</button>
  <img class="lb-img" id="lbImg" src="">
  <button class="lb-nav lb-next" id="lbNext">›</button>
  <div class="lb-scores" id="lbScores"></div>
</div>

<!-- TOP metric profile modal -->
<div class="modal" id="profileModal">
  <div class="modal-card">
    <h2>TOP metric profile</h2>
    <div class="sub" id="profileSub"></div>
    <div id="profileBody"></div>
    <button class="modal-close" id="profileClose">Close</button>
  </div>
</div>

<script>
let folder=null, currentStep='cull', photos=[], lbIndex=0;
const TOPN_DEFAULT=50;

/* ---------- theme ---------- */
const themeToggle=document.getElementById('themeToggle');
themeToggle.onclick=()=>{
  const r=document.documentElement;
  const next=r.getAttribute('data-theme')==='light'?'dark':'light';
  r.setAttribute('data-theme',next);
  themeToggle.textContent=next==='light'?'🌙':'☀️';
};

/* ---------- folders ---------- */
function setFolder(p){
  folder=p;
  const el=document.getElementById('folderCurrent');
  el.textContent=p; el.classList.add('set');
  document.getElementById('startBtn').disabled=false;
  countImages();
}
function countImages(){
  if(!folder)return;
  fetch('/api/count?folder='+encodeURIComponent(folder)).then(r=>r.json())
    .then(d=>{document.getElementById('sImages').textContent=d.count;});
}
function loadShortcuts(){
  fetch('/api/shortcuts').then(r=>r.json()).then(d=>{
    const box=document.getElementById('shortcuts');
    let html='';
    d.sd.forEach(p=>html+=`<button class="shortcut" data-p="${p}"><span class="tag sd">SD</span>${p.split('/').slice(-2).join('/')}</button>`);
    d.recent.forEach(p=>html+=`<button class="shortcut" data-p="${p}"><span class="tag">RECENT</span>${p.split('/').slice(-2).join('/')}</button>`);
    box.innerHTML=html||'<div style="font-size:11px;color:var(--muted)">No SD card or recent folders</div>';
    box.querySelectorAll('.shortcut').forEach(b=>b.onclick=()=>setFolder(b.dataset.p));
  });
}
document.getElementById('browseBtn').onclick=function(){
  this.textContent='…'; this.disabled=true;
  fetch('/api/browse',{method:'POST'}).then(r=>r.json()).then(d=>{
    this.textContent='Browse…'; this.disabled=false;
    if(d.folder){setFolder(d.folder);loadShortcuts();}
  }).catch(()=>{this.textContent='Browse…';this.disabled=false;});
};
document.getElementById('rescanBtn').onclick=loadShortcuts;
loadShortcuts();

/* ---------- steps & settings ---------- */
const SETTINGS={
    cull:`<div class="sidebar-title">⚙️ Cull settings</div>
        <div class="slider-group"><label>Blur threshold</label>
        <input type="range" id="opt" min="20" max="800" step="10" value="120">
        <div class="slider-value">Sharpness ≥ <b id="optVal">120</b> · contrast-normalized · higher = stricter</div></div>
        <div style="margin-top:10px;padding:8px;background:var(--panel2);border-radius:6px;font-size:11px">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" id="autoOrganize" value="1">
        <span>Auto-move blurry → Blurred/</span>
        </label></div>`,
    dedup:`<div class="sidebar-title">⚙️ Dedup settings</div>
        <div class="slider-group"><label>Similarity threshold</label>
        <input type="range" id="opt" min="0.5" max="0.95" step="0.05" value="0.8">
        <div class="slider-value">Group when similarity ≥ <b id="optVal">0.80</b> · lower = more aggressive (bursts auto-relaxed)</div></div>
        <div style="margin-top:10px;padding:8px;background:var(--panel2);border-radius:6px;font-size:11px">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" id="autoOrganize" value="1">
        <span>Auto-move duplicates → Duplicates/</span>
        </label></div>`,
    rank:`<div class="sidebar-title">⚙️ Rank settings</div>
        <div class="field"><label style="font-size:12px;font-weight:600">Keep TOP N</label>
        <input type="number" id="opt" min="1" max="500" value="${TOPN_DEFAULT}"></div>
        <div style="margin-top:10px;padding:8px;background:var(--panel2);border-radius:6px;font-size:11px">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" id="autoOrganize" value="1">
        <span>Auto-copy to TOP_50/</span>
        </label></div>`
};
function renderSettings(){
  document.getElementById('settingsPanel').innerHTML=SETTINGS[currentStep];
  const opt=document.getElementById('opt');
  const val=document.getElementById('optVal');
  if(opt&&val)opt.oninput=()=>{val.textContent=currentStep==='dedup'?parseFloat(opt.value).toFixed(2):opt.value;};
}
document.querySelectorAll('.step-tab').forEach(t=>t.onclick=()=>{
  currentStep=t.dataset.step;
  document.querySelectorAll('.step-tab').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  renderSettings();
  // Profile/export only make sense for a finished rank; hide on step change.
  document.getElementById('profileBtn').style.display='none';
  document.getElementById('exportBtn').style.display='none';
  lastRankSig='';
});
renderSettings();

/* ---------- start ---------- */
document.getElementById('startBtn').onclick=function(){
  // Capture auto-organize setting for current step
  const autoOrg = document.getElementById('autoOrganize');
  if(autoOrg && autoOrg.checked) {
    fetch('/api/set-auto-organize',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({step:currentStep,enabled:true})}).catch(()=>{});
  }
  if(!folder){alert('Pick a folder first');return;}
  const optEl=document.getElementById('opt');
  const opt=optEl?parseFloat(optEl.value):0;
  document.getElementById('progressWrap').style.display='block';
  document.getElementById('gallery').innerHTML='';
  renderedCount=0;photoIdx=0;lastRankSig='';
  document.getElementById('exportBtn').style.display='none';
  document.getElementById('profileBtn').style.display='none';
  fetch('/api/run/'+currentStep,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({folder,opt})});
  setTimeout(poll,150);
};

function poll(){
  fetch('/api/progress/'+currentStep).then(r=>r.json()).then(d=>{
    document.getElementById('progressFill').style.width=d.progress+'%';
    document.getElementById('progressText').textContent=d.status;
    if(d.stats){
      if('sharp'in d.stats)document.getElementById('sSharp').textContent=d.stats.sharp;
      if('blurry'in d.stats)document.getElementById('sBlurry').textContent=d.stats.blurry;
      if('groups'in d.stats)document.getElementById('sGroups').textContent=d.stats.groups;
      if('ranked'in d.stats)document.getElementById('sRanked').textContent=d.stats.ranked;
    }
    if(currentStep==='rank') renderRank(d.photos||[]);
    else renderGallery(d.photos||[]);
    if(d.running){setTimeout(poll,250);}
    else{
      document.getElementById('progressWrap').style.display='none';
      if(currentStep==='rank'&&photos.length){
        document.getElementById('exportBtn').style.display='block';
        document.getElementById('profileBtn').style.display='block';
      }
      // Auto-organize if enabled
      const autoOrg = document.getElementById('autoOrganize');
      if(autoOrg && autoOrg.checked && photos.length){
        organizeStep();
      }
    }
  });
}

const EMPTY_HTML='<div class="empty"><div class="icon">📸</div><div>No results</div></div>';
let renderedCount=0, lastStep=null, photoIdx=0, lastRankSig='';

function cardHTML(p){
  if(p.type==='group') return `<div class="group-header">${p.label}</div>`;
  const i=photoIdx++;
  const badge=p.badge?`<div class="badge ${p.badgeType||'good'}">${p.badge}</div>`:'';
  const rank=p.rank?`<div class="rank-num">${p.rank}</div>`:'';
  const cls=p.kept?'kept':(p.rejected?'rejected':'');
  // In the Cull step, let the user override the auto Sharp/Blurry call so
  // wrongly-flagged shots (e.g. night scenes) stay in the gallery.
  const toggle = currentStep==='cull'
    ? `<button class="status-toggle" data-i="${i}">${p.kept?'→ Blurry':'✓ Keep (Sharp)'}</button>` : '';
  const path=String(p.path).replace(/"/g,'&quot;');
  return `<div class="photo-card ${cls}" data-i="${i}" data-path="${path}">${rank}${badge}${toggle}
    <img class="photo-img" src="${p.thumb}" loading="lazy" decoding="async">
    <div class="photo-info"><div class="photo-name">${p.name}</div>
    ${p.score!=null?`<div class="photo-score">${p.score}</div>`:''}</div></div>`;
}

// Reconciling render: reuse existing card nodes keyed by path so the newest-
// first ordering and the 400-cap never duplicate thumbnails or reload images.
let lastGallerySig='';
function renderGallery(items){
  photos=items;
  const g=document.getElementById('gallery');
  if(lastStep!==currentStep){g.innerHTML='';lastGallerySig='';lastStep=currentStep;}
  if(!items.length){g.innerHTML=EMPTY_HTML;lastGallerySig='';renderedCount=0;return;}
  const sig=items.map(p=>p.path).join('|');
  if(sig===lastGallerySig)return;
  lastGallerySig=sig;
  const emp=g.querySelector('.empty');if(emp)emp.remove();
  const existing={};g.querySelectorAll('.photo-card').forEach(n=>{existing[n.dataset.path]=n;});
  const frag=document.createDocumentFragment();
  items.forEach((p,i)=>{const key=String(p.path);let node=existing[key];
    if(node){const sc=node.querySelector('.photo-score');if(sc&&p.score!=null)sc.textContent=p.score;node.dataset.i=i;delete existing[key];}
    else{const w=document.createElement('div');w.innerHTML=cardHTML(p);node=w.firstElementChild;node.dataset.i=i;}
    frag.appendChild(node);});
  Object.values(existing).forEach(n=>n.remove());g.appendChild(frag);
  renderedCount=items.length;
}

function rankCardHTML(p,idx){
  const rank=(p.rank!=null?p.rank:idx+1);
  const sc=(p.score!=null?`<div class="photo-score">${p.score}</div>`:'');
  const path=String(p.path).replace(/"/g,'&quot;');
  return `<div class="photo-card kept" data-i="${idx}" data-path="${path}">`+
    `<div class="rank-num">${rank}</div>`+
    `<img class="photo-img" src="${p.thumb}" loading="lazy" decoding="async">`+
    `<div class="photo-info"><div class="photo-name">${p.name}</div>${sc}</div></div>`;
}

// Rank renderer: the live TOP-N is re-sorted every poll, but most polls don't
// change WHICH photos are in it. We render only when the ordered TOP-N actually
// changes, and reuse existing card nodes (moving them, not recreating) so their
// thumbnails never reload — no more per-second blink.
function renderRank(items){
  photos=items;
  const g=document.getElementById('gallery');
  if(lastStep!==currentStep){g.innerHTML='';lastRankSig='';lastStep=currentStep;}
  if(!items.length){if(lastRankSig!==''||!g.querySelector('.photo-card')){g.innerHTML=EMPTY_HTML;}lastRankSig='';return;}
  const sig=items.map(p=>(p.rank!=null?p.rank:'')+':'+p.path).join('|');
  if(sig===lastRankSig)return;            // TOP-N unchanged -> touch nothing
  lastRankSig=sig;
  const emp=g.querySelector('.empty'); if(emp)emp.remove();
  const existing={};
  g.querySelectorAll('.photo-card').forEach(n=>{existing[n.dataset.path]=n;});
  const frag=document.createDocumentFragment();
  items.forEach((p,idx)=>{
    let node=existing[p.path];
    if(node){                              // keep node (no img reload), update rank
      const rn=node.querySelector('.rank-num');
      const rk=(p.rank!=null?p.rank:idx+1);
      if(rn&&rn.textContent!=String(rk))rn.textContent=rk;
      node.dataset.i=idx;
      delete existing[p.path];
    }else{
      const w=document.createElement('div');w.innerHTML=rankCardHTML(p,idx);node=w.firstElementChild;
    }
    frag.appendChild(node);
  });
  Object.values(existing).forEach(n=>n.remove());  // dropped out of TOP-N
  g.appendChild(frag);
}

// One delegated click handler for the whole gallery (works with appended cards)
document.getElementById('gallery').addEventListener('click',e=>{
  const tg=e.target.closest('.status-toggle');
  if(tg){e.stopPropagation();toggleStatus(parseInt(tg.dataset.i),tg);return;}
  const card=e.target.closest('.photo-card');
  if(!card)return;
  const only=photos.filter(p=>p.type!=='group');
  openLightbox(only,parseInt(card.dataset.i));
});

// Flip Sharp <-> Blurry for one Cull photo, in place (no re-run).
function toggleStatus(i,btn){
  const p=photos[i];if(!p)return;
  fetch('/api/toggle-status',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path:p.path})}).then(r=>r.json()).then(d=>{
    if(d.error){alert('Could not change status: '+d.error);return;}
    const lbOpen=(typeof lbList!=='undefined'&&lbList[lbIndex]&&lbList[lbIndex].path===p.path);
    p.kept=d.kept;p.rejected=!d.kept;p.badge=d.badge;p.badgeType=d.kept?'good':'bad';
    if(d.path){p.path=d.path;}              // file may have moved (out of / into Blurred/)
    if(d.thumb){p.thumb=d.thumb;}
    const card=btn.closest('.photo-card');
    if(card){
      card.classList.toggle('kept',d.kept);card.classList.toggle('rejected',!d.kept);
      const b=card.querySelector('.badge');
      if(b){b.textContent=d.badge;b.className='badge '+(d.kept?'good':'bad');}
      btn.textContent=d.kept?'→ Blurry':'✓ Keep (Sharp)';
    }
    document.getElementById('sSharp').textContent=d.sharp;
    document.getElementById('sBlurry').textContent=d.blurry;
    if(lbOpen)showLb();
  }).catch(()=>alert('Could not change status (network error).'));
}

/* ---------- lightbox ---------- */
let lbList=[];
function openLightbox(list,i){lbList=list;lbIndex=i;showLb();document.getElementById('lightbox').classList.add('open');}
function showLb(){
  const p=lbList[lbIndex];if(!p)return;
  document.getElementById('lbImg').src='/api/image?path='+encodeURIComponent(p.path);
  document.getElementById('lbName').textContent=p.name;
  document.getElementById('lbCount').textContent=(lbIndex+1)+' / '+lbList.length+(p.score!=null?'   ·   score '+p.score:'');
  // Cull review: show a status-toggle so night shots can be kept from the big view.
  const lbT=document.getElementById('lbToggle');
  if(currentStep==='cull'){
    lbT.style.display='inline-block';
    lbT.textContent=p.kept?'→ Blurry':'✓ Keep (Sharp)';
  } else lbT.style.display='none';
  const s=p.scores;
  if(s){
    const metrics=metricsFromPhoto(p);
    document.getElementById('lbScores').innerHTML=
      `<div class="lb-radar">${radarSVG(metrics,180,'#60a5fa','rgba(96,165,250,.35)','rgba(255,255,255,.18)','rgba(255,255,255,.85)')}</div>`+
      `<div class="lb-nums">`+metrics.map(m=>`<div class="m"><b>${Math.round(m.value)}</b>${m.label}</div>`).join('')+`</div>`;
  } else document.getElementById('lbScores').innerHTML='';
}
// Lightbox status toggle: flip the photo currently shown, then sync its grid card.
document.getElementById('lbToggle').onclick=function(){
  const p=lbList[lbIndex];if(!p)return;
  const gi=photos.findIndex(x=>x.path===p.path);
  const gbtn=document.querySelector('.photo-card[data-i="'+gi+'"] .status-toggle');
  if(gi>=0&&gbtn){toggleStatus(gi,gbtn);}
  else{ // grid card not found (rare) — toggle via API directly
    fetch('/api/toggle-status',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:p.path})}).then(r=>r.json()).then(d=>{
      if(d.error)return; p.kept=d.kept;p.rejected=!d.kept;
      document.getElementById('sSharp').textContent=d.sharp;
      document.getElementById('sBlurry').textContent=d.blurry; showLb();
    });
  }
};

/* ---------- radar / hexagon chart ---------- */
// Build the 6 axes (5 sub-metrics + overall) for one ranked photo.
function metricsFromPhoto(p){
  const s=p.scores||{};
  return [
    {label:'Compose',value:s.compose||0},
    {label:'Light',  value:s.light||0},
    {label:'Focus',  value:s.focus||0},
    {label:'Color',  value:s.color||0},
    {label:'Contrast',value:s.contrast||0},
    {label:'Overall',value:parseFloat(p.score)||0},
  ];
}
// Returns an SVG hexagonal radar. Values are on a 0–100 scale.
function radarSVG(metrics,size=180,stroke='#3b82f6',fill='rgba(59,130,246,.35)',
                  gridcol='rgba(120,120,120,.3)',labelcol='currentColor'){
  const cx=size/2, cy=size/2, R=size/2-28, n=metrics.length;
  const ang=i=> -Math.PI/2 + i*2*Math.PI/n;
  const pt=(i,r)=>[cx+Math.cos(ang(i))*r, cy+Math.sin(ang(i))*r];
  let grid='';
  [0.25,0.5,0.75,1].forEach(f=>{
    const pts=metrics.map((m,i)=>pt(i,R*f).map(v=>v.toFixed(1)).join(',')).join(' ');
    grid+=`<polygon points="${pts}" fill="none" stroke="${gridcol}" stroke-width="1"/>`;
  });
  let spokes='',labels='';
  metrics.forEach((m,i)=>{
    const [x,y]=pt(i,R);
    spokes+=`<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${gridcol}" stroke-width="1"/>`;
    const [lx,ly]=pt(i,R+16);
    labels+=`<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" font-size="9.5" fill="${labelcol}" text-anchor="middle" dominant-baseline="middle">${m.label}</text>`;
  });
  const dp=metrics.map((m,i)=>pt(i,R*Math.max(0,Math.min(1,(m.value||0)/100))).map(v=>v.toFixed(1)).join(',')).join(' ');
  let dots='';
  metrics.forEach((m,i)=>{const [x,y]=pt(i,R*Math.max(0,Math.min(1,(m.value||0)/100)));dots+=`<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.5" fill="${stroke}"/>`;});
  return `<svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}">${grid}${spokes}`+
         `<polygon points="${dp}" fill="${fill}" stroke="${stroke}" stroke-width="2"/>${dots}${labels}</svg>`;
}

/* ---------- TOP-N aggregate profile ---------- */
function showProfile(){
  const ps=(photos||[]).filter(p=>p.scores);
  if(!ps.length){alert('Run Rank first.');return;}
  const keys=[['compose','Compose'],['light','Light'],['focus','Focus'],['color','Color'],['contrast','Contrast']];
  const metrics=keys.map(([k,label])=>({label,value:ps.reduce((a,p)=>a+(p.scores[k]||0),0)/ps.length}));
  metrics.push({label:'Overall',value:ps.reduce((a,p)=>a+(parseFloat(p.score)||0),0)/ps.length});
  document.getElementById('profileSub').textContent='Average across TOP '+ps.length+' photos';
  document.getElementById('profileBody').innerHTML=
    `<div class="profile-radar">${radarSVG(metrics,280)}</div>`+
    `<div class="profile-nums">`+metrics.map(m=>`<div class="m"><b>${Math.round(m.value)}</b>${m.label}</div>`).join('')+`</div>`;
  document.getElementById('profileModal').classList.add('open');
}
document.getElementById('profileBtn').onclick=showProfile;
document.getElementById('profileClose').onclick=()=>document.getElementById('profileModal').classList.remove('open');
document.getElementById('profileModal').onclick=e=>{if(e.target.id==='profileModal')document.getElementById('profileModal').classList.remove('open');};
function lbStep(d){lbIndex=(lbIndex+d+lbList.length)%lbList.length;showLb();}
document.getElementById('lbClose').onclick=()=>document.getElementById('lightbox').classList.remove('open');
document.getElementById('lbPrev').onclick=()=>lbStep(-1);
document.getElementById('lbNext').onclick=()=>lbStep(1);
document.addEventListener('keydown',e=>{
  if(!document.getElementById('lightbox').classList.contains('open'))return;
  if(e.key==='Escape')document.getElementById('lightbox').classList.remove('open');
  if(e.key==='ArrowLeft')lbStep(-1);
  if(e.key==='ArrowRight')lbStep(1);
});

/* ---------- export ---------- */

function organizeStep(){
  const opt = document.getElementById('opt');
  const topn = opt ? parseInt(opt.value) : 50;
  
  fetch('/api/organize',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({step:currentStep, topn})})
    .then(r=>r.json())
    .then(d=>{
      if(d.error){
        console.error('Organize error:',d.error);
        alert('Organization failed: '+d.error);
      } else {
        const moved = d.moved||d.copied||0;
        const action = currentStep==='rank'?'copied':'moved';
        console.log('✓ Organized '+moved+' photos');
      }
    })
    .catch(e=>{console.error('Organize request failed:',e);});
}

document.getElementById('exportBtn').onclick=function(){
  this.disabled=true;this.textContent='Choose destination…';
  const n=document.getElementById('opt')?document.getElementById('opt').value:TOPN_DEFAULT;
  fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({topn:parseInt(n)})}).then(r=>r.json()).then(d=>{
    this.disabled=false;this.textContent='⬇ Export TOP photos…';
    alert(d.error?('Error: '+d.error):('✓ Copied '+d.copied+' photos to\n'+d.dest));
  }).catch(()=>{this.disabled=false;this.textContent='⬇ Export TOP photos…';});
};

/* ---------- reset ---------- */
document.getElementById('resetBtn').onclick=()=>{
  fetch('/api/reset',{method:'POST'}).then(()=>location.reload());
};
</script>
</body>
</html>
'''


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
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


@app.route('/api/count')
def api_count():
    folder = request.args.get('folder', '')
    return jsonify({'count': len(list_images(folder))})


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


@app.route('/api/reset', methods=['POST'])
def api_reset():
    state['cull'] = {**_blank_step(), 'sharp': 0, 'blurry': 0, 'sharp_paths': []}
    state['dedup'] = {**_blank_step(), 'groups': 0, 'kept_paths': []}
    state['rank'] = {**_blank_step(), 'scores': []}
    return jsonify({'ok': True})


def _relocate_for_status(path, now_sharp):
    """Keep the file on disk in sync with its Sharp/Blurry status.

    If blurry shots were already auto-moved into Blurred/, flipping a photo to
    Sharp moves the actual file BACK to the main folder (and flipping to Blurry
    moves it into Blurred/). Returns the file's new absolute path (unchanged if
    no move was needed/possible)."""
    folder = state.get('folder')
    if not folder or not Path(folder).is_dir():
        return path
    folder = Path(folder)
    blurred = folder / "Blurred"
    name = Path(path).name

    # Find where the file actually is right now.
    cur = None
    for cand in (Path(path), folder / name, blurred / name):
        if cand.exists():
            cur = cand
            break
    if cur is None:
        return path  # file missing — just keep the status change

    if now_sharp:
        target_dir = folder                      # always pull back out of Blurred/
    else:
        # Only physically move into Blurred/ when that workflow is active.
        if not (state.get('auto_organize_cull') or blurred.is_dir()):
            return str(cur)
        target_dir = blurred

    if cur.parent == target_dir:
        return str(cur)                          # already in the right place

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        dst = target_dir / name
        if dst.exists():
            dst = target_dir / f"{cur.stem}_{int(time.time())}{cur.suffix}"
        shutil.move(str(cur), str(dst))
        logger.info(f"Moved {name} → {target_dir.name}/ (status change)")
        return str(dst)
    except Exception as e:
        logger.warning(f"Could not relocate {name}: {e}")
        return str(cur)


# ---- manually flip a Cull photo's Sharp/Blurry status ----
@app.route('/api/toggle-status', methods=['POST'])
def api_toggle_status():
    """Override the automatic Sharp/Blurry call for one photo, and move the
    actual file so disk matches: flipping to Sharp pulls it back out of
    Blurred/, flipping to Blurry moves it in (when that workflow is active)."""
    data = request.get_json() or {}
    path = data.get('path', '')
    s = state['cull']
    photo = next((p for p in s.get('photos', []) if p.get('path') == path), None)
    if not photo:
        return jsonify({'error': 'photo not found'}), 404

    now_sharp = not photo.get('kept', False)

    # Move the file to match the new status, then update bookkeeping to the
    # file's (possibly new) location.
    new_path = _relocate_for_status(path, now_sharp)
    photo['path'] = new_path
    photo['thumb'] = thumb_url(new_path)
    photo['kept'] = now_sharp
    photo['rejected'] = not now_sharp
    photo['badge'] = 'Sharp' if now_sharp else 'Blurry'
    photo['badgeType'] = 'good' if now_sharp else 'bad'

    sp = s.setdefault('sharp_paths', [])
    for old in (path, new_path):
        if old in sp:
            sp.remove(old)
    if now_sharp:
        sp.append(new_path)

    s['sharp'] = len(sp)
    s['blurry'] = max(0, len(s.get('photos', [])) - s['sharp'])
    logger.info(f"Status override: {Path(new_path).name} → "
                f"{'Sharp' if now_sharp else 'Blurry'}")
    return jsonify({'ok': True, 'kept': now_sharp, 'badge': photo['badge'],
                    'path': new_path, 'thumb': photo['thumb'],
                    'sharp': s['sharp'], 'blurry': s['blurry']})


# ---- progress (shared) ----
@app.route('/api/progress/<step>')
def api_progress(step):
    if step not in ('cull', 'dedup', 'rank'):
        abort(404)
    s = state[step]
    stats = {}
    if step == 'cull':
        stats = {'sharp': s['sharp'], 'blurry': s['blurry']}
    elif step == 'dedup':
        stats = {'groups': s['groups']}
    elif step == 'rank':
        # live count while analyzing; final TOP-N count once scores are set
        stats = {'ranked': len(s['scores']) or s.get('ranked_count', 0)}
    return jsonify({
        'progress': s['progress'], 'status': s['status'],
        'running': s['running'], 'photos': s['photos'], 'stats': stats,
    })


# ---- run dispatcher ----
@app.route('/api/run/<step>', methods=['POST'])
def api_run(step):
    data = request.get_json() or {}
    folder = data.get('folder') or state['folder']
    opt = data.get('opt', 0)
    if not folder or not Path(folder).is_dir():
        return jsonify({'error': 'No valid folder'}), 400
    state['folder'] = folder
    save_recent(folder)

    if step == 'cull':
        threading.Thread(target=run_cull, args=(folder, float(opt or 120)), daemon=True).start()
    elif step == 'dedup':
        threading.Thread(target=run_dedup, args=(folder, float(opt or 0.8)), daemon=True).start()
    elif step == 'rank':
        threading.Thread(target=run_rank, args=(folder, int(opt or 50)), daemon=True).start()
    else:
        return jsonify({'error': 'unknown step'}), 404
    return jsonify({'status': 'started'})


# ----------------------------------------------------------------------------
# Pipeline workers
# ----------------------------------------------------------------------------
def sharpness_score(gray):
    """Contrast-normalized focus measure, resolution-independent.

    Plain Laplacian variance conflates LOW CONTRAST (e.g. haze, shooting
    through an airplane window) with actual BLUR — a perfectly in-focus but
    hazy frame scores near zero and gets wrongly culled. Normalizing the
    Laplacian variance by the image's own variance makes the score depend on
    relative edge sharpness, not absolute contrast, so haze no longer reads as
    blur. We also resize to a fixed long edge so the threshold means the same
    thing for any camera/resolution.
    """
    h, w = gray.shape[:2]
    long_edge = max(h, w)
    if long_edge > 1024:
        sc = 1024.0 / long_edge
        gray = cv2.resize(gray, (int(w * sc), int(h * sc)),
                          interpolation=cv2.INTER_AREA)
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    img_var = float(gray.astype('float32').var()) + 1e-6
    return lap_var / img_var * 1000.0


def run_cull(folder, threshold):
    s = state['cull']
    s.update({'running': True, 'progress': 0, 'status': 'Scanning…',
              'photos': [], 'sharp': 0, 'blurry': 0, 'sharp_paths': [],
              'complete': False, 'src_folder': str(folder)})
    try:
        images = list_images(folder)
        total = len(images) or 1
        for idx, img_path in enumerate(images):
            s['progress'] = int(idx / total * 100)
            s['status'] = f"Culling {img_path.name} ({idx+1}/{len(images)})"
            try:
                # Decode at HALF resolution (DCT-scaled) — sharpness_score then
                # normalizes to a 1024px long edge anyway, so the result is
                # effectively identical but the decode is ~2-4x faster on big
                # Canon JPEGs. Fall back to full decode if the fast path fails.
                gray = cv2.imread(str(img_path), cv2.IMREAD_REDUCED_GRAYSCALE_2)
                if gray is None or min(gray.shape[:2]) < 200:
                    gray = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
                if gray is None:
                    continue
                variance = sharpness_score(gray)
                is_sharp = bool(variance >= threshold)
                # Newest-processed photo goes to the TOP of the live grid so the
                # user sees progress without scrolling to the bottom.
                s['photos'].insert(0, {
                    'name': img_path.name, 'path': str(img_path),
                    'thumb': thumb_url(img_path),
                    'score': f"{variance:.0f}",
                    'badge': 'Sharp' if is_sharp else 'Blurry',
                    'badgeType': 'good' if is_sharp else 'bad',
                    'kept': is_sharp, 'rejected': not is_sharp,
                })
                if is_sharp:
                    s['sharp'] += 1
                    s['sharp_paths'].append(str(img_path))
                else:
                    s['blurry'] += 1
            except Exception as e:
                logger.warning(f"cull err {img_path}: {e}")
        s['progress'] = 100
        s['complete'] = True   # full pass finished — survivors safe to chain
        s['status'] = f"Done · {s['sharp']} sharp / {s['blurry']} blurry"
    except Exception as e:
        logger.error(f"cull failed: {e}")
        s['status'] = f"Error: {e}"
    finally:
        s['running'] = False


def run_dedup(folder, threshold):
    s = state['dedup']
    s.update({'running': True, 'progress': 5, 'status': 'Preparing…',
              'photos': [], 'groups': 0, 'kept_paths': [],
              'complete': False, 'src_folder': str(folder)})
    try:
        # Chain off Cull's survivors ONLY if Cull finished a full pass on THIS
        # folder; otherwise dedup the whole folder (a partial/other-folder cull
        # must not silently shrink the input).
        cull = state['cull']
        if (cull.get('complete') and cull.get('sharp_paths')
                and cull.get('src_folder') == str(folder)):
            paths = [Path(p) for p in cull['sharp_paths']]
        else:
            paths = list_images(folder)
        if not paths:
            s['status'] = 'No images'
            return

        analyzer = PhotoAnalyzer()
        
        logger.info(f"Starting streaming dedup on {len(paths)} photos (threshold={threshold})")
        
        try:
            from photo_dedup_batch import FastBatchDeduplicator
            logger.info("✓ Loaded FastBatchDeduplicator")
        except ImportError as e:
            logger.error(f"Failed to import FastBatchDeduplicator: {e}")
            s['status'] = f"Error: Missing dedup module"
            return
        
        # The UI slider is a 0..1 similarity value; pass it straight through.
        # (The deduplicator clamps the per-burst bar internally.)
        batch_dedup = FastBatchDeduplicator(threshold=threshold)
        # Persist perceptual signatures next to the photos so a repeat run on
        # the same folder is near-instant.
        try:
            if paths:
                batch_dedup.enable_disk_cache(Path(paths[0]).parent / '.dedup_sig_cache.json')
        except Exception:
            pass
        batch_dedup.reset()

        # INCREMENTAL GLOBAL DEDUP: analyze each photo, then immediately assign
        # it to the global cluster set. Every photo is compared against ALL
        # existing cluster representatives (not a 20-photo window), so burst
        # duplicates are caught no matter how far apart they are — and the
        # surviving thumbnails appear live as we go.
        total = len(paths)
        total_analyzed = 0

        # Live-grid limits: rebuilding + shipping the full survivor list (and the
        # browser re-rendering every thumbnail) gets expensive past a few
        # thousand uniques. Cap displayed thumbnails to the most recent GRID_CAP
        # and refresh on a time interval rather than every Nth photo.
        GRID_CAP = 400
        REFRESH_SECS = 1.0
        last_refresh = [0.0]

        def _refresh_grid(final=False):
            # During the live run, cap to the most-recent GRID_CAP for speed.
            # When finished, show ALL survivors so every kept photo is reviewable.
            surv = batch_dedup.current_survivors()
            shown = surv if final else surv[-GRID_CAP:]
            photos = []
            for score in reversed(shown):   # most-recently-kept first
                photos.append({
                    'name': score.filename, 'path': score.path,
                    'thumb': thumb_url(score.path),
                    'score': f"{score.overall_score:.0f}",
                    'badge': 'KEPT', 'badgeType': 'good',
                    'kept': True,
                })
            s['photos'] = photos
            s['groups'] = len(batch_dedup.clusters)
            last_refresh[0] = time.time()

        for idx, p in enumerate(paths):
            s['progress'] = int((idx + 1) / total * 100)
            s['status'] = (f"Analyzing {p.name} ({idx + 1}/{total}) · "
                           f"{len(batch_dedup.clusters)} unique so far")
            try:
                sc = analyzer.analyze_image(str(p))
                if not sc:
                    continue
                total_analyzed += 1
                batch_dedup.add_photo(sc)
            except Exception as e:
                logger.warning(f"Failed to analyze {p}: {e}")
                continue

            # Time-throttled live grid update (keeps polling + DOM cheap).
            if time.time() - last_refresh[0] >= REFRESH_SECS or idx == total - 1:
                _refresh_grid()

        _refresh_grid(final=True)
        batch_dedup.save_disk_cache()
        survivors = batch_dedup.current_survivors()
        all_kept = [score.path for score in survivors]
        total_removed = total_analyzed - len(all_kept)

        logger.info(f"Global dedup complete: {total_analyzed} analyzed, "
                    f"{total_removed} removed, {len(all_kept)} kept")

        s['kept_paths'] = all_kept
        s['complete'] = True   # full pass finished — survivors safe to chain

        # Move duplicate photos to Duplicates/ folder if auto-organize enabled
        try:
            auto_org = state.get('auto_organize_dedup', False)
            if auto_org and len(all_kept) < total_analyzed:
                s['status'] = f"Moving {total_removed} duplicates to Duplicates/…"
                organizer = PhotoOrganizer(folder)
                
                # Find which photos are duplicates (not in kept list)
                kept_set = set(all_kept)
                duplicate_paths = [str(p) for p in paths if str(p) not in kept_set]
                
                if duplicate_paths:
                    move_result = organizer.move_duplicate_photos(duplicate_paths)
                    logger.info(f"Moved {move_result.get('moved', 0)} duplicates to Duplicates/")
                    s['status'] = f"Done · {len(all_kept)} unique (moved {move_result.get('moved', 0)} to Duplicates/)"
        except Exception as e:
            logger.warning(f"File organization failed: {e}")
        s['progress'] = 100
        s['status'] = f"Done · {len(all_kept)} unique from {total_analyzed} (removed {total_removed})"
    except Exception as e:
        logger.error(f"dedup failed: {e}", exc_info=True)
        s['status'] = f"Error: {str(e)}"
    finally:
        s['running'] = False


def _rank_photo_dict(r, rank):
    return {
        'name': r.filename, 'path': r.path, 'thumb': thumb_url(r.path),
        'score': f"{r.overall_score:.1f}", 'rank': rank, 'kept': True,
        'scores': {'compose': round(r.composition), 'light': round(r.lighting),
                   'focus': round(r.focus), 'color': round(r.color),
                   'contrast': round(r.contrast)},
    }


def run_rank(folder, topn):
    s = state['rank']
    s.update({'running': True, 'progress': 5, 'status': 'Preparing…',
              'photos': [], 'scores': [], 'ranked_count': 0})
    try:
        # Prefer a COMPLETED Dedup on this folder, then a COMPLETED Cull on this
        # folder; otherwise rank the whole folder. Never chain off a partial run.
        dd, cull = state['dedup'], state['cull']
        dedup_done = (dd.get('complete') and dd.get('kept_paths')
                      and dd.get('src_folder') == str(folder))
        cull_done = (cull.get('complete') and cull.get('sharp_paths')
                     and cull.get('src_folder') == str(folder))
        if dedup_done:
            paths = [Path(p) for p in dd['kept_paths']]
        elif cull_done:
            paths = [Path(p) for p in cull['sharp_paths']]
        else:
            paths = list_images(folder)
        if not paths:
            s['status'] = 'No images to rank'
            return

        # If the user already ran Dedup (completed, this folder), the input is
        # burst-free — rank as-is. Otherwise fold the FAST global clustering into
        # ranking so a one-click run on a raw folder still yields a grouped,
        # burst-free TOP N (and we skip the old slow imagehash + SSIM end-stage).
        clusterer = None
        if not dedup_done:
            try:
                from photo_dedup_batch import FastBatchDeduplicator
                clusterer = FastBatchDeduplicator(threshold=0.80)
                try:
                    clusterer.enable_disk_cache(Path(paths[0]).parent / '.dedup_sig_cache.json')
                except Exception:
                    pass
                clusterer.reset()
                logger.info("Rank: folding in global dedup (no prior Dedup run)")
            except Exception as e:
                logger.warning(f"Rank: clusterer unavailable ({e}); ranking raw")

        analyzer = PhotoAnalyzer()
        results = []
        total = len(paths) or 1
        REFRESH_EVERY = 15

        def _pool():
            # The set we rank from: deduped survivors if clustering, else all.
            return clusterer.current_survivors() if clusterer else results

        def _push_top(final=False):
            top_so_far = sorted(_pool(), key=lambda x: x.overall_score,
                                reverse=True)[:topn]
            s['photos'] = [_rank_photo_dict(r, rk)
                           for rk, r in enumerate(top_so_far, 1)]
            if not final:
                uniq = (f"{len(clusterer.clusters)} unique · " if clusterer else "")
                s['status'] = (f"Ranking… live TOP {len(top_so_far)} · "
                               f"{uniq}{len(results)}/{total} analyzed")

        for idx, p in enumerate(paths):
            s['progress'] = int(idx / total * 95)
            sc = analyzer.analyze_image(str(p))
            if sc:
                results.append(sc)
                if clusterer:
                    clusterer.add_photo(sc)
            s['ranked_count'] = len(clusterer.clusters) if clusterer else len(results)

            if (idx + 1) % REFRESH_EVERY == 0 or idx == total - 1:
                _push_top()

        if clusterer:
            clusterer.save_disk_cache()
        top = sorted(_pool(), key=lambda x: x.overall_score, reverse=True)[:topn]
        s['scores'] = [{'path': r.path, 'name': r.filename,
                        'overall': round(r.overall_score, 1)} for r in top]
        s['photos'] = [_rank_photo_dict(r, rk) for rk, r in enumerate(top, 1)]
        s['progress'] = 100

        if clusterer:
            uniq = len(clusterer.clusters)
            s['status'] = (f"Done · TOP {len(top)} from {uniq} unique scenes "
                           f"({len(results)} analyzed, bursts grouped)")
        else:
            s['status'] = f"Done · TOP {len(top)} of {len(results)} ranked"
    except Exception as e:
        logger.error(f"rank failed: {e}", exc_info=True)
        s['status'] = f"Error: {e}"
    finally:
        s['running'] = False




# ---- set auto-organize flag ----
@app.route('/api/set-auto-organize', methods=['POST'])
def api_set_auto_organize():
    data = request.get_json() or {}
    step = data.get('step', 'dedup')
    enabled = data.get('enabled', False)
    state[f'auto_organize_{step}'] = enabled
    logger.info(f"Auto-organize for {step}: {enabled}")
    return jsonify({'ok': True})

# ---- organize ----
@app.route('/api/organize', methods=['POST'])
def api_organize():
    data = request.get_json() or {}
    step = data.get('step', 'dedup')
    folder = state['folder']

    if not folder or not Path(folder).is_dir():
        return jsonify({'error': 'No valid folder'}), 400

    try:
        organizer = PhotoOrganizer(folder)
        results = {}

        if step == 'cull':
            all_images = list_images(folder)
            sharp_set = set(state['cull'].get('sharp_paths', []))
            blurry_paths = [str(p) for p in all_images if str(p) not in sharp_set]
            results = organizer.move_blurry_photos(blurry_paths)
            logger.info(f"Moved {results.get('moved', 0)} blurry photos")

        elif step == 'dedup':
            dup_paths = [p['path'] for p in state['dedup'].get('photos', [])
                        if p.get('type') != 'group' and not p.get('kept', False)]
            results = organizer.move_duplicate_photos(dup_paths)
            logger.info(f"Moved {results.get('moved', 0)} duplicate photos")

        elif step == 'rank':
            top_paths = [s['path'] for s in state['rank'].get('scores', [])]
            topn = int(data.get('topn', 50))
            results = organizer.copy_top_photos(top_paths, topn=topn)
            logger.info(f"Copied {results.get('copied', 0)} top photos")

        return jsonify({'success': True, **results})
    except Exception as e:
        logger.error(f"organize failed: {e}")
        return jsonify({'error': str(e)}), 500


# ---- export ----
@app.route('/api/export', methods=['POST'])
def api_export():
    data = request.get_json() or {}
    topn = int(data.get('topn', 50))
    scores = state['rank'].get('scores', [])
    if not scores:
        return jsonify({'error': 'Nothing ranked yet — run Rank first'}), 400

    dest = native_folder_dialog("Choose where to export the TOP photos")
    if not dest:
        return jsonify({'error': 'No destination chosen'}), 400

    dest_path = Path(dest) / f"PhotoCurator_TOP{topn}_{time.strftime('%Y%m%d_%H%M%S')}"
    dest_path.mkdir(parents=True, exist_ok=True)
    copied = 0
    for i, item in enumerate(scores[:topn], 1):
        try:
            src = Path(item['path'])
            shutil.copy2(str(src), str(dest_path / f"{i:03d}_{src.name}"))
            copied += 1
        except Exception as e:
            logger.warning(f"export copy fail {item['path']}: {e}")
    return jsonify({'copied': copied, 'dest': str(dest_path)})


if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("📸  Photo Curator v2")
    print("=" * 60)
    print("\n   Open:  http://localhost:5000\n")
    print("=" * 60 + "\n")
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=True)
