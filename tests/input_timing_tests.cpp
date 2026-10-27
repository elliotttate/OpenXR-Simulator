#include "input_timing.h"
#include <cmath>
#include <iostream>
#include <limits>

int main() {
    int failures = 0, checks = 0;
    auto check = [&](bool passed) { ++checks; if (!passed) { ++failures; std::cerr << "failed check " << checks << "\n"; } };
    check(perf::InputDeltaSeconds(100, 0, 1000) == 0.0f);
    check(perf::InputDeltaSeconds(100, 101, 1000) == 0.0f);
    check(perf::InputDeltaSeconds(100, 100, 1000) == 0.0f);
    check(perf::InputDeltaSeconds(100, 1, 0) == 0.0f);
    check(std::abs(perf::InputDeltaSeconds(2001, 1, 1000) - 0.1f) < 1e-6f);
    // Two seconds of held movement travels six metres at 3 m/s at each rate.
    for (int hz : {45, 60, 90, 144, 180, 360}) {
        constexpr std::int64_t frequency = 1296000;
        std::int64_t tick = frequency;
        double distance = 0.0;
        for (int frame = 0; frame < hz * 2; ++frame) {
            const auto next = tick + frequency / hz;
            distance += 3.0 * perf::InputDeltaSeconds(next, tick, frequency);
            tick = next;
        }
        check(std::abs(distance - 6.0) < 1e-5);
    }
    using Pose = std::array<float, 5>;
    const Pose pose{1.0f, 2.0f, 3.0f, 0.2f, -0.3f};
    auto zero = [](const Pose& velocity) { for (float v : velocity) if (v != 0.0f) return false; return true; };
    perf::InputVelocityHistory<5> history;
    check(zero(history.Sample(pose, 0.0))); // First zero-dt call seeds the nonzero pose.
    check(zero(history.Sample(pose, 1.0 / 180.0)));
    for (int hz : {45, 60, 90, 144, 180, 360}) {
        history.Reset();
        check(zero(history.Sample(pose, 1.0 / hz))); // Also safe on a nonzero first dt.
        const Pose expected{3.0f, -2.0f, 1.0f, 0.5f, -0.25f};
        Pose moved = pose;
        for (unsigned i = 0; i < moved.size(); ++i) moved[i] += expected[i] / hz;
        const auto measured = history.Sample(moved, 1.0 / hz);
        for (unsigned i = 0; i < moved.size(); ++i) check(std::abs(measured[i] - expected[i]) < 0.001f);
    }
    const Pose discontinuity{101.0f, 202.0f, 303.0f, 1.2f, -2.3f};
    for (int lifecycle = 0; lifecycle < 3; ++lifecycle) { // Focus loss, session end, new session.
        history.Reset();
        check(zero(history.Sample(discontinuity, 1.0 / 360.0)));
        check(zero(history.Sample(discontinuity, 1.0 / 360.0)));
    }
    for (double invalid : {0.0, -0.1, 0.101, 2.0}) {
        history.Sample(pose, 1.0 / 90.0);
        check(zero(history.Sample(discontinuity, invalid)));
        check(zero(history.Sample(discontinuity, 1.0 / 90.0)));
    }
    check(perf::InputSampleSeconds(2001, 1, 1000) == 2.0); // Velocity sees true time, not movement clamp.
    Pose invalidPose = pose; invalidPose[0] = std::numeric_limits<float>::quiet_NaN();
    check(zero(history.Sample(invalidPose, 1.0 / 90.0)));
    check(zero(history.Sample(pose, 1.0 / 90.0)));
    std::cout << "input timing: " << checks << " checks, " << failures << " failures\n";
    return failures ? 1 : 0;
}
