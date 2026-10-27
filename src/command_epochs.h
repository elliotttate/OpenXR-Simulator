#pragma once

#include <atomic>
#include <cstdint>

namespace mcp {

// Frame stages consume commands independently. A render-stage acknowledgement
// must not erase a control-stage wakeup that arrived after xrWaitFrame.
class CommandEpoch {
public:
    void Notify() noexcept { generation_.fetch_add(1, std::memory_order_release); }
    bool ChangedSince(std::uint64_t& observed) const noexcept {
        const auto current = generation_.load(std::memory_order_acquire);
        if (observed == current) return false;
        observed = current;
        return true;
    }
private:
    std::atomic<std::uint64_t> generation_{1};
};

} // namespace mcp
