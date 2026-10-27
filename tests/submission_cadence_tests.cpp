#include "submission_cadence.h"
#include <cmath>
#include <cstdio>
#include <limits>
#include <initializer_list>

static int checks = 0, failures = 0;
static void Check(bool value, const char* name) {
    ++checks;
    if (!value) { ++failures; std::fprintf(stderr, "FAIL: %s\n", name); }
}
int main() {
    perf::SubmissionCadence c;
    Check(c.Snapshot().stereoSubmissions == 0 && !c.Snapshot().intervalValid, "empty");
    c.Observe(false, 10);
    Check(c.Snapshot().stereoSubmissions == 0, "nonprojection not counted");
    c.Observe(true, 1000000000);
    auto a = c.Snapshot();
    Check(a.stereoSubmissions == 1 && a.segment == 1 && !a.intervalValid, "seed");
    c.Observe(true, 1005000000);
    auto b = c.Snapshot();
    Check(b.intervalValid && b.frameIntervalMs == 5.0 && b.segment == a.segment, "200Hz interval");
    Check((b.stereoSubmissions-a.stereoSubmissions)*1e9/(b.lastSubmissionMonotonicNs-a.lastSubmissionMonotonicNs) == 200.0, "difference cadence");
    c.Observe(false, 1006000000);
    Check(!c.Snapshot().lastFrameHadStereo && !c.Snapshot().intervalValid && c.Snapshot().stereoSubmissions == 2, "missing stereo invalidates continuity");
    c.Observe(true, 1007000000);
    Check(c.Snapshot().segment == 2 && !c.Snapshot().intervalValid, "new segment after missing stereo");
    c.Observe(true, 1007000000);
    Check(c.Snapshot().segment == 3 && !c.Snapshot().intervalValid, "duplicate time");
    c.Observe(true, 1006000000);
    Check(c.Snapshot().segment == 4 && !c.Snapshot().intervalValid, "backward time");
    c.Observe(true, 1506000000);
    Check(c.Snapshot().intervalValid && c.Snapshot().frameIntervalMs == 500.0, "boundary included");
    c.Observe(true, 2006000001);
    Check(c.Snapshot().segment == 5 && !c.Snapshot().intervalValid, "long gap");
    c.Observe(true, -1);
    Check(!c.Snapshot().intervalValid, "negative timestamp");
    c.Observe(true, (std::numeric_limits<int64_t>::max)()-1);
    Check(!c.Snapshot().intervalValid, "reseed after invalid timestamp avoids overflow");
    c.Observe(true, (std::numeric_limits<int64_t>::max)());
    Check(c.Snapshot().intervalValid, "near maximum positive timestamp");
    for (int hz : {45,60,90,144,180,360}) {
        perf::SubmissionCadence rate;
        const int64_t step = 1000000000LL / hz;
        for (int i=0; i<=100; ++i) rate.Observe(true, step*i);
        auto s=rate.Snapshot();
        Check(s.stereoSubmissions == 101 && s.segment == 1 && s.intervalValid, "rate count and continuity");
        Check(std::abs(100.0*1e9/s.lastSubmissionMonotonicNs-hz)<0.001, "rate measurement");
    }
    std::printf("%d checks, %d failures\n",checks,failures);
    return failures ? 1 : 0;
}
