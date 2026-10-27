"""Check shipped UI chrome against the unchanged native menu's accent hue.

Pass the Fox checkout with --fox. Photographs and original mode/trophy artwork
are deliberately excluded: this check covers the generated interface atlases.
"""
import argparse
import json
from pathlib import Path
import re

import numpy as np
from PIL import Image


def check(root):
    original = (root/'Flatout/Code/Sources/Menus/FO3DNavigationMenu.cpp').read_text(encoding='cp1252')
    value = re.search(r'm_vEndColor\s*=\s*Vector3\(\s*1.f,\s*([0-9.]+)f,\s*0.f\s*\)', original)
    assert value, 'Missing original spatial menu accent reference'
    expected = float(value[1])
    reports = []
    for relative in ('FrontendEnhanced/Atlas.png', 'OptionsEnhanced/Options.png', 'ChallengeEnhanced/Styles.png'):
        image = np.asarray(Image.open(root/'Flatout/Data/Assets/GUI_FlatOut/Textures/Menus'/relative).convert('RGBA')).astype(float)
        r, g, b, a = np.moveaxis(image, -1, 0)
        spread = r-b
        ratio = (g-b)/np.maximum(spread, 1)
        warm = (a > 100) & (r > 140) & (spread > r*.65) & (ratio > .45) & (ratio < .95)
        assert int(warm.sum()) > 100, ('No meaningful opaque UI accent pixels', relative)
        observed = float(np.median(ratio[warm]))
        reports.append({'atlas': relative, 'original_green_fraction': expected,
                        'observed_hue_fraction': observed, 'samples': int(warm.sum()),
                        'passed': abs(observed-expected) < .025})
    print(json.dumps(reports, indent=2))
    assert all(r['passed'] for r in reports), 'Generated interface accent differs from the original game yellow'
    # Directly authored native amber helpers must follow the same policy too;
    # otherwise a correct atlas can conceal a different ray/button tint.
    number = rb'(?:\d*\.\d+|\d+\.?)(?:f)?'
    color = re.compile(rb'(?:Graphics::)?Color\(\s*('+number+rb')\s*,\s*('+number+rb')\s*,\s*('+number+rb')\s*(?:,\s*[^(),]+)?\)')
    mismatches = []
    for area in ('Menus', 'VR'):
        for file in (root/'Flatout/Code/Sources'/area).rglob('*'):
            if file.suffix not in ('.cpp', '.h', '.inl') or file.name.startswith('FORun'):
                continue
            for match in color.finditer(file.read_bytes()):
                red, green, blue = (float(v.decode().rstrip('f')) for v in match.groups())
                if red == 1 and .5 <= green <= .9 and 0 <= blue <= .15:
                    hue = (green-blue)/(red-blue)
                    if abs(hue-expected) > 2/255:
                        mismatches.append({'source': str(file.relative_to(root)), 'color': match[0].decode(), 'hue': hue})
    assert not mismatches, ('Native ray accents still use a different yellow', mismatches)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fox', type=Path, required=True)
    check(p.parse_args().fox)
