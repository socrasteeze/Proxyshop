"""
* Card Compose Engine (NAS / no Photoshop)
* Pokecardmaker-style pipeline: blank frame PNG + art + text overlay via Pillow.
* Must never import from `src/`.
"""
# Standard Library Imports
from pathlib import Path

# Package root for bundled procedural-frame defaults / optional blank PNGs
FRAMES_ROOT = Path(__file__).resolve().parent / 'frames'

# Standard proxy pixel size (≈ 63×88mm @ 300 DPI)
CARD_W = 750
CARD_H = 1050
