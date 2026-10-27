# FlatOut UI atlas

This runner creates an HTML contact sheet, native screenshots, per-screen JSON,
all exposed control bounds/labels, and source-to-capture coverage. It uses the
game's existing menu APIs to stage the real UI, with the compositor supplying
the screenshots. It is a fast presentation survey; the screenshot/hover/click
E2E tests remain the checks for ray interaction correctness.

Use the simulator's Python environment (`mcp-server/.venv/Scripts/python.exe`
on Windows). The usual `mcp`, NumPy and Pillow dependencies are already used by
the UI tests. All paths below are examples relative to the current project;
pass your active Fox source and the launch manifest from that same build.

```powershell
# Discover source screens/templates and write a report without touching a game.
python tools/flatout_ui_atlas.py --fox-root <fox-root> --catalog-only --output <new-output-dir>

# Read-only attachment: one fresh screenshot/report, no navigation or XR input.
python tools/flatout_ui_atlas.py --fox-root <fox-root> --launch <launch.json> --current --output <new-output-dir>

# Attach through another automation's approved read-only queue; no native pipe takeover.
python tools/flatout_ui_atlas.py --fox-root <fox-root> --launch <owner-launch.json> --pid <vr-pid> --data-dir <owner-sim-ipc-dir> --owner-queue <owner-queue-dir> --current --output <new-output-dir>

# Fast sweep of safely reachable front-end screens and dismissible templates.
python tools/flatout_ui_atlas.py --fox-root <fox-root> --launch <launch.json> --output <new-output-dir>

# Include prepared owned-car and Career fixtures; inspect every hover/selection.
python tools/flatout_ui_atlas.py --fox-root <fox-root> --launch <launch.json> --fixture showroom --fixture career --items all --hover all --output <new-output-dir>

# Short capture pass of particular screens, leaving template previews for later.
python tools/flatout_ui_atlas.py --fox-root <fox-root> --launch <launch.json> --only main options quick --no-dialog-templates --output <new-output-dir>

# Recheck livery overlays/clean close without recapturing a verified full gallery.
python tools/flatout_ui_atlas.py --fox-root <fox-root> --launch <launch.json> --fixture showroom --only livery --livery-overlays-only --no-dialog-templates --output <new-output-dir>

# Correct report inventory from saved native evidence without recapturing.
python tools/flatout_ui_atlas.py --reprocess <old-report-dir> --output <new-derived-dir>
```

Open `index.html` in the resulting directory. Search screens and control names,
filter by group/state, or enable projected target bounds in both eyes. Each
screen links to the original image and full metadata. `report.json` retains
coverage, source locations, errors, timing, build hashes and capture identities.
Every source enum/dialog template appears in its coverage mapping even if no
recipe could reach it. Existing output directories are never overwritten.
While a native modal is open, spatial menu items behind it are listed as
non-interactive background controls and excluded from exposed-control counts.
The centered livery keyboard and Workshop tasks also exclude the tool-column
widgets that the native renderer omits while those tasks are open. Their raw
records remain in metadata and the background inventory, but do not receive
click-target overlays or comfort warnings. This uses the native captured-popup
window names; unknown or absent popup metadata does not imply hidden controls.
Real game message boxes retain their native targets when Livery is underneath.
Reprocessing also removes hidden-control overlays. If that changes the inventory
of an older comfort review, its assets are preserved and labelled as needing
analysis with the corrected controls; the old summary is not presented as current.
`--reprocess` preserves source report, image and raw metadata files, copies the
native evidence unchanged, and labels its corrected inventory as derived, with
the original capture identity and SHA-256 provenance. It accesses no runtime.

The launch manifest must identify an isolated test slot. Without it, supply
`--pid`, `--data-dir` and `--isolated-profile`. Do not run a navigating sweep
during human testing or while another automation owns the process. Start at
Main Menu and resolve any existing real modal first. The runner stops on unknown
real confirmations instead of approving them. Only diagnostic dialog templates
that this run creates may be automatically dismissed. Template previews use
explicit diagnostic text and do not certify the original localized flow.

Default mode captures unique pages, category/scroll layouts, Quick Play modes,
available front-end screens and dialog templates. Carousel pages are sampled;
`--items all` captures each selection. Every visible target is inventoried from
the same screenshot; `--hover sample` or `--hover all` expands the slower hover
variants. Equivalent stable UI states share one native image with a dedup reason.
Livery's left and right meshes use their individual native affine canvas planes.
A navigating livery sweep captures an acknowledged glance toward each panel,
keeps the eye position fixed, and restores the original head pose afterward.
Centered keyboards/store overlays get their own centered view. The two glances
have separate capture identities so deduplication cannot hide a peripheral panel.
Projected bounds use all four corners of the control on its selected plane and
are clipped to the correct eye viewport. Metadata lists controls outside each view.
`--current` remains a single snapshot of the existing view; it never glances,
clicks, restores a pose, or otherwise sends input.
Navigating runs save the original attachment head pose and keep its tracked
position. Before ordinary staged routes and menu callbacks, they acknowledge
zero yaw/pitch/roll. This also happens before Livery opens, preserving its native
opening head-reference rule; per-plane livery glances remain independent.
Ordinary images and ray sources require matching actual and submitted stereo
poses at that neutral framing. A carried-over livery angle cannot become an
accepted ordinary-menu image. Framing participates in deduplication, and reports
record setup checks and original-pose restoration on success or failure. If
restoration cannot be acknowledged, the report retains that error without
masking an earlier case failure. Initial unknown modals are captured as-is and
never trigger framing input. Neutral framing does not certify that every
authored control fits the eye viewport; offscreen-control metadata still applies.
Head commands are queue deliveries, not proof that the view moved. The atlas
allows at most three deliveries of the same absolute pose within eight seconds,
records attempts and the last actual native pose, and keeps the strict pose
tolerance. Before a livery image is stored or used for a ray, both the actual
livery frame and submitted stereo projection geometry must agree with that
acknowledged pose. A stale image blocks the operation. Customization is identified
by its recipe/source identity; its shared “SKINS” title does not make it a livery
editor. An attach-only unknown “SKINS” view uses the exact native read-only
not-livery response to distinguish the two, without hiding other errors.
Successful click/hover delivery retains a controller lease in the simulator.
The atlas releases hands it acquired before native menu callbacks or head-pose
changes, after saving hover variants, and before restoring a livery glance.
Releases must be acknowledged and are recorded; unrelated hands are untouched.
Both MCP command publishers share an atomic JSON writer with unique owned
temporary files and a half-second retry bound for Windows replacement sharing
or access denials. Hard errors and invalid JSON payloads propagate. Publishing
still does not prove consumption; the atlas preserves exact transport errors
and requires the independent native-pose and submitted-geometry checks above.

`showroom` means the disposable profile already has an owned, valid selected car.
`career` means it has the required progression for the selected class. `network`
means its existing private online setup is valid. The runner does not buy/unlock
cars, create/join lobbies, save/publish liveries, or start races. Livery navigation
requires a clean document and only uses view/browse/tab/cancel plus real ray
clicks to open the keyboard and browser. Edit-only states, gameplay/results,
network-room states and progression-dependent screens retain explicit pending
entries until their fixtures are supplied. Wreck Run remains deferred.

Career and showroom recipes always use the normal Main → Career → Garage menu
callbacks. These establish the game mode and selected vehicle/material/driver;
opening the same screen directly with `menu.go_to` omits that setup. Even old or
custom recipes with `goto: livery` use the normal prelude automatically. The
runner verifies Career mode, a visible owned-car recap and the expected Garage
layout, then waits for two stable observations without loading indicators. It
only surveys the livery once its real presentation reports ready, with nonzero
bindings, projection primitives and exposed VR controls. Declaring a fixture
does not bypass these checks. Career/showroom routes infer the `showroom`
requirement; all online recipes infer `network` even if a custom manifest omits it.

`--fixture-timeout` bounds each readiness wait (20 seconds by default). Missing
owned cars, unexpected entry dialogs, active cups on a class-selection route,
uninitialized presentations and unavailable native pipes stop the sweep in
place and write a partial report. The runner sends no confirmation or recovery
navigation after these failures. It never continues/cancels an existing cup to
reach a class screen. Party staging uses its normal Multiplayer callback to set
the Party game mode, without activating Online's match-creation callback.
The Party driver recipe resets through its owning menu, verifies an empty
playlist, chooses an actually visible stunt on whichever page the picker
retained, verifies that one-stunt selection, and presses Start once to reach
drivers. It never retries Start or assumes `stunt.0` is on the current page.

`--livery-overlays-only` captures initial side panels, the large keyboard,
Workshop browser and the actual return to Garage. Its case scope and report
configuration explicitly omit tab/view/sticker-category galleries; a successful
focused pass does not certify those omitted states. Default livery scope keeps
the full galleries. Clean Cancel can close the editor immediately, so cleanup
observes the actual menu before reading editor state. Intermediate transitions
are bounded; an actual Garage observation proves success. Cancel is never
repeated as recovery, and changed documents are never discarded automatically.

Leaderboard recipes use currently exposed event targets, including a retained
later page. Each hover must establish the selected event and expose its enabled
Leaderboard action before it can be clicked. If no visible event supports it,
or its native service read fails, a screenshot and explanatory blocked case are
recorded. An unresolved real service modal stops navigation. These states remain
pending until their capable event/service fixture is available.

Screenshots must have a fresh compositor frame and a simulator session matching
the attached PID. A desktop process cannot reuse stale VR captures. Native pipe
reads have a bounded deadline, including a partial response from a paused or
unfocused game. A timeout writes the partial report and stops further staging.

Run offline tests with:

```powershell
python -m unittest discover -s tests -p test_flatout_ui_atlas.py -v
```

Use `--comfort-guides` to run the read-only MCP `analyze_ui_visibility` tool
after each screenshot. Each state links a separate diagnostic PNG and JSON;
the original native screenshot is unchanged. Native control rectangles use
their measured world plane or both-eye composition carrier. Split livery
controls retain their own full-canvas plane mapping. Spatial text with native
`rendered_bounds` is measured from its actual HUD layout rectangle; this does
not infer glyph ink or a ray collider. Spatial Back uses its native centre,
half-extents and scaled layout axes. Both are included in inventory and
both-eye overlays. Text without those bounds, out-of-canvas regions and
controls crossing physical planes remain explicitly
unmeasured rather than receiving a guessed depth.

Comfort mode takes a fresh frame for every requested state, including repeated
layouts: layout-only dedup cannot prove unchanged physical placement. The fast
default survey retains its existing dedup behavior. Saved opening orientation
is used for comfort placement even while capturing a livery glance; actual
per-eye coverage still uses the screenshot's submitted views. `--current`
continues to send no input and uses the captured level heading because no
earlier opening reference is available. An analysis error remains visible in
the report without replacing the native screenshot or claiming a valid review.

These are configurable placement advisories, not lens-visibility, occlusion,
readability or ergonomic certificates. Physical-device testing is still needed.
Livery is an intentional car-view workspace: looking sideways or somewhat down
can be appropriate when it preserves the car preview. Assess the accessibility
of each panel after turning, and retain the original neutral-facing measurements
to describe the required turn. A placement advisory alone is not a defect in
that workspace. Ordinary menus retain the forward-gaze review policy.
See `mcp-server/ui_comfort.md` for sources and profile applicability. Integration
contracts run with `python -m unittest discover -s tests -p test_flatout_atlas_comfort.py -v`.

Compare immutable reports with `tools/flatout_ui_consistency_audit.py --report
<older-report-directory> --report <newer-report-directory> --output <new-directory>`.
Later cases replace earlier ones. The report measures perpendicular plane depth,
eye-relative height and physical control dimensions, including Back. It preserves
native images and records source hashes. It does not infer visual styling or
certify states absent from the source reports.

Ordinary spatial navigation (Main, Multiplayer, Extras and Career hub) shares
a 3 m rest plane, the same type scale and a column centre 1 m below the eye height
captured at entry. The complete column includes Back. The retained plane is upright
and world-locked; head movement must not continuously recenter it. Reading screens
use their wider reading surface. Showroom/car-clearance layouts and livery are
separate task families, whose exceptions need an explicit reason.
`tests/flatout_spatial_placement_audit.py` checks saved native geometry against that
policy. `tests/flatout_spatial_consistency_e2e.py` checks seated/standing entry,
both-eye coverage, world locking and both-hand physical hover on a disposable
owned-car fixture. Wreck Run remains excluded.
