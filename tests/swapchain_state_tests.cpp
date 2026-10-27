#include "swapchain_state.h"

#include <cstdio>

namespace {

int failures = 0;

void Check(bool condition, const char* message) {
    if (condition) return;
    std::fprintf(stderr, "FAIL: %s\n", message);
    ++failures;
}

} // namespace

int main() {
    swapchain_state::Lifecycle lifecycle;
    lifecycle.Reset(3, false);

    uint32_t index = UINT32_MAX;
    Check(lifecycle.Wait(index) == XR_ERROR_CALL_ORDER_INVALID,
          "wait before acquire is rejected");
    Check(lifecycle.Release(index) == XR_ERROR_CALL_ORDER_INVALID,
          "release before wait is rejected");

    uint32_t first = UINT32_MAX, second = UINT32_MAX, third = UINT32_MAX;
    Check(lifecycle.Acquire(first) == XR_SUCCESS && first == 0,
          "first image is acquired");
    Check(lifecycle.IsAppOwned(first), "an acquired image is owned by the application");
    Check(lifecycle.Acquire(second) == XR_SUCCESS && second == 1,
          "multiple images may be acquired");
    Check(lifecycle.Acquire(third) == XR_SUCCESS && third == 2,
          "all available images may be acquired");
    Check(lifecycle.Acquire(index) == XR_ERROR_CALL_ORDER_INVALID,
          "an owned image cannot be reacquired");

    Check(lifecycle.Release(index) == XR_ERROR_CALL_ORDER_INVALID,
          "an acquired image must be waited before release");
    Check(lifecycle.Wait(index) == XR_SUCCESS && index == first,
          "wait selects the oldest acquisition");
    Check(lifecycle.Wait(index) == XR_ERROR_CALL_ORDER_INVALID,
          "the waited image must be released before the next wait");
    Check(lifecycle.Release(index) == XR_SUCCESS && index == first,
          "release selects the oldest waited acquisition");
    Check(!lifecycle.IsAppOwned(first), "a released image is no longer owned by the application");
    Check(lifecycle.Acquire(index) == XR_SUCCESS && index == first,
          "a released non-static image becomes available again");

    Check(lifecycle.Wait(index) == XR_SUCCESS && index == second,
          "FIFO order continues after reacquisition");
    Check(lifecycle.Release(index) == XR_SUCCESS && index == second,
          "the second acquired image releases second");

    lifecycle.Reset(1, true);
    Check(lifecycle.Acquire(index) == XR_SUCCESS && index == 0,
          "a static image can be acquired once");
    Check(lifecycle.Wait(index) == XR_SUCCESS, "a static image can be waited");
    Check(lifecycle.Release(index) == XR_SUCCESS, "a static image can be released");
    Check(lifecycle.Acquire(index) == XR_ERROR_CALL_ORDER_INVALID,
          "a static image cannot be acquired twice in its lifetime");

    if (failures) return 1;
    std::puts("swapchain state tests passed");
    return 0;
}
