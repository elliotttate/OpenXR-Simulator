#include "command_epochs.h"
#include <cstdio>

#define CHECK(expression) do { ++checks; if (!(expression)) { \
    std::fprintf(stderr, "Command wakeup check failed at line %d\n", __LINE__); return 1; \
} } while (0)

int main() {
    int checks = 0;
    mcp::CommandEpoch commands;
    std::uint64_t controls = 0, render = 0;
    CHECK(commands.ChangedSince(controls));
    CHECK(commands.ChangedSince(render));
    CHECK(!commands.ChangedSince(controls));

    // A command arrives after controls ran. Rendering consumes the shared
    // notification first; controls must still wake on the following frame.
    commands.Notify();
    CHECK(commands.ChangedSince(render));
    CHECK(!commands.ChangedSince(render));
    CHECK(commands.ChangedSince(controls));
    CHECK(!commands.ChangedSince(controls));

    // A notification during processing survives until that consumer's next turn.
    commands.Notify();
    CHECK(commands.ChangedSince(controls));
    commands.Notify();
    CHECK(commands.ChangedSince(controls));
    CHECK(commands.ChangedSince(render));
    CHECK(!commands.ChangedSince(render));
    std::printf("command_epochs_tests: %d checks passed\n", checks);
}
