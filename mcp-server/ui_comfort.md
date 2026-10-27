# Spatial UI visibility and comfort review

`get_ui_comfort_guidelines` returns the review profile, its sources and scope.
`analyze_ui_visibility` measures an existing `capture_id` from the same MCP
session. It reads the immutable screenshot geometry and cached pixels. It sends
no screenshot request, pointer command, head movement or runtime configuration.
There is no runtime DLL cost; computation happens only when the tool is called.

```text
capture_screenshot(eye="both", max_width=2048)
get_ui_comfort_guidelines()
analyze_ui_visibility(capture_id="...", include_overlay=true)
```

With no `targets`, every captured composition quad is reviewed as a panel.
That covers its full canvas, including transparent padding; it does not identify
the populated UI from pixel alpha. Pass actual control rectangles for a layout audit.
World-space meshes are not inferred from pixels. Supply their measured surface
in **OpenXR local space**, using metres and a unit `[x,y,z,w]` quaternion:

```json
{
  "capture_id": "...",
  "targets": [
    {
      "name": "settings.back",
      "role": "control",
      "surface": {
        "pose": {"position": [0, 1.5, -2], "orientation": [0, 0, 0, 1]},
        "size": [2.4, 1.35]
      },
      "rect": [1520, 940, 1820, 1030],
      "canvas_size": [1920, 1080]
    }
  ],
  "reference_mode": "level_head",
  "include_overlay": true
}
```

The pose is the **full-canvas centre**; local +X is right, +Y is up, and +Z
faces the viewer. A rectangle uses top-left canvas coordinates. `canvas_size`
defaults to `[1,1]`, allowing normalized rectangles. Omitted rectangles cover
the full surface. Use `surface_id` instead of `surface` for a composition quad
from the capture. For split panels, supply the correct plane for each control;
do not pretend two angled planes share one centre mapping.

Scene surfaces use the app's `projection_eyes`; composition quad IDs use the
compositor `eyes`. Neither camera is guessed or silently substituted. Both eye
geometries are required even when the returned screenshot is cropped to one eye.

## Measured coverage and advisory placement are separate

Each target returns:

- `fully_in_both_submitted_eye_frusta`, plus each eye's `full`, `partial`,
  `outside` or `not_submitted` status and its clipped projected polygon.
- `eye_in_returned_image`, so a cropped-away eye is not confused with geometry
  outside that eye's submitted frustum. Odd stereo widths, per-eye asymmetric
  FOVs, image UV rectangles and source crop/scale are used directly.
- Selected-reference centre yaw/elevation and angular bounds, plus actual
  captured-head centre angles, centre distance and angular size between opposite
  edge midpoints. A saved reference position does not change the actual viewing
  distance or angular-size advisories. Elevation bounds include edge
  stationary points; testing only the four corners can underestimate them.
- `placement_advisories` and `preferred_gaze_zone`. A control can fit in both
  frusta and still sit below the preferred reading region or be too small.

The default comfort reference is `level_head`: captured head position and
heading, with pitch/roll removed relative to **XR local +Y up**. It does not
infer the game's floor or compensate for an artificially tilted game camera.
`captured_head` answers placement relative to the current head direction.

For a menu that has already made the person turn, pass its saved opening
`reference_pose: {position:[...], orientation:[...]}`. That changes only the
advisory reference; actual submitted per-eye frustum tests remain tied to the
captured cameras. Using the current head alone cannot prove where a person's
original neutral facing direction was.

The PNG is explicitly marked as diagnostic. Cyan outlines the captured eye
viewport; green is the preferred review region; blue is the resting-elevation
band; amber is the outer review envelope. Target outlines show clipped versus
full frustum coverage and advisories; numbered tags refer to JSON target order.
Angular guide lines are orientation-only,
without depth parallax; each target's coverage and measurements use actual 3D
depth. The original JPEG and its capture identity are untouched. Save original,
PNG and JSON separately for comparison.

Old captures can be inspected: `capture_age_seconds`, frame and session are
reported. This is a review of that saved frame, not a claim about the current
game. Take another screenshot when the head, layout or scene changes.

## Default profile and provenance

All thresholds are configurable with a `profile` object; only supplied fields
override defaults. `name` should describe project-specific choices.

| Field | Default | Meaning |
|---|---|---|
| `preferred_yaw_deg` | `[-30,30]` | Project review policy for central frequent UI; not a published universal human limit |
| `preferred_elevation_deg` | `[-35,0]` | Microsoft preferred content-region guidance |
| `resting_elevation_deg` | `[-20,-10]` | Microsoft resting-gaze guidance |
| `review_yaw_deg` | `[-45,45]` | Microsoft horizontal review envelope |
| `review_elevation_deg` | `[-60,10]` | Microsoft vertical review envelope |
| `distance_m` | `[0.8,3]` | Meta indirect hand/ray UI guidance, advisory for this controller-ray project |
| `min_control_angle_deg` | `3` | Conservative end of Meta's roughly 2.5–3° hand/ray collider guidance; applies to `role: control` |

[Microsoft's comfort guidance](https://learn.microsoft.com/en-us/windows/mixed-reality/design/comfort)
describes the gaze regions; it does not establish a guaranteed comfortable area
for every user. The HoloLens-specific focal-distance values on that page are not
used as generic immersive-headset limits here.

[Meta's hand UI guidance](https://developers.meta.com/vr/design/hands-ui-best-practices/)
informs the distance and angular-size advisories. Those recommendations address
hand/indirect interaction and are not a controller-specific certification.

[Meta distinguishes render extent from visible lens extent](https://developers.meta.com/vr/essentials/field-of-view/).
This tool therefore uses captured per-eye frusta instead of a hard-coded headset
FOV, while avoiding a claim that every rendered pixel reaches the user's eye.

Intentionally peripheral controls, direct-touch interfaces, large preview panels
and accessibility alternatives need their own review policy. An advisory does
not automatically mean the layout is wrong. No result certifies occlusion,
backface/material rendering, lens masks, text readability, eye tracking, visual
acuity, animation or comfort for a particular person. Validate physical devices
and seated/standing users.

## Tests

```powershell
python -m unittest discover -s tests -p test_ui_comfort.py -v
```

Tests cover independent asymmetric/odd-width eye geometry, source crops and UVs,
near-plane clipping, behind-reference layouts, edge extrema, saved neutral pose,
configurable guidance, original-pixel preservation, MCP registration, and the
absence of runtime/file-command side effects.
