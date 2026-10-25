"""Audit a captured Options HUD texture using its native target rectangles.

Export the RGBA 1920x1080 menu render target from RenderDoc. The snapshot JSON
contains the read-only options.get_state and vr.targets results under those two
keys. Run from General with an enabled ON toggle visible.
"""
import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image


def audit(texture, snapshot):
    pixels = np.asarray(Image.open(texture).convert("RGBA"))
    assert pixels.shape[:2] == (1080, 1920), "Use the native menu render target"
    state = snapshot["options"]
    selected = state["selection_index"]
    targets = {t["name"]: t["rect"] for t in snapshot["targets"]["targets"]}
    rows = [r for r in state["boxes"][selected]["rows"] if r["visible"]]
    cases = []
    for row in rows:
        if row["type"] != 1 or not row["value"] or not row["clickable"]:
            continue
        rect = targets[f"box{selected}.row{row['index']}.toggle"]
        left, top, right, bottom = map(round, rect)
        inside = pixels[top+12:bottom-12, left+12:right-12, :3]
        ink = int(np.sum(np.max(inside, axis=2) < 100))
        cases.append({"case": "on-toggle-readable-"+str(row["index"]),
                      "passed": ink >= 20, "dark_ink_pixels": ink, "rect": rect})
    assert cases, "Show at least one ON toggle for the foreground contrast audit"
    ordered = sorted(targets[f"box{selected}.row{r['index']}"] for r in rows)
    gap_alpha = []
    for upper, lower in zip(ordered, ordered[1:]):
        y = round((upper[3]+lower[1])/2)
        # The continuous settings plate should keep scenery out of its dividers.
        gap_alpha.append(float(np.median(pixels[y, 500:1800, 3])/255))
    cases.append({"case": "continuous-settings-plate", "passed": bool(gap_alpha) and min(gap_alpha) >= .95,
                  "divider_alpha": gap_alpha})
    transparent = int(pixels[0, 0, 3]) == 0
    cases.append({"case": "scene-visible-outside-panels", "passed": transparent})
    return {"passed": all(c["passed"] for c in cases), "cases": cases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--texture", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.texture, json.loads(args.snapshot.read_text()))
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
