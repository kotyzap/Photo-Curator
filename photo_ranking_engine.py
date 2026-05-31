#!/usr/bin/env python3
"""
Photo Ranking Engine - Analyzes sharp photos and scores them
Based on Gemini Photo Curator scoring criteria
"""

import cv2
import numpy as np
from pathlib import Path
from PIL import Image
import json
import logging
from dataclasses import dataclass
from typing import List, Dict
import threading
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class PhotoScore:
    filename: str
    path: str
    overall_score: float
    composition: float
    lighting: float
    focus: float
    color: float
    contrast: float
    timestamp: float = None

class PhotoAnalyzer:
    def __init__(self):
        self.scores = []
        self.analyzing = False

    def analyze_image(self, image_path: str) -> PhotoScore:
        """Analyze a single image and return scores"""
        try:
            img = cv2.imread(image_path)
            if img is None:
                return None

            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

            # 1. FOCUS SCORE (Laplacian variance)
            laplacian = cv2.Laplacian(gray, cv2.CV_64F)
            focus_score = min(100, (laplacian.var() / 10))

            # 2. LIGHTING SCORE (histogram analysis)
            hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
            # Check for good exposure (not too dark, not too bright)
            dark_pixels = np.sum(hist[0:50]) / hist.sum()
            bright_pixels = np.sum(hist[200:256]) / hist.sum()
            exposure_penalty = abs(dark_pixels - 0.15) * 50 + abs(bright_pixels - 0.15) * 50
            lighting_score = max(0, 100 - exposure_penalty)

            # 3. CONTRAST SCORE
            contrast = gray.std()
            contrast_score = min(100, contrast * 1.5)

            # 4. COLOR SCORE (saturation and diversity)
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            saturation = hsv[:, :, 1]
            color_diversity = saturation.std()
            color_score = min(100, color_diversity * 0.8)

            # 5. COMPOSITION SCORE (edges and structure)
            edges = cv2.Canny(gray, 50, 150)
            edge_density = np.count_nonzero(edges) / edges.size
            composition_score = min(100, edge_density * 300)  # More edges = more detail

            # 6. OVERALL SCORE (weighted average)
            overall = (
                focus_score * 0.25 +
                lighting_score * 0.20 +
                contrast_score * 0.20 +
                color_score * 0.20 +
                composition_score * 0.15
            )

            return PhotoScore(
                filename=Path(image_path).name,
                path=image_path,
                overall_score=float(overall),
                composition=float(composition_score),
                lighting=float(lighting_score),
                focus=float(focus_score),
                color=float(color_score),
                contrast=float(contrast_score),
                timestamp=time.time()
            )

        except Exception as e:
            logger.error(f"Error analyzing {image_path}: {e}")
            return None

    def analyze_directory(self, directory: str, callback=None) -> List[PhotoScore]:
        """Analyze all photos in directory"""
        self.analyzing = True
        self.scores = []

        path = Path(directory)
        image_files = []

        # Find all image files
        for ext in ['*.jpg', '*.jpeg', '*.png', '*.cr2', '*.tiff']:
            image_files.extend(path.glob(ext.lower()))
            image_files.extend(path.glob(ext.upper()))

        image_files = sorted(set(image_files))

        logger.info(f"Found {len(image_files)} images to analyze")

        for idx, img_path in enumerate(image_files):
            if not self.analyzing:
                break

            score = self.analyze_image(str(img_path))
            if score:
                self.scores.append(score)
                if callback:
                    callback({
                        'processed': idx + 1,
                        'total': len(image_files),
                        'filename': img_path.name
                    })

        # Sort by overall score
        self.scores.sort(key=lambda x: x.overall_score, reverse=True)
        self.analyzing = False

        return self.scores

    def get_top_n(self, n: int = 50) -> List[PhotoScore]:
        """Get top N photos by score"""
        return self.scores[:n]

    def copy_top_photos(self, top_n: int, destination: str) -> Dict:
        """Copy top N photos to destination folder"""
        dest_path = Path(destination)
        dest_path.mkdir(parents=True, exist_ok=True)

        results = {
            'copied': 0,
            'failed': 0,
            'files': []
        }

        for score in self.get_top_n(top_n):
            try:
                src = Path(score.path)
                dst = dest_path / src.name

                # For RAW files, use PIL fallback
                if src.suffix.lower() == '.cr2':
                    import shutil
                    shutil.copy2(str(src), str(dst))
                else:
                    img = Image.open(src)
                    img.save(dst)

                results['copied'] += 1
                results['files'].append({
                    'filename': src.name,
                    'score': round(score.overall_score, 1)
                })
                logger.info(f"Copied: {src.name} (score: {score.overall_score:.1f})")

            except Exception as e:
                results['failed'] += 1
                logger.error(f"Failed to copy {score.path}: {e}")

        return results

    def export_scores_json(self, filepath: str):
        """Export scores to JSON for web UI"""
        data = {
            'total_analyzed': len(self.scores),
            'top_50': [
                {
                    'rank': idx + 1,
                    'filename': score.filename,
                    'overall_score': round(score.overall_score, 1),
                    'composition': round(score.composition, 1),
                    'lighting': round(score.lighting, 1),
                    'focus': round(score.focus, 1),
                    'color': round(score.color, 1),
                    'contrast': round(score.contrast, 1),
                }
                for idx, score in enumerate(self.get_top_n(50))
            ]
        }

        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)

        logger.info(f"Exported scores to {filepath}")


def main():
    """Standalone testing"""
    analyzer = PhotoAnalyzer()

    # Example usage
    folder = "/Volumes/EOS_DIGITAL/DCIM/102CANON"
    if Path(folder).exists():
        scores = analyzer.analyze_directory(
            folder,
            callback=lambda s: print(f"[{s['processed']}/{s['total']}] {s['filename']}")
        )

        print("\n" + "="*60)
        print("TOP 10 PHOTOS")
        print("="*60)
        for idx, score in enumerate(analyzer.get_top_n(10), 1):
            print(f"{idx}. {score.filename:<40} {score.overall_score:>6.1f}/100")


if __name__ == '__main__':
    main()
