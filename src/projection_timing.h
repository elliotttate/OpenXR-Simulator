#pragma once

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <deque>
#include <vector>

namespace perf {

struct ProjectionTimingSnapshot {
    bool active = false;
    double fps = 0.0;
    double latestFrameMs = 0.0;
    double p50FrameMs = 0.0;
    double p95FrameMs = 0.0;
    size_t sampleCount = 0;
};

// One sample per xrEndFrame containing a valid two-view projection. Measuring
// at this seam reports the app's stereo submission cadence and cannot be
// inflated by projection-layer count, WM_PAINT, or preview readback cadence.
class ProjectionTimingTracker {
public:
    using Clock = std::chrono::steady_clock;

    void Observe(bool hasStereoProjection, Clock::time_point now) {
        if (!hasStereoProjection) {
            if (m_hasLastProjection && now - m_lastProjection > kInactiveGap) {
                Reset();
            }
            return;
        }

        if (!m_hasLastProjection || now - m_lastProjection > kInactiveGap) {
            Reset();
            m_hasLastProjection = true;
            m_lastProjection = now;
            return;
        }

        const double frameMs = std::chrono::duration<double, std::milli>(
            now - m_lastProjection).count();
        m_lastProjection = now;
        if (frameMs < 0.1 || frameMs > 500.0) return;

        m_frameTimes.push_back(frameMs);
        m_sumFrameMs += frameMs;
        m_latestFrameMs = frameMs;
        while (m_frameTimes.size() > kMaxSamples) {
            m_sumFrameMs -= m_frameTimes.front();
            m_frameTimes.pop_front();
        }
    }

    ProjectionTimingSnapshot Snapshot(Clock::time_point now) const {
        ProjectionTimingSnapshot result;
        result.active = m_hasLastProjection &&
            now - m_lastProjection <= kInactiveGap;
        result.sampleCount = m_frameTimes.size();
        if (!result.active || m_frameTimes.empty()) return result;

        result.fps = 1000.0 * (double)m_frameTimes.size() / m_sumFrameMs;
        result.latestFrameMs = m_latestFrameMs;

        std::vector<double> sorted(m_frameTimes.begin(), m_frameTimes.end());
        std::sort(sorted.begin(), sorted.end());
        result.p50FrameMs = Percentile(sorted, 0.50);
        result.p95FrameMs = Percentile(sorted, 0.95);
        return result;
    }

    bool ShouldRefreshTitle(Clock::time_point now) {
        if (!m_hasTitleUpdate ||
            now - m_lastTitleUpdate >= std::chrono::milliseconds(250)) {
            m_hasTitleUpdate = true;
            m_lastTitleUpdate = now;
            return true;
        }
        return false;
    }

private:
    static constexpr size_t kMaxSamples = 240;
    static constexpr auto kInactiveGap = std::chrono::milliseconds(500);

    static double Percentile(const std::vector<double>& sorted, double percentile) {
        if (sorted.empty()) return 0.0;
        const size_t index = (size_t)std::ceil(percentile * (double)sorted.size()) - 1;
        return sorted[(std::min)(index, sorted.size() - 1)];
    }

    void Reset() {
        m_frameTimes.clear();
        m_sumFrameMs = 0.0;
        m_latestFrameMs = 0.0;
        m_hasLastProjection = false;
    }

    std::deque<double> m_frameTimes;
    double m_sumFrameMs = 0.0;
    double m_latestFrameMs = 0.0;
    bool m_hasLastProjection = false;
    Clock::time_point m_lastProjection{};
    bool m_hasTitleUpdate = false;
    Clock::time_point m_lastTitleUpdate{};
};

} // namespace perf
