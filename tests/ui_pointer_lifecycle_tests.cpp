#include "ui_automation.h"

#include <cstdio>

namespace {
int failures = 0;
void Check(bool condition, const char* message) {
    if (condition) return;
    std::fprintf(stderr, "FAIL: %s\n", message);
    ++failures;
}
ui_auto::Pointer Pending(const char* action, int dwell = 45) {
    ui_auto::Pointer p;
    p.owned = p.pending = true;
    p.action = action;
    p.down = p.action == "hold";
    p.dwell = dwell;
    p.hold = 2;
    return p;
}
} // namespace

int main() {
    // A Cancel can consume the first held snapshot and immediately remove the
    // composition carrier. This is an already-delivered press, even while the
    // requested 45 observation cycles have not finished reporting the lease.
    auto held = Pending("hold");
    Check(ui_auto::NeedsPrepressValidation(held), "a new hold validates geometry before its first published snapshot");
    Check(!ui_auto::AdvancePointerAfterSync(held, false) && held.cycles == 0,
          "unobserved action syncs do not count as delivered input");
    Check(ui_auto::NeedsPrepressValidation(held), "an unobserved hold still rejects stale geometry");
    Check(!ui_auto::AdvancePointerAfterSync(held, true) && held.down,
          "the first observed held snapshot preserves the pending trigger lease");
    const bool carrierStillExists = false; // Application dismisses on that press.
    Check(!(ui_auto::NeedsPrepressValidation(held) && !carrierStillExists),
          "a delivered held Cancel must not later fail prepress because its dialog closed");
    int completions = 0;
    for (int cycle = 1; cycle < 45; ++cycle) {
        if (ui_auto::AdvancePointerAfterSync(held, true)) {
            ++completions;
            held.pending = false; // WriteUiResult retires reporting, not the held button.
        }
    }
    Check(completions == 1 && held.owned && held.down && !held.pending,
          "a closing Cancel reports once after the requested syncs while keeping the trigger held");
    Check(!ui_auto::AdvancePointerAfterSync(held, true) && !ui_auto::NeedsPrepressValidation(held),
          "a completed hold cannot produce another completion or prepress error");

    // A stale carrier before any bound action was published remains an error.
    auto unobserved = Pending("hold");
    for (int i = 0; i < 10; ++i) ui_auto::AdvancePointerAfterSync(unobserved, false);
    Check(ui_auto::NeedsPrepressValidation(unobserved) && !carrierStillExists,
          "disappearance before the first observed hold is still rejected");

    // Ordinary click dwell is deliberately unchanged: it revalidates right up
    // to the press, then holds/releases and reports after settling.
    auto click = Pending("click", 3);
    for (int i = 0; i < 2; ++i) {
        Check(!ui_auto::AdvancePointerAfterSync(click, true) && !click.down &&
              ui_auto::NeedsPrepressValidation(click), "click dwell keeps prepress validation armed");
    }
    Check(!ui_auto::AdvancePointerAfterSync(click, true) && click.down &&
          !ui_auto::NeedsPrepressValidation(click), "click validation retains its existing transition into the press phase");
    Check(!ui_auto::AdvancePointerAfterSync(click, true) && click.down, "click holds through its first pressed sync");
    Check(!ui_auto::AdvancePointerAfterSync(click, true) && !click.down, "click releases at the requested hold duration");
    Check(!ui_auto::AdvancePointerAfterSync(click, true), "click waits through its first release sync");
    Check(ui_auto::AdvancePointerAfterSync(click, true) && !click.down, "click reports success after two release syncs");

    auto hover = Pending("hover", 3);
    Check(!ui_auto::AdvancePointerAfterSync(hover, true) && ui_auto::NeedsPrepressValidation(hover),
          "hover geometry validation retains its existing dwell behavior");
    if (failures) return 1;
    std::puts("ui_pointer_lifecycle_tests: PASS");
    return 0;
}
