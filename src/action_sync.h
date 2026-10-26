// Automation delivery observes the validated, latched OpenXR action state.
#pragma once

static int HandForPath(XrPath path) {
    auto it=rt::g_pathStrings.find(path);
    if (it==rt::g_pathStrings.end()) return -1;
    if (it->second.find("/user/hand/left")==0) return 0;
    if (it->second.find("/user/hand/right")==0) return 1;
    return -1;
}

// Acknowledge only pointer inputs consumed by the action snapshots in this sync.
static void ObserveUiPointerSync() {
    bool observed[2]{};
    for (int hand=0;hand<2;++hand) {
        const auto& p=ui_auto::g_pointer[hand];
        if (!p.pending) continue;
        for (const auto& entry:rt::g_actions) {
            const auto& action=entry.second;
            const auto& slot=action.slots[hand+1];
            const bool active=slot.poseActive || slot.boolean.active || slot.scalar.active || slot.vector.active;
            if (!active) continue;
            for (const auto& binding:action.bindings) {
                if (binding.profile!=rt::g_activeProfile || HandForPath(binding.sourcePath)!=hand) continue;
                const auto& path=rt::g_pathStrings.at(binding.sourcePath);
                if (p.action=="hover") {
                    if (path.find("/input/aim/pose")!=std::string::npos) observed[hand]=true;
                } else if (p.action=="scroll") {
                    if (path.find("/input/thumbstick")!=std::string::npos && path.find("/click")==std::string::npos) observed[hand]=true;
                } else if ((p.button=="primary" && (path.find("/input/a/click")!=std::string::npos || path.find("/input/x/click")!=std::string::npos)) ||
                           (p.button=="secondary" && (path.find("/input/b/click")!=std::string::npos || path.find("/input/y/click")!=std::string::npos)) ||
                           (p.button=="menu" && path.find("/input/menu/click")!=std::string::npos) ||
                           ((p.button=="trigger" || p.button=="select") && (path.find("/input/trigger/")!=std::string::npos || path.find("/input/select/")!=std::string::npos))) observed[hand]=true;
            }
        }
    }
    AdvanceUiPointersAfterSync(observed);
}
