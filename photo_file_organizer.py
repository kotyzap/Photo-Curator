#!/usr/bin/env python3
"""
Photo File Organizer - Moves photos to subfolders based on curator decisions
Handles: Blurred (blurry), Duplicates, TOP_N folders
"""

import shutil
from pathlib import Path
import logging
from typing import List, Dict, Callable, Optional
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PhotoOrganizer:
    """Organizes photos into subfolders with safety checks"""
    
    def __init__(self, root_folder: str):
        self.root = Path(root_folder)
        self.rozmazane_dir = self.root / "Blurred"
        self.duplicates_dir = self.root / "Duplicates"
        self.topn_dir = None  # Set when exporting
        self.dry_run = False
        
    def _ensure_dir(self, path: Path) -> Path:
        """Create directory if it doesn't exist"""
        path.mkdir(parents=True, exist_ok=True)
        return path
    
    def move_blurry_photos(self, blurry_paths: List[str], 
                          progress_cb: Optional[Callable[[int, str], None]] = None) -> Dict:
        """Move blurry photos to Blurred/ folder"""
        results = {'moved': 0, 'failed': 0, 'skipped': 0, 'errors': []}
        
        if not blurry_paths:
            return results
        
        self._ensure_dir(self.rozmazane_dir)
        
        for idx, path_str in enumerate(blurry_paths):
            try:
                src = Path(path_str)
                if not src.exists():
                    results['skipped'] += 1
                    continue
                
                # Don't move if already in Blurred
                if 'Blurred' in str(src):
                    results['skipped'] += 1
                    continue
                
                dst = self.rozmazane_dir / src.name
                
                # Handle duplicates in destination
                if dst.exists():
                    name_parts = src.stem.split('_')
                    dst = self.rozmazane_dir / f"{src.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{src.suffix}"
                
                if not self.dry_run:
                    shutil.move(str(src), str(dst))
                results['moved'] += 1
                
                if progress_cb and (idx + 1) % 5 == 0:
                    progress_cb(int(idx / len(blurry_paths) * 100), 
                              f"Moving {idx + 1}/{len(blurry_paths)} blurry photos…")
            
            except Exception as e:
                logger.warning(f"Failed to move {path_str}: {e}")
                results['failed'] += 1
                results['errors'].append(str(e))
        
        logger.info(f"Moved {results['moved']} blurry photos to Blurred/")
        return results
    
    def move_duplicate_photos(self, duplicate_paths: List[str], 
                             progress_cb: Optional[Callable[[int, str], None]] = None) -> Dict:
        """Move duplicate photos to Duplicates/ folder"""
        results = {'moved': 0, 'failed': 0, 'skipped': 0, 'errors': []}
        
        if not duplicate_paths:
            return results
        
        self._ensure_dir(self.duplicates_dir)
        
        for idx, path_str in enumerate(duplicate_paths):
            try:
                src = Path(path_str)
                if not src.exists():
                    results['skipped'] += 1
                    continue
                
                # Don't move if already in Duplicates
                if 'Duplicates' in str(src):
                    results['skipped'] += 1
                    continue
                
                dst = self.duplicates_dir / src.name
                
                # Handle duplicates in destination
                if dst.exists():
                    dst = self.duplicates_dir / f"{src.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{src.suffix}"
                
                if not self.dry_run:
                    shutil.move(str(src), str(dst))
                results['moved'] += 1
                
                if progress_cb and (idx + 1) % 5 == 0:
                    progress_cb(int(idx / len(duplicate_paths) * 100),
                              f"Moving {idx + 1}/{len(duplicate_paths)} duplicates…")
            
            except Exception as e:
                logger.warning(f"Failed to move {path_str}: {e}")
                results['failed'] += 1
                results['errors'].append(str(e))
        
        logger.info(f"Moved {results['moved']} duplicate photos to Duplicates/")
        return results
    
    def copy_top_photos(self, top_photo_paths: List[str], topn: int = 50,
                       progress_cb: Optional[Callable[[int, str], None]] = None) -> Dict:
        """Copy top N photos to TOP_N/ folder (don't move originals)"""
        results = {'copied': 0, 'failed': 0, 'skipped': 0, 'errors': []}
        
        if not top_photo_paths:
            return results
        
        topn_name = f"TOP_{min(topn, len(top_photo_paths))}"
        self.topn_dir = self._ensure_dir(self.root / topn_name)
        
        for idx, path_str in enumerate(top_photo_paths[:topn]):
            try:
                src = Path(path_str)
                if not src.exists():
                    results['skipped'] += 1
                    continue
                
                # Number the output files
                dst_name = f"{idx + 1:03d}_{src.name}"
                dst = self.topn_dir / dst_name
                
                if not self.dry_run:
                    shutil.copy2(str(src), str(dst))
                results['copied'] += 1
                
                if progress_cb and (idx + 1) % 10 == 0:
                    progress_cb(int(idx / min(topn, len(top_photo_paths)) * 100),
                              f"Copying {idx + 1}/{min(topn, len(top_photo_paths))} top photos…")
            
            except Exception as e:
                logger.warning(f"Failed to copy {path_str}: {e}")
                results['failed'] += 1
                results['errors'].append(str(e))
        
        logger.info(f"Copied {results['copied']} top photos to {topn_name}/")
        return results
    
    def preview_moves(self, blurry_paths: List[str], duplicate_paths: List[str], 
                     top_paths: List[str]) -> Dict:
        """Return preview of what would be moved (dry run)"""
        self.dry_run = True
        result = {
            'blurry': self.move_blurry_photos(blurry_paths),
            'duplicates': self.move_duplicate_photos(duplicate_paths),
            'top': self.copy_top_photos(top_paths),
        }
        self.dry_run = False
        return result
