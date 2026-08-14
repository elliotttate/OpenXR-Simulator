#pragma once

#include <cstdint>

// Continuous diagnostics over the exact CPU DIB painted by the D3D12 simulator
// preview. This is intentionally independent from application/game telemetry: a
// runtime presentation bug can flicker while the application's swapchain images
// remain perfectly stable.
//
// Threading: every Observe* call runs its sampling and classification inline on
// the caller (the app's xrEndFrame thread), but all disk output — status JSON,
// incident BMP packets, manual capture-request polling — happens on a private
// worker thread. Nothing here blocks the frame loop on storage.
namespace flicker {

struct UiFrameInfo {
    uint32_t quadLayers = 0;
    bool projectionRefreshed = false;
    bool freshReadback = false;
    bool cachedPixelsUsed = false;
    bool cacheValid = false;
    bool composed = false;
    int32_t rects[2][4] = {};
    float sourceAlphaCoverage = 0.0f;
};

void ObserveSubmission(uint64_t frame, uint32_t projectionLayers, uint32_t totalLayers);

// `bgra` is a top-down preview image whose rows are `pitchBytes` apart (0 means
// tightly packed). D3D12 readback rows are 256-byte aligned, and taking the
// pitch here is what lets the caller hand the mapped readback over directly
// instead of repacking the whole frame first. `generation` changes only when
// the simulator has completed a new GPU readback, so the 90 Hz xrEndFrame loop
// does not mistake the intentional preview rate cap for frozen frames.
void ObservePreview(const uint8_t* bgra, uint32_t width, uint32_t height,
                    uint32_t pitchBytes, uint64_t generation, uint64_t frame);

// Records the actual WM_PAINT result. A stable off-screen DIB is not sufficient
// evidence when the window itself intermittently takes the fallback-black path.
void ObservePaint(uint64_t generation, bool paintedPreview);

// UI-only diagnostics. The supplied rectangles identify the projected quad in
// each eye, allowing temporal analysis to ignore the independently moving world.
// `pitchBytes` as in ObservePreview.
void ObserveUi(const uint8_t* bgra, uint32_t width, uint32_t height,
               uint32_t pitchBytes, uint64_t generation, uint64_t frame,
               const UiFrameInfo& info);

} // namespace flicker
