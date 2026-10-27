#pragma once
#include <windows.h>

namespace perf {
// A runtime owns its preview UI thread, not the application's message queue.
// Retain the old behavior only as an explicit startup compatibility diagnostic.
inline bool HostMessagePumpEnabled() {
    static const bool enabled = []() {
        char value[8]{};
        return GetEnvironmentVariableA("SIMXR_PUMP_HOST_MESSAGES", value, sizeof(value)) == 1 && value[0] == '1';
    }();
    return enabled;
}
inline unsigned PumpHostMessagesIfEnabled() {
    if (!HostMessagePumpEnabled()) return 0;
    MSG message{};
    unsigned drained = 0;
    while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
        if (++drained == 2000) break;
    }
    return drained;
}
} // namespace perf
