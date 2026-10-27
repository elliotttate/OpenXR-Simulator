#pragma once
#include <cstdint>

namespace perf {
struct SubmissionCadenceSnapshot {
    uint64_t stereoSubmissions = 0;
    int64_t lastSubmissionMonotonicNs = 0;
    uint64_t segment = 0;
    double frameIntervalMs = 0.0;
    bool intervalValid = false;
    bool lastFrameHadStereo = false;
};

// Constant-cost counters at the valid-stereo xrEndFrame seam. Difference two
// snapshots from the same segment to measure cadence with the game recorder off.
// These measure submissions, not physical display delivery or GPU execution.
// Owned exclusively by the xrEndFrame thread; no cross-thread reader is allowed.
class SubmissionCadence {
public:
    void Observe(bool stereo, int64_t nowNs) {
        m_state.lastFrameHadStereo = stereo;
        if (!stereo) {
            m_hasPrevious = false;
            m_state.intervalValid = false;
            m_state.frameIntervalMs = 0.0;
            return;
        }
        ++m_state.stereoSubmissions;
        const bool valid = m_hasPrevious && nowNs > m_state.lastSubmissionMonotonicNs &&
            nowNs - m_state.lastSubmissionMonotonicNs <= 500000000;
        m_state.intervalValid = valid;
        m_state.frameIntervalMs = valid ?
            static_cast<double>(nowNs - m_state.lastSubmissionMonotonicNs) / 1000000.0 : 0.0;
        if (!valid) ++m_state.segment;
        m_state.lastSubmissionMonotonicNs = nowNs;
        m_hasPrevious = nowNs >= 0;
    }
    SubmissionCadenceSnapshot Snapshot() const { return m_state; }
private:
    SubmissionCadenceSnapshot m_state;
    bool m_hasPrevious = false;
};
} // namespace perf
