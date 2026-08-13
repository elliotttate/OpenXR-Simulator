#pragma once

#include <cstdint>

// Continuous diagnostics over the exact CPU DIB painted by the D3D12 simulator
// preview. This is intentionally independent from application/game telemetry: a
// runtime presentation bug can flicker while the application's swapchain images
// remain perfectly stable.
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

// `bgra` is a tightly packed, top-down preview image. `generation` changes only
// when the simulator has completed a new GPU readback, so the 90 Hz xrEndFrame
// loop does not mistake the intentional 30 Hz preview throttle for frozen frames.
void ObservePreview(const uint8_t* bgra, uint32_t width, uint32_t height,
                    uint64_t generation, uint64_t frame);

// Records the actual WM_PAINT result. A stable off-screen DIB is not sufficient
// evidence when the window itself intermittently takes the fallback-black path.
void ObservePaint(uint64_t generation, bool paintedPreview);

// UI-only diagnostics. The supplied rectangles identify the projected quad in
// each eye, allowing temporal analysis to ignore the independently moving world.
void ObserveUi(const uint8_t* bgra, uint32_t width, uint32_t height,
               uint64_t generation, uint64_t frame, const UiFrameInfo& info);

} // namespace flicker
