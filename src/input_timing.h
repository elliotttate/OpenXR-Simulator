#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>

namespace perf {
inline double InputSampleSeconds(std::int64_t now, std::int64_t previous,
                                 std::int64_t frequency) {
    if (previous <= 0 || now <= previous || frequency <= 0) return 0.0;
    return (static_cast<double>(now) - static_cast<double>(previous)) /
           static_cast<double>(frequency);
}
// Bound keyboard movement through long stalls, separately from velocity timing.
inline float InputDeltaSeconds(std::int64_t now, std::int64_t previous,
                               std::int64_t frequency) {
    return static_cast<float>(std::min(InputSampleSeconds(now, previous, frequency), 0.1));
}

// Seed nonzero initial poses rather than differentiating them from the origin.
// Focus/session recovery and invalid/long intervals yield zero, then a fresh sample.
template <std::size_t N> class InputVelocityHistory {
public:
    void Reset() { valid_ = false; }
    std::array<float, N> Sample(const std::array<float, N>& value, double seconds) {
        std::array<float, N> velocity{};
        for (float v : value) if (!std::isfinite(v)) { Reset(); return velocity; }
        if (valid_ && std::isfinite(seconds) && seconds > 0.0 && seconds <= 0.1)
            for (std::size_t i = 0; i < N; ++i)
                velocity[i] = static_cast<float>((value[i] - previous_[i]) / seconds);
        previous_ = value;
        valid_ = true;
        return velocity;
    }
private:
    std::array<float, N> previous_{};
    bool valid_ = false;
};
}
