# Motion direction for the FlatOut VR menus

The menus are task surfaces over the live 3D game. Motion should explain focus,
confirmation and navigation without moving the world or making the player wait.
Keep the current typography, amber accents and bounded reading plates.

## Start with the shared dialog and action buttons

The focal interaction is the button physically approaching the player when
pointed at. The prototype and first native HUD-dialog integration supply that
real stereo movement. Supporting
dialog fades should establish and release modal attention at a fixed world pose.
Avoid full-screen fades for ordinary menu changes, staggered settings rows,
spring effects and simultaneous button scaling.

| State change | Proposed production behavior |
| --- | --- |
| Dialog opens | Fade its bounded presentation in over 100–140 ms at its final pose. |
| Dialog closes | Release input immediately; let only the outgoing visual fade for 70–90 ms. |
| Button hover | Approach 30 mm forward, about 135 ms to 95% of the destination. |
| Button press | Depress toward 6 mm above the source plane, about 75 ms to 95%. |
| Button exit | Return to the source plane in about 105 ms to 95%. |
| Options category changes | Keep the category rail and control geometry stable; a brief bounded content fade can explain the replacement. |
| Track or vehicle changes | Keep arrows available immediately; transition only the preview content, preserving current selection and action placement. |

These are candidate timings except where native validation is recorded below.
The prototype's exponential hover/press/return is implemented and tested. Fox's
shared VR dialog entrance now uses a separate 120 ms alpha animation; desktop
continues to use the existing 300 ms entrance.

## Use the existing runtime deliberately

`KTUMessageBoxContainer::Popup` selects `FadeInVRDialog` in VR and `FadeInHUD`
on desktop. Both use the existing widget animator, unmodified time and fixed
geometry. Input becomes ready immediately, independently of the fade.
A native screenshot → ray hover → held click test measured the previous entrance
at about 303 ms and rejected it against a 200 ms response budget. After rebuilding
the latest RC with the separate animation, the same test measured about 115 ms.
It observed intermediate alpha followed by full opacity, and a retry did not
reopen the dialog. Real ray Cancel dismissed it exactly once.

Credits uses a bounded reader with a 180 ms entrance and automatic ending fade.
The heading and clipped text inherit the reader's alpha. Its transport dock stays
separate from the scrolling text. Native tests cover both hands, immediate Pause,
held scrubbing, the complete roll and automatic return to Extras. RenderDoc
confirms transparent exterior/footer gaps and stencil rejection of text there.

Front-end Options now shares that head-height, world-locked HUD presentation.
The prior authored RTT anchor put its plane about 32 degrees below a neutral
standing gaze. Native red/green checks cover placement, all visible controls in
both eyes, translation/rotation stability and ray Back hover with either hand.
Its target collector owns the reading surface explicitly so rendering and ray
intersection use the same HUD pose. Showroom and other world-space menus retain
their own presentation paths.

`DrawRayActions` refreshes local colors every collection. A new fade must operate
at a shared presentation parent, or after those local colors are assigned.
Verify the composed title, illustration, border, plate and label together.
Animating only a new button's opacity can leave its label or original glyph
visible. `FOVRMenuRTT::UpdateFadeOverlay` covers the whole menu texture and is
unsuitable for fading only a dialog over a preserved spatial menu.

`FONavMenu::Close` and message-box `Hide` tear down immediately. A future exit
fade needs a short-lived, noninteractive visual snapshot/subtree. Do not delay
callbacks, saves, navigation or modal dismissal to keep an animation alive. A
dialog replaced during its exit must not retain a stale Yes/No target.

The soundtrack HUD's authored Flash timeline must disappear immediately when
a modal takes ownership. Stopping its timer left the last music frame, black
strip and gamepad SkipMusic prompt drawn beside Quit. Fox now hides that HUD's
own parent for the modal lifetime, then restores it; the spatial menu remains.

## Interruption, accessibility and cost

Retarget from the current alpha or physical depth. Repeated hover on the same
semantic action must not restart its entrance. A fast move to a neighbor can
use the two transient tiles already proposed in the integration design.
Destroyed screens invalidate their weak references and lose input immediately.

Keep the source hit rectangle stable, accept the same button's raised face,
and preserve dual-ray ownership and held-slider capture. Do not wait for an
animation to complete before accepting a click. The physical button surface
must replace its original rendering, including label and icon, to avoid ghosts.

Reduced motion should disable depth travel and pulses while retaining clear
hover/pressed color feedback and a brief opacity change. Fox's existing
Wreck Run preference has mode-specific semantics; broader VR motion control
needs an explicitly scoped setting rather than silently reusing it.

The first native dialog slice is behind `ExperimentalHudButtonDepth`. Its GPU
atlas renderer is created after graphics initialization and released before
graphics teardown. Native screenshot and ray tests cover both hands, a separate
BOTH-eye layer 30 mm forward, fixed source geometry, rapid focus changes and
return, and a raised-face edge that the base rectangle cannot receive. Dismissal
and same-ID retry produce one click and retire the tile immediately. The actual
production-header fixture covers the 6 mm press destination and timing; closing
dialogs deliberately do not wait at that destination before dispatching.

Quick Start, Party's player selector and stunt picker, and Statistics now use
the same head-height reading surface as Options. Native tests verify fixed world
placement during head motion and visible controls in both eyes with either hand.
Quick Start's complete mode/stepper/next-step sweep and Party's complete stunt
playlist sweep pass after that transport change. Statistics paging now lives in
the header, with 80 x 72 targets clear of the action dock; the previous Down
target overlapped Back by 64 x 11 pixels.

Reuse the GPU HUD renderer and bounded atlas tiles. Pose and alpha changes do
not justify per-frame texture readback, additional blur passes, or one OpenXR
layer per button. Measure the eventual production path on a headset before
claiming comfort or frame-budget results.

## Verification gates

Native evidence for this increment lives under the ignored
`build-ui-automation/flatout4-evidence/` directory:

- `rc17f-popup-native-motion-baseline`: actual spatial Quit hover/click,
  monotonic entrance alpha, popup spacing, exclusive targets and one ray Cancel.
- `rc17f-music-modal-native-red`: reproduces the lingering soundtrack controls.
- `rc17f-music-modal-native-green-right-final` and
  `rc17f-music-modal-native-green-left`: both hands, held trigger, same-ID retry,
  one dismissal, original menu retained and soundtrack HUD restored.
- `rc17f-popup-spacing-renderdoc/music-modal-red` and `music-modal-green`:
  RenderDoc pixel history confirms the outside black strip changes from alpha
  1 to 0, its prompt strip from about 0.35 to 0, and the dialog body stays opaque.
- `rc00c-popup-quick-motion-red` and `rc00c-popup-quick-motion-green-right`:
  measured native dialog entrance before/after on RC `00c4999b0` plus the UI
  overlay. The test derives duration from intermediate native alpha frames.
- `rc00c-credits-immediate-stop-green`, `rc00c-credits-controls-green-right`,
  `rc00c-credits-controls-green-left` and `rc00c-credits-complete-roll-green`:
  fresh native reader, transport, ending and both-eye visibility checks.
- `rc17f-popup-spacing-renderdoc/rc00c-credits-reader-final`:
  the captured RC `00c4999b0` HUD has alpha 0 outside the plate and in the footer
  gap; pixel history records two stencil-rejected text fragments in that gap.

For production motion integration, add rapid open/close and replacement during
the entrance, cancel during appearance, screen destruction during hover,
disabled/loading actions, both eyes, both rays, reduced motion, fixed geometry,
raised-edge clicks, and RenderDoc checks on both quad-layer and world-space paths.
