#include "projection_timing.h"

// These checks must also run in the Release configuration used for the DLL.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <thread>

namespace {

using Tracker = perf::ProjectionTimingTracker;

bool Near(double actual, double expected, double tolerance) {
    return std::abs(actual - expected) <= tolerance;
}

void ConcurrentTitleSnapshots() {
    Tracker tracker;
    std::atomic<bool> start{false};
    std::atomic<int> ready{0};
    std::atomic<int> failures{0};
    std::atomic<int> populated{0};
    constexpr auto frame = std::chrono::milliseconds(10);
    const auto epoch = Tracker::Clock::time_point{};
    tracker.Observe(true, epoch);
    tracker.Observe(true, epoch + frame);
    // Publish time separately: Snapshot may see an earlier time than the newest
    // sample, just as a caller can be descheduled between reading its clock and
    // entering Snapshot. The copied fields must nevertheless remain consistent.
    std::atomic<int64_t> nowMs{10};
    auto reader = [&] {
        ++ready;
        while (!start.load()) std::this_thread::yield();
        for (int i = 0; i < 20000; ++i) {
            const auto now = epoch + std::chrono::milliseconds(nowMs.load());
            const auto s = tracker.Snapshot(now);
            tracker.ShouldRefreshTitle(now);
            if (s.sampleCount > 240) ++failures;
            if (s.active && s.sampleCount) {
                ++populated;
                if (!Near(s.fps, 100.0, 0.0001) ||
                    !Near(s.latestFrameMs, 10.0, 0.0001) ||
                    !Near(s.p50FrameMs, 10.0, 0.0001) ||
                    !Near(s.p95FrameMs, 10.0, 0.0001)) ++failures;
            }
        }
    };
    std::thread readers[] = {std::thread(reader), std::thread(reader), std::thread(reader)};
    while (ready.load() != 3) std::this_thread::yield();
    start.store(true);
    int64_t ms = 10;
    for (int i = 0; i < 60000; ++i) {
        if (i % 500 == 499) {
            ms += 600;
            tracker.Observe(false, epoch + std::chrono::milliseconds(ms));
            tracker.Observe(true, epoch + std::chrono::milliseconds(ms));
        }
        ms += 10;
        tracker.Observe(true, epoch + std::chrono::milliseconds(ms));
        nowMs.store(ms);
        tracker.ShouldRefreshTitle(epoch + std::chrono::milliseconds(ms));
    }
    for (auto& thread : readers) thread.join();
    assert(populated.load() > 0);
    assert(failures.load() == 0);
    assert(Near(tracker.Snapshot(epoch + std::chrono::milliseconds(ms)).fps, 100.0, 0.0001));
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

    ConcurrentTitleSnapshots();
    std::cout << "projection timing tests passed, including concurrent title snapshots\n";
    return 0;
}
