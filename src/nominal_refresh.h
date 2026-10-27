#pragma once
#include <cmath>
#include <cstdlib>
#include <cstdint>
namespace perf {
inline double NominalRefreshHz(const char* text) {
    if (!text || !*text) return 90.0;
    char* end = nullptr;
    const double hz = std::strtod(text, &end);
    return end != text && *end == '\0' && std::isfinite(hz) && hz >= 30.0 && hz <= 500.0 ? hz : 90.0;
}
inline double ConfiguredNominalRefreshHz() { static const double hz = NominalRefreshHz(std::getenv("OPENXR_SIM_REFRESH_HZ")); return hz; }
inline std::int64_t NominalPeriodNs(double hz) { return static_cast<std::int64_t>(1e9 / hz); }
}
