// Bound-source evaluation and stable xrSyncActions snapshots. Included by runtime.cpp.
#pragma once

static int HandForPath(XrPath path) {
    auto it=rt::g_pathStrings.find(path);
    if (it==rt::g_pathStrings.end()) return -1;
    if (it->second.find("/user/hand/left")==0) return 0;
    if (it->second.find("/user/hand/right")==0) return 1;
    return -1;
}

static rt::ActionSnapshot EvaluateBoundAction(XrAction action, int hand) {
    rt::ActionSnapshot value;
    const auto& ctrl=hand ? rt::g_rightController : rt::g_leftController;
    const auto type=rt::g_actionInfo.at(action).type;
    auto bindings=rt::g_bindings.find(rt::g_activeProfile);
    if (bindings==rt::g_bindings.end()) return value;
    for (const auto& b:bindings->second) {
        if (b.action!=action || HandForPath(b.binding)!=hand) continue;
        const auto& path=rt::g_pathStrings.at(b.binding);
        value.active=ctrl.isTracking;
        float source=0;
        if (path.find("/input/trigger/")!=std::string::npos || path.find("/input/select/")!=std::string::npos)
            source=ctrl.triggerValue;
        else if (path.find("/input/squeeze/")!=std::string::npos) source=ctrl.gripValue;
        else if (path.find("/input/menu/")!=std::string::npos) source=ctrl.menuPressed ? 1.0f:0.0f;
        else if (path.find("/input/a/")!=std::string::npos || path.find("/input/x/")!=std::string::npos)
            source=ctrl.primaryPressed ? 1.0f:0.0f;
        else if (path.find("/input/b/")!=std::string::npos || path.find("/input/y/")!=std::string::npos)
            source=ctrl.secondaryPressed ? 1.0f:0.0f;
        else if (path.find("/input/thumbstick")!=std::string::npos || path.find("/input/trackpad")!=std::string::npos) {
            source=ctrl.thumbstickPressed ? 1.0f:0.0f; value.vector=ctrl.thumbstick;
        }
        if (type==XR_ACTION_TYPE_BOOLEAN_INPUT) source=source>=0.5f ? 1.0f:0.0f;
        value.value=(std::max)(value.value,source);
    }
    return value;
}

static rt::ActionSnapshot ReadActionSnapshot(const XrActionStateGetInfo* info) {
    std::lock_guard<std::recursive_mutex> lock(ui_auto::g_mutex);
    auto it=rt::g_actionSnapshots.find(info->action);
    if (it==rt::g_actionSnapshots.end()) return {};
    const int hand=HandForPath(info->subactionPath);
    return it->second[hand<0 ? 0:hand+1];
}

static XrResult XRAPI_PTR xrGetActionStateBoolean_runtime(XrSession, const XrActionStateGetInfo* info, XrActionStateBoolean* state) {
    if (!info || !state) return XR_ERROR_VALIDATION_FAILURE;
    const auto v=ReadActionSnapshot(info);
    state->type=XR_TYPE_ACTION_STATE_BOOLEAN; state->currentState=v.value>=0.5f;
    state->isActive=v.active; state->changedSinceLastSync=v.changed; state->lastChangeTime=v.changedAt;
    return XR_SUCCESS;
}
static XrResult XRAPI_PTR xrGetActionStateFloat_runtime(XrSession, const XrActionStateGetInfo* info, XrActionStateFloat* state) {
    if (!info || !state) return XR_ERROR_VALIDATION_FAILURE;
    const auto v=ReadActionSnapshot(info);
    state->type=XR_TYPE_ACTION_STATE_FLOAT; state->currentState=v.value;
    state->isActive=v.active; state->changedSinceLastSync=v.changed; state->lastChangeTime=v.changedAt;
    return XR_SUCCESS;
}
static XrResult XRAPI_PTR xrGetActionStatePose_runtime(XrSession, const XrActionStateGetInfo* info, XrActionStatePose* state) {
    if (!info || !state) return XR_ERROR_VALIDATION_FAILURE;
    state->type=XR_TYPE_ACTION_STATE_POSE; state->isActive=ReadActionSnapshot(info).active;
    return XR_SUCCESS;
}
static XrResult XRAPI_PTR xrGetActionStateVector2f_runtime(XrSession, const XrActionStateGetInfo* info, XrActionStateVector2f* state) {
    if (!info || !state) return XR_ERROR_VALIDATION_FAILURE;
    const auto v=ReadActionSnapshot(info);
    state->type=XR_TYPE_ACTION_STATE_VECTOR2F; state->currentState=v.vector;
    state->isActive=v.active; state->changedSinceLastSync=v.changed; state->lastChangeTime=v.changedAt;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrSyncActions_runtime(XrSession, const XrActionsSyncInfo* info) {
    std::lock_guard<std::recursive_mutex> lock(ui_auto::g_mutex);
    if (!info || (info->countActiveActionSets && !info->activeActionSets)) return XR_ERROR_VALIDATION_FAILURE;
    ++ui_auto::g_sync;
    // Input commands are also serviced for applications that sync without waiting a frame.
    mcp::RefreshCommandsDue(); PollUiPointerCommand(); ApplyUiPointerButtons();
    const bool focused=rt::g_session.state==XR_SESSION_STATE_FOCUSED;
    LARGE_INTEGER tick,frequency; QueryPerformanceCounter(&tick); QueryPerformanceFrequency(&frequency);
    const XrTime now=(XrTime)((double)tick.QuadPart*1e9/(double)frequency.QuadPart);
    for (const auto& entry:rt::g_actionInfo) {
        auto& previous=rt::g_actionSnapshots[entry.first];
        std::array<rt::ActionSnapshot,3> next{};
        for (int hand=0;hand<2;++hand) {
            bool enabled=false;
            for (uint32_t i=0;i<info->countActiveActionSets;++i) {
                const auto& active=info->activeActionSets[i];
                if (active.actionSet==entry.second.set && (active.subactionPath==XR_NULL_PATH || HandForPath(active.subactionPath)==hand)) enabled=true;
            }
            if (focused && enabled) next[hand+1]=EvaluateBoundAction(entry.first,hand);
        }
        next[0].active=next[1].active || next[2].active;
        const int dominant=next[1].value>next[2].value ? 1:2;
        next[0].value=next[dominant].value;
        const auto magnitude=[](XrVector2f v) { return v.x*v.x+v.y*v.y; };
        next[0].vector=magnitude(next[1].vector)>magnitude(next[2].vector) ? next[1].vector:next[2].vector;
        for (int slot=0;slot<3;++slot) {
            auto& n=next[slot]; const auto& p=previous[slot];
            n.changed=n.active && (n.value!=p.value || n.vector.x!=p.vector.x || n.vector.y!=p.vector.y);
            n.changedAt=n.changed ? now:p.changedAt;
        }
        previous=next;
    }
    bool observed[2]{};
    for (int hand=0;hand<2;++hand) {
        const auto& p=ui_auto::g_pointer[hand];
        if (!p.pending) continue;
        for (const auto& b:rt::g_bindings[rt::g_activeProfile]) {
            if (HandForPath(b.binding)!=hand || !rt::g_actionSnapshots[b.action][hand+1].active) continue;
            const auto& path=rt::g_pathStrings[b.binding];
            if (p.action=="hover") {
                if (path.find("/input/aim/pose")!=std::string::npos) observed[hand]=true;
            } else if ((p.button=="primary" && (path.find("/input/a/click")!=std::string::npos || path.find("/input/x/click")!=std::string::npos)) ||
                       (p.button=="secondary" && (path.find("/input/b/click")!=std::string::npos || path.find("/input/y/click")!=std::string::npos)) ||
                       (p.button=="menu" && path.find("/input/menu/click")!=std::string::npos) ||
                       ((p.button=="trigger" || p.button=="select") && (path.find("/input/trigger/")!=std::string::npos || path.find("/input/select/")!=std::string::npos))) observed[hand]=true;
        }
    }
    if (focused) AdvanceUiPointersAfterSync(observed);
    return focused ? XR_SUCCESS:XR_SESSION_NOT_FOCUSED;
}
