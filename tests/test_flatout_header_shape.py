"""Check the shared header's two ends and accent form one parallel slant."""
import argparse
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image


def check(root):
    folder = root/'Flatout/Data/Assets/GUI_FlatOut/Textures/Menus/FrontendEnhanced'
    manifest = json.loads((folder/'manifest.json').read_text())
    sprite = manifest['sprites']['Header']
    atlas = Image.open(folder/'Atlas.png').convert('RGBA')
    x, y = [round(v*2048-.5) for v in sprite['uv'][:2]]
    w, h = sprite['size']
    pixels = np.asarray(atlas.crop((x, y, x+w, y+h)))
    rows = []
    for yy in (15, h-16):
        row = pixels[yy]
        body = np.flatnonzero(row[:, 3] > 180)
        yellow = np.flatnonzero((row[:, 0] > 170) & (row[:, 1] > 90) & (row[:, 1] < row[:, 0]*.9)
                                & (row[:, 2] < 70) & (row[:, 3] > 180))
        assert body.size and yellow.size, 'The accent must be authored into the slanted header, not stretched vertically beside it'
        rows.append((yy, int(body[0]), int(body[-1]), float(np.median(yellow))))
    expected = (rows[1][0]-rows[0][0])*math.tan(math.radians(10))
    travel = [rows[0][i]-rows[1][i] for i in (1, 2, 3)]
    assert all(abs(t-expected) < 2 for t in travel), ('Header ends/accent slant differ', rows, travel, expected)
    print(json.dumps({'passed': True, 'rows': rows, 'slant_displacements': travel, 'expected': expected}, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fox', required=True, type=Path)
    check(p.parse_args().fox)
