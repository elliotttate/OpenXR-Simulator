#include "projection_timing.h"

#include <cassert>
#include <cmath>
#include <iostream>

namespace {

using Tracker = perf::ProjectionTimingTracker;

bool Near(double actual, double expected, double tolerance) {
    return std::abs(actual - expected) <= tolerance;
}

} // namespace

int main() {
    Tracker tracker;
    const auto start = Tracker::Clock::time_point{};

    // The first stereo projection warms up the interval measurement. Idle or
    // quad-only calls never manufacture an FPS sample.
    tracker.Observe(false, start);
    assert(!tracker.Snapshot(start).active);
    tracker.Observe(true, start);
    assert(tracker.Snapshot(start).sampleCount == 0);

    // A stable 90 Hz submission stream reports 90 FPS and 11.11 ms percentiles.
    constexpr auto frame90 = std::chrono::nanoseconds(11111111);
    auto now = start;
    for (int i = 0; i < 240; ++i) {
        now += frame90;
        tracker.Observe(true, now);
    }
    auto stats = tracker.Snapshot(now);
    assert(stats.active);
    assert(stats.sampleCount == 240);
    assert(Near(stats.fps, 90.0, 0.01));
    assert(Near(stats.latestFrameMs, 11.111111, 0.001));
    assert(Near(stats.p50FrameMs, 11.111111, 0.001));
    assert(Near(stats.p95FrameMs, 11.111111, 0.001));

    // The ring is bounded and tracks the new cadence rather than process-lifetime
    // average performance.
    constexpr auto frame72 = std::chrono::nanoseconds(13888889);
    for (int i = 0; i < 240; ++i) {
        now += frame72;
        tracker.Observe(true, now);
    }
    stats = tracker.Snapshot(now);
    assert(stats.sampleCount == 240);
    assert(Near(stats.fps, 72.0, 0.01));
    assert(Near(stats.p50FrameMs, 13.888889, 0.001));
    assert(Near(stats.p95FrameMs, 13.888889, 0.001));

    // A projection gap returns the title to its waiting state and clears stale
    // menu/previous-session data before the next warm-up.
    now += std::chrono::milliseconds(600);
    tracker.Observe(false, now);
    stats = tracker.Snapshot(now);
    assert(!stats.active);
    assert(stats.sampleCount == 0);

    std::cout << "projection timing tests passed\n";
    return 0;
}
