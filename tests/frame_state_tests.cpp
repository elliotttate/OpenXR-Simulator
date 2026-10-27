#include "frame_state.h"

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
    frame_state::Lifecycle lifecycle;
    lifecycle.Reset();

    Check(lifecycle.CanWait(), "a reset session can wait for its first frame");
    Check(lifecycle.Begin() == XR_ERROR_CALL_ORDER_INVALID,
          "begin without a successful wait is rejected");
    Check(lifecycle.End() == XR_ERROR_CALL_ORDER_INVALID,
          "end without a successful begin is rejected");

    Check(lifecycle.MarkWaited() == XR_SUCCESS, "the first wait succeeds");
    Check(!lifecycle.CanWait(), "another wait blocks until the pending frame is begun");
    Check(lifecycle.Begin() == XR_SUCCESS, "begin consumes the corresponding wait");
    Check(lifecycle.CanWait(), "a pipelined wait may proceed after begin");
    Check(lifecycle.CanEnd(), "a begun frame may be ended");
    Check(lifecycle.End() == XR_SUCCESS, "end completes the begun frame");
    Check(lifecycle.End() == XR_ERROR_CALL_ORDER_INVALID,
          "the same frame cannot be ended twice");

    Check(lifecycle.MarkWaited() == XR_SUCCESS, "a new frame can be waited");
    Check(lifecycle.Begin() == XR_SUCCESS, "the new frame can be begun");
    Check(lifecycle.MarkWaited() == XR_SUCCESS, "the next frame can be pipelined");
    Check(lifecycle.Begin() == XR_FRAME_DISCARDED,
          "beginning the next frame discards an unended prior frame");
    Check(lifecycle.CanEnd(), "the replacement frame remains begun");
    Check(lifecycle.End() == XR_SUCCESS, "the replacement frame can be ended");

    Check(lifecycle.MarkWaited() == XR_SUCCESS, "state can become pending before reset");
    lifecycle.Reset();
    Check(lifecycle.CanWait(), "session restart clears the pending wait");
    Check(!lifecycle.CanEnd(), "session restart clears the begun frame");

    if (failures) return 1;
    std::puts("frame state tests passed");
    return 0;
}
