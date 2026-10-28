"""Check the shared header's two ends and accent form one parallel slant."""
import argparse
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image


def slant(pixels, name, yy0, yy1, threshold=180, accent=True):
    rows = []
    for yy in (yy0, yy1):
        row = pixels[yy]
        body = np.flatnonzero(row[:, 3] > threshold)
        yellow = np.flatnonzero((row[:, 0] > 170) & (row[:, 1] > 90) & (row[:, 1] < row[:, 0]*.9)
                                & (row[:, 2] < 70) & (row[:, 3] > 180))
        assert body.size and (yellow.size or not accent), (name, 'Missing header body or integrated accent')
        rows.append((yy, int(body[0]), int(body[-1]), float(np.median(yellow)) if accent else 0))
    expected = (rows[1][0]-rows[0][0])*math.tan(math.radians(10))
    travel = [rows[0][i]-rows[1][i] for i in ((1, 2, 3) if accent else (1, 2))]
    assert all(abs(t-expected) < 2 for t in travel), ('Header ends/accent slant differ', rows, travel, expected)
    return {'surface': name, 'passed': True, 'rows': rows, 'slant_displacements': travel, 'expected': expected}


def check(root):
    base = root/'Flatout/Data/Assets/GUI_FlatOut/Textures/Menus'
    folder = base/'FrontendEnhanced'
    sprite = json.loads((folder/'manifest.json').read_text())['sprites']['Header']
    atlas = Image.open(folder/'Atlas.png').convert('RGBA')
    x, y = [round(v*2048-.5) for v in sprite['uv'][:2]]
    w, h = sprite['size']
    reports = [slant(np.asarray(atlas.crop((x, y, x+w, y+h))), 'Garage / Quick Start shared header', 15, h-16)]
    folder = base/'OptionsEnhanced'
    skin = next(s for s in ET.parse(folder/'Options.PSK.XML').iter('skin') if s.attrib['Name'] == 'Status')
    uv = list(map(float, skin.attrib['uvs'].split(';')))
    box = [round(v*2048 + (-.5 if i < 2 else .5)) for i, v in enumerate(uv)]
    crop = np.asarray(Image.open(folder/'Options.png').convert('RGBA').crop(box))
    reports.append(slant(crop, 'Options header status plate', 12, crop.shape[0]-13, accent=False))
    folder = base/'ChallengeEnhanced'
    skin = next(s for s in json.loads((folder/'Styles.manifest.json').read_text())['materials'] if s['name'] == 'HeaderSurface')
    x, y, w, h = skin['atlas_rect']
    crop = np.asarray(Image.open(folder/'Styles.png').convert('RGBA').crop((x, y, x+w, y+h)))
    top = round(-skin['draw_offset'][1]+20)
    bottom = round(-skin['draw_offset'][1]+skin['reference']['height']-20)
    reports.append(slant(crop, 'FlatOut Mode integrated header', top, bottom, threshold=80))
    print(json.dumps(reports, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fox', required=True, type=Path)
    check(p.parse_args().fox)
