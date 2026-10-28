#include "frame_analysis.h"

// These checks must also run in the Release configuration used for the DLL.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <random>
#include <vector>

namespace {

using namespace perf::analysis;
using namespace perf::timeline;

constexpr uint64_t kQpc = 10000000;    // 10 MHz, as Windows QPC runs
constexpr uint64_t kTsc = 3000000000;  // 3 GHz thread-cycle counter
constexpr uint64_t kOrigin = 5000000;

uint64_t Ticks(double ms) { return uint64_t(std::llround(ms * double(kQpc) / 1000.0)); }
uint64_t Cycles(double ms) { return uint64_t(std::llround(ms * double(kTsc) / 1000.0)); }
bool Near(double actual, double expected, double tolerance = 0.002) {
    return std::abs(actual - expected) <= tolerance;
}

// The simulator's xrWaitFrame pacing around an app whose whole loop runs on
// one thread: wait, render for `appMs` (on a CPU for `busyShare` of it), submit.
struct SingleThreadApp {
    double hz = 90.0;
    uint64_t now = kOrigin, nextTick = kOrigin, cycles = 0, index = 0;
    std::vector<FrameRecord> out;

    void Frame(double appMs, double busyShare = 1.0, double endMs = 0.2, double simWaitMs = 0.0) {
        const uint64_t period = Ticks(1000.0 / hz);
        FrameRecord r{};
        r.index = index++;
        r.frameId = r.index + 1;
        r.waitThreadId = r.endThreadId = 7;
        r.flags = kFrameWaitMatched | kFrameBeginSeen | kFrameStereoProjection;
        r.waitEnterQpc = now;
        r.waitEnterCycles = cycles;
        now += Ticks(simWaitMs);
        cycles += Cycles(simWaitMs);
        r.waitPaceQpc = now;
        r.waitTargetQpc = nextTick;
        if (now < nextTick) now = nextTick;  // blocked: no cycles
        r.waitExitQpc = r.beginQpc = now;
        r.waitExitCycles = cycles;
        nextTick += period;
        if (now > nextTick + period) {
            nextTick = now;
            r.flags |= kFramePacingRebased;
        }
        now += Ticks(appMs);
        cycles += Cycles(appMs * busyShare);
        r.endEnterQpc = now;
        r.endEnterCycles = cycles;
        now += Ticks(endMs);
        cycles += Cycles(endMs);
        r.endExitQpc = now;
        r.endExitCycles = cycles;
        out.push_back(r);
    }
};

// A pipelined engine: the game thread waits and simulates for `gameMs`, then
// hands the frame to the render thread, which renders for `renderMs` and
// submits while the game thread is already waiting for the next frame.
std::vector<FrameRecord> Pipelined(int frames, double gameMs, double renderMs) {
    const uint64_t period = Ticks(1000.0 / 90.0);
    std::vector<FrameRecord> out;
    uint64_t gameNow = kOrigin, nextTick = kOrigin, renderFree = kOrigin;
    uint64_t gameCycles = 0, renderCycles = 0;
    for (int k = 0; k < frames; ++k) {
        FrameRecord r{};
        r.index = uint64_t(k);
        r.frameId = r.index + 1;
        r.waitThreadId = 1;
        r.endThreadId = 2;
        r.flags = kFrameWaitMatched | kFrameBeginSeen | kFrameStereoProjection;
        r.waitEnterQpc = r.waitPaceQpc = gameNow;
        r.waitEnterCycles = gameCycles;
        r.waitTargetQpc = nextTick;
        const uint64_t exit = std::max(gameNow, nextTick);
        r.waitExitQpc = r.beginQpc = exit;
        r.waitExitCycles = gameCycles;
        nextTick += period;
        if (exit > nextTick + period) nextTick = exit;
        const uint64_t handoff = exit + Ticks(gameMs);
        gameCycles += Cycles(gameMs);
        const uint64_t renderStart = std::max(handoff, renderFree);
        renderCycles += Cycles(renderMs);
        r.endEnterQpc = renderStart + Ticks(renderMs);
        r.endEnterCycles = renderCycles;
        r.endExitQpc = r.endEnterQpc + Ticks(0.2);
        renderCycles += Cycles(0.2);
        r.endExitCycles = renderCycles;
        renderFree = r.endExitQpc;
        gameNow = renderStart;  // the game thread waits for the next frame once the handoff is taken
        out.push_back(r);
    }
    return out;
}

Analyzer Make(const std::vector<FrameRecord>& records, double hz = 90.0) {
    Analyzer a;
    a.Reset(kQpc, kOrigin);
    a.SetTscFrequency(kTsc);
    a.SetBudget(hz, 2.0);
    a.AddFrames(records);
    return a;
}

void SteadyFramesCarryTheirRealCost() {
    SingleThreadApp app;
    for (int i = 0; i < 100; ++i) app.Frame(5.0, 1.0, 0.2, 0.05);
    Analyzer a = Make(app.out);
    const auto& m = a.Metrics();
    assert(m[0].gap && m[0].cls == FrameClass::Gap);
    for (size_t i = 2; i < m.size(); ++i) {
        assert(Near(m[i].intervalMs, 1000.0 / 90.0));
        // The previous xrEndFrame, xrWaitFrame's own work and the app's 5 ms.
        assert(Near(m[i].costMs, 5.25));
        assert(Near(m[i].blockedMs, 1000.0 / 90.0 - 5.25));
        assert(Near(m[i].simulatorMs, 0.25));
        assert(Near(m[i].appSpanMs, 5.0));
        assert(Near(m[i].pacingErrorMs, 0.0));
        assert(Near(m[i].submitOnCpu, 1.0));
        assert(m[i].cls == FrameClass::Ok && m[i].missed == 0 && m[i].cause == Cause::None);
    }
    assert(a.LateFrames().empty());
    const RangeStats s = a.Stats(0, a.Size());
    assert(s.frames == 99 && s.gaps == 1);
    assert(Near(s.deliveredFps, 90.0, 0.01));
    assert(Near(s.costP99Ms, 5.25) && s.overBudget == 0 && s.hitches == 0);
}

void LateFramesAreClassifiedAndExplained() {
    SingleThreadApp app;
    for (int i = 0; i < 20; ++i) app.Frame(5.0);
    app.Frame(30.0, 1.0);      // 20: busy for 30 ms
    for (int i = 0; i < 20; ++i) app.Frame(5.0);
    app.Frame(15.0, 0.2);      // 41: mostly off the CPU (disk, lock, GPU sync)
    for (int i = 0; i < 20; ++i) app.Frame(5.0);
    app.Frame(11.0, 1.0, 0.2, 0.5);  // 62: 11.7 ms only because of the simulator's 0.7 ms
    for (int i = 0; i < 5; ++i) app.Frame(5.0);
    Analyzer a = Make(app.out);
    const auto& m = a.Metrics();

    assert(Near(m[20].costMs, 30.2) && m[20].cls == FrameClass::Hitch);
    assert(m[20].missed == 2 && m[20].cause == Cause::AppBusy);
    assert(Near(m[41].costMs, 15.2) && m[41].cls == FrameClass::OverBudget);
    assert(m[41].missed == 1 && m[41].cause == Cause::AppWaiting);
    assert(m[62].cls == FrameClass::OverBudget && m[62].cause == Cause::Simulator);
    // Pacing catches up straight after a late frame.
    assert(m[21].cls == FrameClass::Ok && m[42].cls == FrameClass::Ok);
    assert((a.LateFrames() == std::vector<uint32_t>{20, 41, 62}));

    const RangeStats s = a.Stats(0, a.Size());
    assert(s.overBudget == 3 && s.hitches == 1 && s.missedRefreshes == 4);
    assert(Near(s.costMaxMs, 30.2));

    // Re-scored against another headset: 72 Hz forgives the 11.7 ms frame,
    // 120 Hz makes the 5.2 ms frames tight but still on time.
    a.SetBudget(72.0, 2.0);
    assert(a.Metrics()[62].cls == FrameClass::Ok);
    assert(a.Metrics()[41].cls == FrameClass::OverBudget);
    assert(a.Metrics()[20].cls == FrameClass::Hitch && a.Metrics()[20].missed == 2);
    assert((a.LateFrames() == std::vector<uint32_t>{20, 41}));
    a.SetBudget(120.0, 2.0);
    assert(a.Metrics()[41].cls == FrameClass::OverBudget && a.Metrics()[41].missed == 1);
    assert(a.Metrics()[30].cls == FrameClass::Ok);
}

// Submissions jitter by the app's varying work even though pacing ticks are
// exact. That moves frames between intervals; it does not make them late.
void SubmissionJitterIsNotLateness() {
    SingleThreadApp app;
    for (int i = 0; i < 200; ++i) app.Frame(i % 2 ? 6.0 : 2.0);
    Analyzer a = Make(app.out);
    float maxInterval = 0.0f;
    for (size_t i = 2; i < a.Size(); ++i) {
        const FrameMetrics& f = a.Metrics()[i];
        maxInterval = std::max(maxInterval, f.intervalMs);
        assert(f.cls == FrameClass::Ok);
        assert(Near(f.costMs, i % 2 ? 6.2 : 2.2));
    }
    assert(maxInterval > 15.0f);
    assert(a.Stats(0, a.Size()).overBudget == 0);
}

void PipelinedEnginesAreMeasuredOnTheBusierThread() {
    // Render thread (5 ms) is busier than the game thread (4 ms), and it is the
    // one that submits. The game thread's pacing headroom alone would claim 4 ms.
    const auto records = Pipelined(300, 4.0, 5.0);
    Analyzer batch = Make(records);
    for (size_t i = 3; i + 2 < batch.Size(); ++i) {
        const FrameMetrics& f = batch.Metrics()[i];
        assert(Near(f.intervalMs, 1000.0 / 90.0));
        assert(Near(f.submitCpuMs, 5.0));
        assert(Near(f.costMs, 5.0));
        assert(f.cls == FrameClass::Ok);
    }

    // Records arriving one by one (as a live viewer polls) end up identical:
    // the wait for frame i+1 lands after frame i and is folded back in.
    Analyzer live;
    live.Reset(kQpc, kOrigin);
    live.SetTscFrequency(kTsc);
    live.SetBudget(90.0, 2.0);
    for (const FrameRecord& r : records) live.AddFrames({r});
    for (size_t i = 0; i < batch.Size(); ++i) {
        assert(live.Metrics()[i].costMs == batch.Metrics()[i].costMs);
        assert(live.Metrics()[i].blockedMs == batch.Metrics()[i].blockedMs);
        assert(live.Metrics()[i].cls == batch.Metrics()[i].cls);
    }

    // Render-bound: every frame is late. While the tick schedule still has slack
    // the game thread keeps blocking a little, but cost never drops below the
    // render thread's 13 ms; once the slack is gone it is the whole interval.
    Analyzer slow = Make(Pipelined(100, 4.0, 13.0));
    for (size_t i = 1; i < slow.Size(); ++i) {
        const FrameMetrics& f = slow.Metrics()[i];
        assert(f.costMs >= 13.0f - 0.002f && f.costMs <= 13.2f + 0.002f);
        assert(f.cls == FrameClass::OverBudget && f.missed == 1);
        if (i >= 4) assert(Near(f.costMs, 13.2) && f.blockedMs == 0.0f);
    }
}

void IdleSessionsAndLostRecordsAreGaps() {
    SingleThreadApp app;
    for (int i = 0; i < 10; ++i) app.Frame(5.0);
    app.now += Ticks(3000.0);  // a 3 s pause...
    for (int i = 0; i < 10; ++i) app.Frame(5.0);
    app.now += Ticks(3000.0);  // ...and another
    for (int i = 0; i < 10; ++i) app.Frame(5.0);

    Analyzer a;
    a.Reset(kQpc, kOrigin);
    a.SetTscFrequency(kTsc);
    a.SetBudget(90.0, 2.0);
    // The first pause spans a session state change (the app lost focus): idle, not a hitch.
    MarkerRecord focusLost{};
    focusLost.kind = uint32_t(MarkerKind::SessionState);
    focusLost.qpc = app.out[9].endExitQpc + Ticks(100.0);
    a.AddMarkers({focusLost});
    a.AddFrames(app.out);
    assert(a.Metrics()[10].gap && a.Metrics()[10].cls == FrameClass::Gap);
    assert(a.Metrics()[20].cls == FrameClass::Hitch && a.Metrics()[20].costMs > 2900.0f);

    // Records the viewer never saw (it fell a whole ring behind).
    std::vector<FrameRecord> withHole(app.out.begin(), app.out.begin() + 5);
    withHole.insert(withHole.end(), app.out.begin() + 7, app.out.begin() + 10);
    Analyzer b = Make(withHole);
    assert(b.Metrics()[5].gap && !b.Metrics()[6].gap);
}

void ColumnsKeepEverySpike() {
    SingleThreadApp app;
    std::mt19937 rng(7);
    std::uniform_real_distribution<double> work(1.0, 9.0);
    for (int i = 0; i < 20000; ++i) app.Frame(i == 12345 ? 40.0 : work(rng));
    Analyzer a = Make(app.out);

    for (XAxis axis : {XAxis::Time, XAxis::Frame}) {
        const double x0 = a.StartX(axis, 0), x1 = a.EndX(axis, a.Size() - 1) + 1e-9;
        for (int columns : {7, 64, 333, 1920, 50000}) {
            std::vector<Column> cols;
            a.Columns(axis, x0, x1, columns, cols);
            // Brute force over the same column boundaries.
            const double width = (x1 - x0) / columns;
            uint32_t total = 0;
            float spike = 0.0f;
            for (int c = 0; c < columns; ++c) {
                const double left = x0 + width * c, right = c + 1 == columns ? x1 : x0 + width * (c + 1);
                float best = 0.0f;
                uint32_t frames = 0;
                for (size_t i = 0; i < a.Size(); ++i) {
                    const double end = a.EndX(axis, i);
                    if (end < left || end >= right || a.Metrics()[i].gap) continue;
                    ++frames;
                    best = std::max(best, a.Metrics()[i].costMs);
                }
                assert(cols[size_t(c)].frames == frames);
                assert(cols[size_t(c)].maxCostMs == best);
                if (frames) assert(a.Metrics()[cols[size_t(c)].maxIndex].costMs == best);
                total += frames;
                spike = std::max(spike, cols[size_t(c)].maxCostMs);
            }
            assert(total == a.Size() - 1);
            assert(spike == a.Metrics()[12345].costMs);
        }
    }

    std::vector<uint32_t> wide;
    a.WideFrames(XAxis::Time, a.StartX(XAxis::Time, 0), a.EndX(XAxis::Time, a.Size() - 1), 0.030, wide);
    assert((wide == std::vector<uint32_t>{12345}));
}

void StatsMatchTheirDefinitions() {
    SingleThreadApp app;
    for (int i = 0; i < 1001; ++i) app.Frame(double(i % 10) + 1.0);
    Analyzer a = Make(app.out);
    const RangeStats s = a.Stats(1, a.Size());  // 1000 frames, costs 1.2 .. 10.2 ms
    assert(s.frames == 1000 && s.gaps == 0);
    std::vector<float> costs;
    for (size_t i = 1; i < a.Size(); ++i) costs.push_back(a.Metrics()[i].costMs);
    std::sort(costs.begin(), costs.end());
    assert(s.costP50Ms == costs[499] && s.costP90Ms == costs[899]);
    assert(s.costP99Ms == costs[989] && s.costP999Ms == costs[998] && s.costMaxMs == costs[999]);
    assert(Near(s.costMeanMs, 5.7, 0.01));
    // The slowest 1% are ten 10.2 ms frames.
    assert(Near(s.onePercentLowFps, 1000.0 / 10.2, 0.01));
    assert(Near(s.deliveredFps, 90.0, 0.01));

    std::vector<double> bins[3];
    a.Histogram(1, a.Size(), 1.0, 8, bins);
    double sum = 0.0;
    for (const auto& c : bins) for (double v : c) sum += v;
    assert(sum == 1000.0);
    assert(bins[0][7] == 400.0);  // 7.2, 8.2, 9.2 and 10.2 ms frames land in the last bin
    assert(bins[0][1] == 100.0 && bins[1][7] == 0.0 && bins[2][7] == 0.0);
}

// GPU records for frames, with the GPU busy from `startMs` after the frame's
// wait returned for `gpuMs`. Ticks are nanoseconds; QPC calibrated.
std::vector<GpuRecord> GpuFor(const std::vector<FrameRecord>& frames, double startMs, double gpuMs) {
    std::vector<GpuRecord> out;
    for (const FrameRecord& f : frames) {
        GpuRecord g{};
        g.frameId = f.frameId;
        g.tickFrequency = 1000000000ull;
        g.beginQpc = f.waitExitQpc + Ticks(startMs);
        g.endQpc = g.beginQpc + Ticks(gpuMs);
        g.beginTicks = g.beginQpc * 100;  // 10 MHz QPC -> ns
        g.endTicks = g.endQpc * 100;
        g.api = uint32_t(GpuApi::D3D12);
        g.flags = kGpuCalibrated;
        out.push_back(g);
    }
    return out;
}

void GpuBoundFramesBlameTheGpu() {
    // The CPU submits and then waits on its own fence (as the probe does): 15 ms
    // per frame, almost none of it on a CPU. The GPU is busy 14.4 ms of it and
    // finishes 0.4 ms after xrEndFrame, where its end marker was submitted.
    SingleThreadApp app;
    for (int i = 0; i < 60; ++i) app.Frame(15.0, 0.05);
    Analyzer a = Make(app.out);
    a.AddGpu(GpuFor(app.out, 1.0, 14.4));
    for (size_t i = 2; i < a.Size(); ++i) {
        const FrameMetrics& f = a.Metrics()[i];
        assert(Near(f.gpuMs, 14.4) && Near(f.costMs, 15.2) && Near(f.cpuMs, 15.2));
        assert(f.cls == FrameClass::OverBudget && f.cause == Cause::Gpu);
        assert(Near(f.gpuLatencyMs, 0.4));
        assert(!std::isnan(f.gpuStartSec) && f.gpuEndSec > f.gpuStartSec);
    }
    const RangeStats s = a.Stats(1, a.Size());
    assert(s.gpuFrames == 59 && s.gpuOverBudget == 59 && Near(s.gpuP99Ms, 14.4));

    // A CPU busy for longer than the GPU is the app's fault, not the GPU's.
    SingleThreadApp busy;
    for (int i = 0; i < 20; ++i) busy.Frame(20.0, 1.0);
    Analyzer b = Make(busy.out);
    b.AddGpu(GpuFor(busy.out, 0.5, 14.0));
    assert(b.Metrics()[5].cause == Cause::AppBusy && Near(b.Metrics()[5].costMs, 20.2));
}

void GpuTimeLeavesOutIdleAndOverlap() {
    // GPU done in 3 ms of each 11 ms frame: its time is 3 ms, not the interval.
    SingleThreadApp app;
    for (int i = 0; i < 30; ++i) app.Frame(5.0);
    Analyzer a = Make(app.out);
    a.AddGpu(GpuFor(app.out, 1.0, 3.0));
    for (size_t i = 1; i < a.Size(); ++i) {
        assert(Near(a.Metrics()[i].gpuMs, 3.0) && Near(a.Metrics()[i].costMs, 5.2));
        assert(a.Metrics()[i].cls == FrameClass::Ok);
    }

    // Deep pipelining: each frame's begin marker lands while the GPU is still on
    // the previous frame, so its time starts where that one ended.
    std::vector<GpuRecord> gpu = GpuFor(app.out, 1.0, 3.0);
    for (size_t i = 1; i < gpu.size(); ++i) {
        gpu[i].beginTicks = gpu[i - 1].endTicks - 1000000;  // 1 ms before the previous end
        gpu[i].endTicks = gpu[i - 1].endTicks + 9000000;    // 9 ms of work after it
    }
    Analyzer p = Make(app.out);
    p.AddGpu(gpu);
    for (size_t i = 2; i < p.Size(); ++i) assert(Near(p.Metrics()[i].gpuMs, 9.0));
}

void LateGpuResultsRescoreTheirFrames() {
    SingleThreadApp app;
    for (int i = 0; i < 40; ++i) app.Frame(15.0, 0.05);
    Analyzer a = Make(app.out);
    // Without GPU data the waiting CPU gets the blame.
    assert(a.Metrics()[10].cause == Cause::AppWaiting);
    const auto gpu = GpuFor(app.out, 0.5, 14.4);
    // Results trickle in a few frames behind, as they do live.
    a.AddGpu(std::vector<GpuRecord>(gpu.begin(), gpu.begin() + 20));
    assert(a.Metrics()[10].cause == Cause::Gpu && a.Metrics()[30].cause == Cause::AppWaiting);
    a.AddGpu(std::vector<GpuRecord>(gpu.begin() + 20, gpu.end()));
    assert(a.Metrics()[30].cause == Cause::Gpu);
    assert(a.LateFrames().size() == 39);
    // Results for frames the analyzer never saw are ignored.
    GpuRecord stray = gpu[0];
    stray.frameId = 999999;
    a.AddGpu({stray});
}

void GpuSpikesCountOnce() {
    // 4 ms of CPU and 6 ms of GPU a frame, then one frame takes the GPU 30 ms.
    // The next frame's CPU blocks on it (as an app does on its swapchain or
    // fence), so its interval is long too, but that wait was the spike.
    SingleThreadApp app;
    for (int i = 0; i < 31; ++i) app.Frame(4.0);
    const uint64_t spikeEnd = app.out[30].waitExitQpc + Ticks(0.5 + 30.0);
    const uint64_t resume = std::max(app.now, app.nextTick);  // frame 31's wait returns here
    const double waitMs = double(spikeEnd - resume) * 1000.0 / double(kQpc);
    app.Frame(waitMs + 4.0, 4.0 / (waitMs + 4.0));
    for (int i = 0; i < 30; ++i) app.Frame(4.0);
    std::vector<GpuRecord> gpu = GpuFor(app.out, 0.5, 6.0);
    for (size_t i = 0; i < gpu.size(); ++i) {
        uint64_t begin = gpu[i].beginQpc;
        if (i) begin = std::max(begin, gpu[i - 1].endQpc);  // in order on one queue
        gpu[i].endQpc = begin + Ticks(i == 30 ? 30.0 : 6.0);
        gpu[i].endTicks = gpu[i].endQpc * 100;
    }
    Analyzer a = Make(app.out);
    a.AddGpu(gpu);

    const FrameMetrics& spike = a.Metrics()[30];
    assert(Near(spike.gpuMs, 30.0) && spike.cls == FrameClass::Hitch && spike.cause == Cause::Gpu);
    const FrameMetrics& next = a.Metrics()[31];
    assert(next.intervalMs > 30.0);
    assert(Near(next.gpuWaitMs, waitMs, 0.01) && Near(next.cpuMs, 4.2, 0.01));
    assert(Near(next.gpuMs, 6.0) && Near(next.costMs, 6.0, 0.01) && next.cls == FrameClass::Ok);
    // Steady frames overlap the previous GPU frame only while paced: no wait.
    assert(a.Metrics()[10].gpuWaitMs == 0.0f && Near(a.Metrics()[10].cpuMs, 4.2));
    const RangeStats s = a.Stats(1, a.Size());
    assert(s.hitches == 1 && a.LateFrames().size() == 1 && s.missedRefreshes == spike.missed);
}

void SimulatorStallsLandOnTheIntervalTheyFallIn() {
    // Frame 20's xrEndFrame takes 20 ms. That time runs between frame 20's
    // submission and frame 21's, so frame 21 is the late one and the simulator's.
    SingleThreadApp app;
    for (int i = 0; i < 20; ++i) app.Frame(5.0);
    app.Frame(5.0, 1.0, 20.0);
    for (int i = 0; i < 10; ++i) app.Frame(5.0);
    Analyzer a = Make(app.out);
    assert(Near(a.Metrics()[20].simulatorMs, 0.2, 0.01) && a.Metrics()[20].cls == FrameClass::Ok);
    const FrameMetrics& next = a.Metrics()[21];
    assert(Near(next.simulatorMs, 20.0, 0.01));
    assert(next.cls == FrameClass::Hitch && next.cause == Cause::Simulator);
}

void GpuResultsAheadOfTheirFrameAreKept() {
    // The runtime collects GPU results before it publishes the frame being
    // submitted, so a poll can see a result before its frame.
    SingleThreadApp app;
    for (int i = 0; i < 20; ++i) app.Frame(15.0, 0.05);
    Analyzer a;
    a.Reset(kQpc, kOrigin);
    a.SetTscFrequency(kTsc);
    a.SetBudget(90.0, 2.0);
    a.AddFrames(std::vector<FrameRecord>(app.out.begin(), app.out.begin() + 10));
    a.AddGpu(GpuFor(app.out, 0.5, 14.4));
    a.AddFrames(std::vector<FrameRecord>(app.out.begin() + 10, app.out.end()));
    assert(Near(a.Metrics()[15].gpuMs, 14.4) && a.Metrics()[15].cause == Cause::Gpu);
}

void LongStallsCountEveryMissedRefresh() {
    SingleThreadApp app;
    for (int i = 0; i < 5; ++i) app.Frame(5.0);
    app.Frame(5000.0);
    Analyzer a = Make(app.out);
    assert(a.Metrics()[5].missed > 255 && a.Metrics()[5].cls == FrameClass::Hitch);
}

} // namespace

int main() {
    SteadyFramesCarryTheirRealCost();
    LateFramesAreClassifiedAndExplained();
    SubmissionJitterIsNotLateness();
    PipelinedEnginesAreMeasuredOnTheBusierThread();
    IdleSessionsAndLostRecordsAreGaps();
    ColumnsKeepEverySpike();
    StatsMatchTheirDefinitions();
    GpuBoundFramesBlameTheGpu();
    GpuTimeLeavesOutIdleAndOverlap();
    LateGpuResultsRescoreTheirFrames();
    GpuSpikesCountOnce();
    SimulatorStallsLandOnTheIntervalTheyFallIn();
    GpuResultsAheadOfTheirFrameAreKept();
    LongStallsCountEveryMissedRefresh();
    std::cout << "frame analysis tests passed\n";
    return 0;
}
