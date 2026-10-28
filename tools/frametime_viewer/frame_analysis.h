// Turns raw frame-timeline records into the numbers the frame-time viewer
// draws. Pure computation over the records in frame_timeline.h - no Win32, no
// UI - so every definition here is unit-tested.
//
// Frame time. The simulator paces xrWaitFrame, so the gap between two
// submissions mixes the app's work with time it sat blocked waiting for the
// next tick. "Cost" removes that blocked time: it is how long the frame would
// have taken had nothing held the app back, which is what decides whether a
// headset would have shown it in time. The raw interval is kept alongside.
//
// In a pipelined engine the thread that waits (game) is not the one that
// submits (render), and the blocked time only says how idle the waiting thread
// was. A render thread busier than the game thread would hide behind it, so
// cost is never less than the submitting thread's own on-CPU time.
//
// GPU. When the runtime timed the app's GPU work (GpuRecord), a frame costs
// the larger of its CPU and GPU times: whichever processor was slower decides
// when the frame could be shown.
#pragma once
#include "frame_timeline.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <unordered_map>
#include <vector>

namespace perf::analysis {

using timeline::FrameRecord;
using timeline::GpuRecord;
using timeline::MarkerRecord;

enum class FrameClass : uint8_t { Ok, OverBudget, Hitch, Gap };

// Why a late frame was late, from what the CPU side can see.
enum class Cause : uint8_t { None, Simulator, AppBusy, AppWaiting, Unknown, Gpu };

inline const char* ClassName(FrameClass c) {
    switch (c) {
        case FrameClass::Ok: return "on time";
        case FrameClass::OverBudget: return "over budget";
        case FrameClass::Hitch: return "hitch";
        case FrameClass::Gap: return "gap";
    }
    return "?";
}

inline const char* CauseName(Cause c) {
    switch (c) {
        case Cause::None: return "";
        case Cause::Simulator: return "simulator";
        case Cause::AppBusy: return "app busy";
        case Cause::AppWaiting: return "app waiting";
        case Cause::Unknown: return "unknown";
        case Cause::Gpu: return "GPU";
    }
    return "?";
}

// A frame within this much of the budget is not called late: the pacing timer
// itself jitters by about this much.
constexpr double kBudgetToleranceMs = 0.25;
// A pause this long that spans a session state change is the session idling,
// not the app hitching.
constexpr double kGapMs = 1000.0;
// Frames per summary block. Blocks keep each column's maximum exact when a
// plot is zoomed out over hundreds of thousands of frames.
constexpr size_t kBlockFrames = 64;

struct FrameMetrics {
    double startSec = 0.0;       // previous submission: the left edge of this frame's bar
    double endSec = 0.0;         // this submission (xrEndFrame entry), seconds since the timeline began
    float intervalMs = 0.0f;     // since the previous submission; 0 when there is none
    float costMs = 0.0f;         // the larger of CPU and GPU time
    float cpuMs = 0.0f;          // interval minus the time the app was blocked by pacing or the previous frame's GPU
    float blockedMs = 0.0f;      // pacing headroom inside the interval
    float simulatorMs = 0.0f;    // the runtime's own share: xrEndFrame, plus xrWaitFrame's work before pacing
    float appSpanMs = NAN;       // matched xrWaitFrame return to xrEndFrame entry
    float pacingErrorMs = NAN;   // how late the matched wait returned after its tick
    float submitCpuMs = NAN;     // the submitting thread's on-CPU time between its xrEndFrame calls
    float submitOnCpu = NAN;     // that time as a share of the wall time it spans
    float gpuMs = NAN;           // GPU time: end minus the later of begin and the previous frame's end
    float gpuLatencyMs = NAN;    // xrEndFrame entry to the GPU finishing the frame (calibrated APIs)
    float gpuWaitMs = NAN;       // app time spent waiting on the GPU's previous frame (calibrated APIs)
    double gpuStartSec = NAN;    // when the GPU started and finished it (calibrated APIs)
    double gpuEndSec = NAN;
    uint32_t missed = 0;         // refreshes a headset would have repeated
    bool gap = false;            // no usable interval: first frame, lost records, or an idle session
    FrameClass cls = FrameClass::Ok;
    Cause cause = Cause::None;
};

struct RangeStats {
    size_t frames = 0;           // frames with an interval (gaps excluded)
    size_t gaps = 0;             // frames without a usable interval
    size_t idleGaps = 0;         // of those, pauses where the session went idle
    double seconds = 0.0;
    double deliveredFps = 0.0;   // frames per second of submitted intervals
    double costMeanMs = 0.0, costP50Ms = 0.0, costP90Ms = 0.0, costP99Ms = 0.0, costP999Ms = 0.0, costMaxMs = 0.0;
    double intervalP50Ms = 0.0, intervalP99Ms = 0.0;
    double onePercentLowFps = 0.0;  // 1000 / mean cost of the slowest 1% of frames
    double headroomP50Ms = 0.0;
    double simulatorP50Ms = 0.0, simulatorP99Ms = 0.0;
    size_t overBudget = 0;       // late frames, hitches included
    size_t hitches = 0;
    size_t missedRefreshes = 0;
    size_t gpuFrames = 0;        // frames with a GPU time
    size_t gpuOverBudget = 0;    // of those, how many the GPU alone made late
    double gpuP50Ms = 0.0, gpuP99Ms = 0.0, gpuMaxMs = 0.0;
};

enum class XAxis { Time, Frame };

// One pixel column of a zoomed-out plot.
struct Column {
    uint32_t maxIndex = UINT32_MAX;  // the costliest non-gap frame in the column
    float maxCostMs = 0.0f;
    float maxIntervalMs = 0.0f;
    float maxGpuMs = NAN;
    uint32_t frames = 0;
    bool noProjection = false;       // any 2D-only frame
};

class Analyzer {
public:
    void Reset(uint64_t qpcFrequency, uint64_t originQpc) {
        m_records.clear();
        m_metrics.clear();
        m_markers.clear();
        m_blocks.clear();
        m_late.clear();
        m_gpu.clear();
        m_pendingGpu.clear();
        m_indexById.clear();
        m_gpuApi = 0;
        ++m_revision;
        m_qpcFrequency = qpcFrequency ? qpcFrequency : 1;
        m_originQpc = originQpc;
    }

    // Thread-cycle rate, once the runtime has calibrated it. Changing it
    // recomputes every frame's on-CPU share.
    void SetTscFrequency(uint64_t hz) {
        if (hz == m_tscFrequency) return;
        // The runtime refines its estimate as it runs. A change this small moves no
        // frame's on-CPU share visibly, and every change rescores the whole history.
        if (m_tscFrequency && hz && std::abs(double(hz) - double(m_tscFrequency)) < 0.005 * double(m_tscFrequency)) return;
        m_tscFrequency = hz;
        Recompute(0);
    }

    void SetBudget(double refreshHz, double hitchBudgets) {
        m_budgetMs = 1000.0 / (refreshHz > 1.0 ? refreshHz : 90.0);
        m_hitchMs = m_budgetMs * (hitchBudgets > 1.0 ? hitchBudgets : 2.0);
        for (FrameMetrics& m : m_metrics) Classify(m);
        RebuildFrom(0);
    }

    double BudgetMs() const { return m_budgetMs; }
    double HitchMs() const { return m_hitchMs; }
    uint64_t QpcFrequency() const { return m_qpcFrequency; }
    uint64_t TscFrequency() const { return m_tscFrequency; }

    // Markers first: a frame whose interval spans a session state change is
    // classified as a gap when it arrives.
    void AddMarkers(const std::vector<MarkerRecord>& markers) {
        m_markers.insert(m_markers.end(), markers.begin(), markers.end());
    }

    void AddFrames(const std::vector<FrameRecord>& frames) {
        if (frames.empty()) return;
        const size_t first = m_records.size();
        m_records.insert(m_records.end(), frames.begin(), frames.end());
        m_metrics.resize(m_records.size());
        m_gpu.resize(m_records.size(), GpuRecord{});
        for (size_t i = first; i < m_records.size(); ++i) m_indexById[m_records[i].frameId] = uint32_t(i);
        // GPU results that came in ahead of these frames.
        if (!m_pendingGpu.empty()) {
            std::vector<GpuRecord> pending;
            pending.swap(m_pendingGpu);
            for (const GpuRecord& g : pending) {
                const auto it = m_indexById.find(g.frameId);
                if (it != m_indexById.end()) m_gpu[it->second] = g;
                else if (!m_records.empty() && g.frameId > m_records.back().frameId) m_pendingGpu.push_back(g);
            }
        }
        // A pipelined app waits for the next frame before submitting this one,
        // so the pacing that falls inside the last few intervals may only just
        // have arrived with the new records.
        Recompute(first >= 3 ? first - 3 : 0);
    }

    const std::vector<FrameRecord>& Records() const { return m_records; }
    const std::vector<FrameMetrics>& Metrics() const { return m_metrics; }
    const std::vector<MarkerRecord>& Markers() const { return m_markers; }

    // GPU results trail their frames by a few; each re-scores its frame and the
    // next (whose GPU time starts no earlier than this one ended).
    void AddGpu(const std::vector<GpuRecord>& records) {
        size_t from = SIZE_MAX;
        for (const GpuRecord& g : records) {
            if (!g.tickFrequency || !g.endTicks) continue;
            const auto it = m_indexById.find(g.frameId);
            if (it == m_indexById.end()) {
                // Published ahead of its frame record; matched when that arrives.
                if (m_records.empty() || g.frameId > m_records.back().frameId) {
                    if (m_pendingGpu.size() < 4096) m_pendingGpu.push_back(g);
                }
                continue;
            }
            m_gpu[it->second] = g;
            m_gpuApi = g.api;
            from = std::min<size_t>(from, it->second);
        }
        if (from != SIZE_MAX) Recompute(from);
    }

    // The GPU record of frame `i`; frameId 0 when it has none.
    const GpuRecord& Gpu(size_t i) const { return m_gpu[i]; }
    // Changes whenever any frame's metrics may have; a reader caching results compares it.
    uint64_t Revision() const { return m_revision; }
    uint32_t GpuApi() const { return m_gpuApi; }
    // Indices of late frames (over budget or hitch), in order.
    const std::vector<uint32_t>& LateFrames() const { return m_late; }
    size_t Size() const { return m_metrics.size(); }

    double Sec(uint64_t qpc) const {
        return (double(qpc) - double(m_originQpc)) / double(m_qpcFrequency);
    }
    double Ms(uint64_t ticks) const { return double(ticks) * 1000.0 / double(m_qpcFrequency); }

    double StartX(XAxis axis, size_t i) const { return axis == XAxis::Time ? m_metrics[i].startSec : double(i); }
    double EndX(XAxis axis, size_t i) const { return axis == XAxis::Time ? m_metrics[i].endSec : double(i + 1); }

    // First frame whose bar ends at or after x.
    size_t LowerBound(XAxis axis, double x) const {
        if (axis == XAxis::Frame) {
            if (x <= 1.0) return 0;
            const double i = std::ceil(x - 1.0);
            return i >= double(m_metrics.size()) ? m_metrics.size() : size_t(i);
        }
        return size_t(std::lower_bound(m_metrics.begin(), m_metrics.end(), x,
                                       [](const FrameMetrics& m, double v) { return m.endSec < v; }) -
                      m_metrics.begin());
    }

    RangeStats Stats(size_t begin, size_t end) const {
        RangeStats s;
        end = std::min(end, m_metrics.size());
        if (begin >= end) return s;
        std::vector<float> cost, interval, headroom, simulator, gpu;
        cost.reserve(end - begin);
        double costSum = 0.0, intervalSum = 0.0;
        for (size_t i = begin; i < end; ++i) {
            const FrameMetrics& m = m_metrics[i];
            if (m.gap) {
                ++s.gaps;
                if (m.intervalMs > 0.0f) ++s.idleGaps;
                continue;
            }
            cost.push_back(m.costMs);
            interval.push_back(m.intervalMs);
            headroom.push_back(m.blockedMs);
            simulator.push_back(m.simulatorMs);
            if (!std::isnan(m.gpuMs)) {
                gpu.push_back(m.gpuMs);
                if (m.gpuMs > m_budgetMs + kBudgetToleranceMs) ++s.gpuOverBudget;
            }
            costSum += m.costMs;
            intervalSum += m.intervalMs;
            if (m.cls == FrameClass::OverBudget || m.cls == FrameClass::Hitch) ++s.overBudget;
            if (m.cls == FrameClass::Hitch) ++s.hitches;
            s.missedRefreshes += m.missed;
        }
        s.seconds = m_metrics[end - 1].endSec - m_metrics[begin].startSec;
        s.frames = cost.size();
        if (cost.empty()) return s;
        s.deliveredFps = intervalSum > 0.0 ? 1000.0 * double(s.frames) / intervalSum : 0.0;
        s.costMeanMs = costSum / double(s.frames);
        s.costP50Ms = Select(cost, 0.50);
        s.costP90Ms = Select(cost, 0.90);
        s.costP99Ms = Select(cost, 0.99);
        s.costP999Ms = Select(cost, 0.999);
        s.costMaxMs = *std::max_element(cost.begin(), cost.end());
        s.intervalP50Ms = Select(interval, 0.50);
        s.intervalP99Ms = Select(interval, 0.99);
        s.headroomP50Ms = Select(headroom, 0.50);
        s.simulatorP50Ms = Select(simulator, 0.50);
        s.simulatorP99Ms = Select(simulator, 0.99);
        s.gpuFrames = gpu.size();
        if (!gpu.empty()) {
            s.gpuP50Ms = Select(gpu, 0.50);
            s.gpuP99Ms = Select(gpu, 0.99);
            s.gpuMaxMs = *std::max_element(gpu.begin(), gpu.end());
        }
        const size_t slowest = std::max<size_t>(1, s.frames / 100);
        std::nth_element(cost.begin(), cost.end() - ptrdiff_t(slowest), cost.end());
        double slowSum = 0.0;
        for (auto it = cost.end() - ptrdiff_t(slowest); it != cost.end(); ++it) slowSum += *it;
        s.onePercentLowFps = 1000.0 / (slowSum / double(slowest));
        return s;
    }

    // Frame-cost histogram of [begin, end) per class (Ok, OverBudget, Hitch).
    // The last bin also holds everything beyond it.
    void Histogram(size_t begin, size_t end, double binMs, size_t bins,
                   std::vector<double> (&counts)[3]) const {
        for (auto& c : counts) c.assign(bins, 0.0);
        if (bins == 0 || binMs <= 0.0) return;
        end = std::min(end, m_metrics.size());
        for (size_t i = begin; i < end; ++i) {
            const FrameMetrics& m = m_metrics[i];
            if (m.gap) continue;
            const size_t bin = std::min(bins - 1, size_t(std::max(0.0, double(m.costMs)) / binMs));
            counts[size_t(m.cls)][bin] += 1.0;
        }
    }

    // Per-pixel-column summary of [x0, x1). Each column's maximum is exact, so
    // no spike disappears however far the plot is zoomed out.
    void Columns(XAxis axis, double x0, double x1, int columns, std::vector<Column>& out) const {
        out.assign(columns > 0 ? size_t(columns) : 0, Column{});
        if (columns <= 0 || x1 <= x0 || m_metrics.empty()) return;
        const double width = (x1 - x0) / double(columns);
        size_t lo = LowerBound(axis, x0);
        for (int c = 0; c < columns && lo < m_metrics.size(); ++c) {
            const double right = c + 1 == columns ? x1 : x0 + width * double(c + 1);
            const size_t hi = std::min(m_metrics.size(), LowerBound(axis, right));
            if (hi > lo) Summarize(lo, hi, out[size_t(c)]);
            lo = hi;
        }
    }

    // Frames in [x0, x1) wider than `minWidth`: long stalls a zoomed-out plot
    // should still draw at their true width.
    void WideFrames(XAxis axis, double x0, double x1, double minWidth, std::vector<uint32_t>& out) const {
        out.clear();
        if (axis != XAxis::Time || m_metrics.empty()) return;
        const double minMs = minWidth * 1000.0;
        size_t i = LowerBound(axis, x0);
        while (i < m_metrics.size() && m_metrics[i].startSec < x1) {
            const size_t block = i / kBlockFrames;
            const size_t blockEnd = std::min(m_metrics.size(), (block + 1) * kBlockFrames);
            if (i % kBlockFrames == 0 && blockEnd - i == kBlockFrames && m_blocks[block].maxIntervalMs < minMs) {
                i = blockEnd;  // nothing that wide in this block
                continue;
            }
            const FrameMetrics& m = m_metrics[i];
            if (!m.gap && m.intervalMs >= minMs) out.push_back(uint32_t(i));
            ++i;
        }
    }

private:
    struct Block {
        uint32_t maxIndex = UINT32_MAX;
        float maxCostMs = 0.0f;
        float maxIntervalMs = 0.0f;
        float maxGpuMs = NAN;
        uint32_t frames = 0;
        bool noProjection = false;
    };

    // Nearest-rank percentile without sorting; reorders `values`.
    static double Select(std::vector<float>& values, double p) {
        if (values.empty()) return 0.0;
        const size_t rank = size_t(std::ceil(p * double(values.size())));
        const size_t k = std::min(values.size() - 1, rank ? rank - 1 : 0);
        std::nth_element(values.begin(), values.begin() + ptrdiff_t(k), values.end());
        return values[k];
    }

    void Add(const FrameMetrics& m, size_t i, Column& c) const {
        c.noProjection |= (m_records[i].flags & timeline::kFrameNoProjection) != 0;
        if (m.gap) return;
        ++c.frames;
        c.maxIntervalMs = std::max(c.maxIntervalMs, m.intervalMs);
        if (!std::isnan(m.gpuMs) && !(m.gpuMs <= c.maxGpuMs)) c.maxGpuMs = m.gpuMs;
        if (c.maxIndex == UINT32_MAX || m.costMs > c.maxCostMs) {
            c.maxCostMs = m.costMs;
            c.maxIndex = uint32_t(i);
        }
    }

    void Summarize(size_t lo, size_t hi, Column& c) const {
        size_t i = lo;
        while (i < hi) {
            const size_t block = i / kBlockFrames;
            const size_t blockEnd = (block + 1) * kBlockFrames;
            if (i % kBlockFrames == 0 && blockEnd <= hi && block < m_blocks.size()) {
                const Block& b = m_blocks[block];
                c.noProjection |= b.noProjection;
                c.frames += b.frames;
                c.maxIntervalMs = std::max(c.maxIntervalMs, b.maxIntervalMs);
                if (!std::isnan(b.maxGpuMs) && !(b.maxGpuMs <= c.maxGpuMs)) c.maxGpuMs = b.maxGpuMs;
                if (b.maxIndex != UINT32_MAX && (c.maxIndex == UINT32_MAX || b.maxCostMs > c.maxCostMs)) {
                    c.maxCostMs = b.maxCostMs;
                    c.maxIndex = b.maxIndex;
                }
                i = blockEnd;
            } else {
                Add(m_metrics[i], i, c);
                ++i;
            }
        }
    }

    void Recompute(size_t from) {
        for (size_t i = from; i < m_metrics.size(); ++i) Compute(i);
        RebuildFrom(from);
        ++m_revision;
    }

    // Blocks and the late-frame list from frame `from` on.
    void RebuildFrom(size_t from) {
        const size_t firstBlock = from / kBlockFrames;
        m_blocks.resize((m_metrics.size() + kBlockFrames - 1) / kBlockFrames);
        for (size_t b = firstBlock; b < m_blocks.size(); ++b) {
            Column c;
            const size_t end = std::min(m_metrics.size(), (b + 1) * kBlockFrames);
            for (size_t i = b * kBlockFrames; i < end; ++i) Add(m_metrics[i], i, c);
            m_blocks[b] = Block{c.maxIndex, c.maxCostMs, c.maxIntervalMs, c.maxGpuMs, c.frames, c.noProjection};
        }
        while (!m_late.empty() && m_late.back() >= from) m_late.pop_back();
        for (size_t i = from; i < m_metrics.size(); ++i) {
            const FrameClass cls = m_metrics[i].cls;
            if (cls == FrameClass::OverBudget || cls == FrameClass::Hitch) m_late.push_back(uint32_t(i));
        }
    }

    bool SessionChangedBetween(double startSec, double endSec) const {
        for (auto it = m_markers.rbegin(); it != m_markers.rend(); ++it) {
            const double t = Sec(it->qpc);
            if (t <= startSec) break;
            if (t <= endSec && it->kind == uint32_t(timeline::MarkerKind::SessionState)) return true;
        }
        return false;
    }

    void Compute(size_t i) {
        const FrameRecord& r = m_records[i];
        FrameMetrics m;
        m.endSec = Sec(r.endEnterQpc);
        m.startSec = m.endSec;
        const bool matched = (r.flags & timeline::kFrameWaitMatched) != 0 && r.waitExitQpc;
        const bool hasPrevious = i > 0 && m_records[i - 1].index + 1 == r.index &&
                                 r.endEnterQpc > m_records[i - 1].endEnterQpc;
        if (hasPrevious) {
            const FrameRecord& p = m_records[i - 1];
            m.startSec = Sec(p.endEnterQpc);
            m.intervalMs = float(Ms(r.endEnterQpc - p.endEnterQpc));
            // Time between `from` and `to` that the app sat blocked by pacing.
            auto blockedIn = [&](uint64_t from, uint64_t to) {
                double ms = 0.0;
                const size_t last = std::min(m_records.size(), i + 3);
                for (size_t j = i >= 2 ? i - 2 : 0; j < last; ++j) {
                    const FrameRecord& w = m_records[j];
                    if (!(w.flags & timeline::kFrameWaitMatched) || !w.waitPaceQpc || w.waitExitQpc < w.waitPaceQpc) continue;
                    const uint64_t a = std::max(w.waitPaceQpc, from);
                    const uint64_t b = std::min(w.waitExitQpc, to);
                    if (b > a) ms += Ms(b - a);
                }
                return ms;
            };
            const double blocked = blockedIn(p.endEnterQpc, r.endEnterQpc);
            m.blockedMs = float(blocked);
            m.gap = m.intervalMs >= kGapMs && SessionChangedBetween(m.startSec, m.endSec);

            // The submitting thread's on-CPU share between its xrEndFrame calls,
            // leaving out its own time in xrWaitFrame when it waits as well.
            double offCpuMs = NAN;
            if (m_tscFrequency && p.endThreadId == r.endThreadId && r.endEnterQpc > p.endExitQpc &&
                r.endEnterCycles >= p.endExitCycles && p.endExitQpc) {
                double cycles = double(r.endEnterCycles - p.endExitCycles);
                double ticks = double(r.endEnterQpc - p.endExitQpc);
                if (matched && r.waitThreadId == r.endThreadId && r.waitEnterQpc >= p.endExitQpc &&
                    r.waitExitCycles >= r.waitEnterCycles) {
                    cycles -= double(r.waitExitCycles - r.waitEnterCycles);
                    ticks -= double(r.waitExitQpc - r.waitEnterQpc);
                }
                if (ticks > 0.0) {
                    const double cpuMs = cycles * 1000.0 / double(m_tscFrequency);
                    const double wallMs = ticks * 1000.0 / double(m_qpcFrequency);
                    m.submitCpuMs = float(std::clamp(cpuMs, 0.0, wallMs));
                    m.submitOnCpu = float(double(m.submitCpuMs) / wallMs);
                    offCpuMs = wallMs - double(m.submitCpuMs);
                }
            }
            double cost = std::max(0.0, double(m.intervalMs) - blocked);
            // Waiting for the GPU to finish the previous frame is that frame's
            // cost, not this one's; left in, a GPU spike would count twice, on its
            // own frame and again on the next, whose CPU sat it out. The wait is
            // the app time the GPU was still on the previous frame, as far as the
            // submitting thread was off its CPU then. An overlap within the
            // tolerance is calibration noise.
            const GpuRecord& prior = m_gpu[i - 1];
            if (!std::isnan(offCpuMs) && prior.frameId && (prior.flags & timeline::kGpuCalibrated)) {
                double wait = 0.0;
                if (prior.endQpc > p.endExitQpc) {
                    const uint64_t until = std::min(prior.endQpc, r.endEnterQpc);
                    wait = std::min(Ms(until - p.endExitQpc) - blockedIn(p.endExitQpc, until), offCpuMs);
                    if (wait <= kBudgetToleranceMs) wait = 0.0;
                }
                m.gpuWaitMs = float(wait);
                cost = std::max(0.0, cost - wait);
            }
            if (!std::isnan(m.submitCpuMs)) cost = std::max(cost, double(m.submitCpuMs));
            m.cpuMs = float(cost);
            m.costMs = m.cpuMs;
        } else {
            m.gap = true;
        }
        // The interval runs from the previous frame's xrEndFrame entry to this one's, so
        // the xrEndFrame inside it is the previous frame's.
        if (hasPrevious) {
            const FrameRecord& p = m_records[i - 1];
            if (p.endExitQpc >= p.endEnterQpc) m.simulatorMs = float(Ms(p.endExitQpc - p.endEnterQpc));
        }
        if (matched) {
            if (r.waitPaceQpc >= r.waitEnterQpc) m.simulatorMs += float(Ms(r.waitPaceQpc - r.waitEnterQpc));
            if (r.endEnterQpc >= r.waitExitQpc) m.appSpanMs = float(Ms(r.endEnterQpc - r.waitExitQpc));
            m.pacingErrorMs = float((double(r.waitExitQpc) - double(r.waitTargetQpc)) * 1000.0 / double(m_qpcFrequency));
        }
        const GpuRecord& g = m_gpu[i];
        if (g.frameId && g.tickFrequency && g.endTicks) {
            // The GPU's frame starts at its begin marker, or where it finished the
            // previous frame if that was later; with no begin marker, only then.
            uint64_t start = g.beginTicks;
            if (hasPrevious) {
                const GpuRecord& prior = m_gpu[i - 1];
                if (prior.frameId && prior.tickFrequency == g.tickFrequency && prior.endTicks > start &&
                    prior.endTicks <= g.endTicks) {
                    start = prior.endTicks;
                }
            }
            if (start && g.endTicks >= start) {
                m.gpuMs = float(double(g.endTicks - start) * 1000.0 / double(g.tickFrequency));
            }
            if (g.flags & timeline::kGpuCalibrated) {
                m.gpuEndSec = Sec(g.endQpc);
                m.gpuStartSec = start == g.beginTicks ? Sec(g.beginQpc)
                                : m.gpuEndSec - double(g.endTicks - start) / double(g.tickFrequency);
                if (g.endQpc >= r.endEnterQpc) m.gpuLatencyMs = float(Ms(g.endQpc - r.endEnterQpc));
            }
            if (!m.gap && !std::isnan(m.gpuMs)) m.costMs = std::max(m.cpuMs, m.gpuMs);
        }
        Classify(m);
        m_metrics[i] = m;
    }

    void Classify(FrameMetrics& m) const {
        m.missed = 0;
        m.cause = Cause::None;
        if (m.gap) {
            m.cls = FrameClass::Gap;
            return;
        }
        const double cost = m.costMs;
        if (cost > m_budgetMs + kBudgetToleranceMs) {
            const double refreshes = std::ceil((cost - kBudgetToleranceMs) / m_budgetMs);
            m.missed = uint32_t(std::min(1.0e9, std::max(1.0, refreshes - 1.0)));
        }
        m.cls = cost > m_hitchMs ? FrameClass::Hitch
              : m.missed ? FrameClass::OverBudget
              : FrameClass::Ok;
        if (m.cls == FrameClass::Ok) return;
        const bool gpuLate = !std::isnan(m.gpuMs) && m.gpuMs > m_budgetMs + kBudgetToleranceMs;
        const bool cpuBusier = !std::isnan(m.submitOnCpu) && m.submitOnCpu >= 0.5f && m.submitCpuMs > m.gpuMs;
        if (m.cpuMs - m.simulatorMs <= m_budgetMs + kBudgetToleranceMs && !gpuLate) {
            m.cause = Cause::Simulator;  // without the runtime's share the frame would have fit
        } else if (gpuLate && !cpuBusier) {
            m.cause = Cause::Gpu;        // the GPU alone overran; a waiting CPU was most likely waiting on it
        } else if (!std::isnan(m.submitOnCpu)) {
            m.cause = m.submitOnCpu < 0.5f ? Cause::AppWaiting : Cause::AppBusy;
        } else {
            m.cause = Cause::Unknown;
        }
    }

    std::vector<FrameRecord> m_records;
    std::vector<FrameMetrics> m_metrics;
    std::vector<MarkerRecord> m_markers;
    std::vector<Block> m_blocks;
    std::vector<uint32_t> m_late;
    std::vector<GpuRecord> m_gpu;                        // by frame index; frameId 0 = none yet
    std::vector<GpuRecord> m_pendingGpu;                 // results for frames not seen yet
    uint64_t m_revision = 0;
    std::unordered_map<uint64_t, uint32_t> m_indexById;  // frameId -> index
    uint32_t m_gpuApi = 0;
    uint64_t m_qpcFrequency = 1;
    uint64_t m_originQpc = 0;
    uint64_t m_tscFrequency = 0;
    double m_budgetMs = 1000.0 / 90.0;
    double m_hitchMs = 2000.0 / 90.0;
};

} // namespace perf::analysis
