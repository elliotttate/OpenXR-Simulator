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

`showroom` means the disposable profile already has an owned, valid selected car.
`career` means it has the required progression for the selected class. `network`
means its existing private online setup is valid. The runner does not buy/unlock
cars, create/join lobbies, save/publish liveries, or start races. Livery navigation
requires a clean document and only uses view/browse/tab/cancel plus real ray
clicks to open the keyboard and browser. Edit-only states, gameplay/results,
network-room states and progression-dependent screens retain explicit pending
entries until their fixtures are supplied. Wreck Run remains deferred.

Screenshots must have a fresh compositor frame and a simulator session matching
the attached PID. A desktop process cannot reuse stale VR captures. Native pipe
reads have a bounded deadline, including a partial response from a paused or
unfocused game. A timeout writes the partial report and stops further staging.

Run offline tests with:

```powershell
python -m unittest discover -s tests -p test_flatout_ui_atlas.py -v
```
