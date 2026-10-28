// Included by runtime.cpp after its pose/preview helpers. Callers hold g_mutex
// across input/capture access; each D3D12 slot carries its own immutable geometry.
#pragma once

static int ResolveSpaceHand(const rt::ActionSpace& space) {
    if (space.sourceMask==interaction_query::kLeftHandSource) return 0;
    if (space.sourceMask==interaction_query::kRightHandSource) return 1;
    auto action=rt::g_actions.find(space.action);
    if (action==rt::g_actions.end()) return -1;
    for (const auto& binding:action->second.bindings) {
        if (binding.profile!=rt::g_activeProfile) continue;
        if (binding.sourceMask==interaction_query::kLeftHandSource) return 0;
        if (binding.sourceMask==interaction_query::kRightHandSource) return 1;
    }
    return -1;
}

static XrPosef SpaceWorldPose(XrSpace space) {
    auto controller = rt::g_controllerSpaces.find(space);
    if (controller != rt::g_controllerSpaces.end()) {
        XrPosef pose;
        const int hand=ResolveSpaceHand(controller->second);
        if (hand<0) return ui_auto::Identity();
        rt::GetControllerPose(hand == 0 ? rt::g_leftController : rt::g_rightController, &pose);
        return ui_auto::Compose(pose, controller->second.poseInActionSpace);
    }
    auto ref = rt::g_referenceSpaces.find(space);
    if (ref == rt::g_referenceSpaces.end()) return ui_auto::Identity();
    if (ref->second.type == XR_REFERENCE_SPACE_TYPE_VIEW) {
        XrPosef head{rt::QuatFromYawPitchRoll(rt::g_headYaw,rt::g_headPitch,rt::g_headRoll),rt::g_headPos};
        return ui_auto::Compose(head,ref->second.poseInRef);
    }
    return ref->second.poseInRef;
}

static ui_auto::Frame BuildUiFrame(const XrFrameEndInfo* info, uint64_t frame) {
    using namespace ui_auto;
    auto& s=rt::g_session;
    Frame g;
    g.session=g_session; g.frame=frame; g.sync=g_sync; g.tick=GetTickCount64();
    g.width=(int)s.previewWidth; g.height=(int)s.previewHeight;
    if (s.usesD3D12 && s.previewRT12) {
        const auto desc=s.previewRT12->GetDesc(); g.width=(int)desc.Width; g.height=(int)desc.Height;
    }
    g.head={rt::QuatFromYawPitchRoll(rt::g_headYaw,rt::g_headPitch,rt::g_headRoll),rt::g_headPos};
    XrPosef hand;
    // Use the natural hand origin even when a previous pointer lease is active.
    for (int h=0;h<2;++h) {
        const bool owned=g_pointer[h].owned; g_pointer[h].owned=false;
        rt::GetControllerPose(h ? rt::g_rightController : rt::g_leftController,&hand);
        g_pointer[h].owned=owned; g.hands[h]=hand.position;
    }
    const auto viewMode=ui::g_uiState.viewMode;
    const auto layout=ui::g_uiState.displayLayout;
    g.layout=ui::DisplayLayoutName(layout);
    g.supported=!rt::g_poseSweepEnabled && (layout!=ui::DisplayLayout::Anaglyph || viewMode!=ui::ViewMode::BothEyes);
    const auto projectionRect=rt::ComputePresentRect(g.width,g.height);
    auto rect=projectionRect;
    // The D3D12/quad draw paths round their actual viewports to integral pixels.
    {
        rect.x=roundf(rect.x); rect.y=roundf(rect.y); rect.w=(std::max)(1.0f,roundf(rect.w)); rect.h=(std::max)(1.0f,roundf(rect.h));
    }
    for (int e=0;e<2;++e) {
        if (viewMode==ui::ViewMode::LeftEyeOnly && e==1) continue;
        if (viewMode==ui::ViewMode::RightEyeOnly && e==0) continue;
        Eye eye;
        eye.eye=e; eye.x=rect.x; eye.y=rect.y; eye.w=rect.w; eye.h=rect.h;
        if (viewMode==ui::ViewMode::BothEyes && layout!=ui::DisplayLayout::Anaglyph) {
            if (layout==ui::DisplayLayout::OverUnder) { eye.h=floorf(rect.h/2); eye.y+=e*eye.h; }
            else { eye.w=floorf(rect.w/2); eye.x+=e*eye.w; }
        }
        eye.pose=rt::ViewPoseFromAngles(e,rt::g_headYaw,rt::g_headPitch,rt::g_headRoll);
        eye.fov=rt::GetViewFov(e); g.eyes.push_back(eye);
    }
    if (!info) return g;
    for (uint32_t i=0;i<info->layerCount;++i) {
        const auto* layer=info->layers[i]; if (!layer) continue;
        if (layer->type==XR_TYPE_COMPOSITION_LAYER_PROJECTION) {
            if (!g.projectionEyes.empty()) continue; // Keep the world view; overlay projections are not UI surfaces.
            const auto& p=*(const XrCompositionLayerProjection*)layer;
            if (!p.views || p.viewCount>2) { g.supported=false; continue; }
            for (const auto& viewport:g.eyes) {
                if ((uint32_t)viewport.eye>=p.viewCount) continue;
                auto eye=viewport; const auto& v=p.views[eye.eye];
                if (!s.usesD3D12) {
                    eye.x=projectionRect.x; eye.y=projectionRect.y; eye.w=projectionRect.w; eye.h=projectionRect.h;
                    if (viewMode==ui::ViewMode::BothEyes) {
                        if (layout==ui::DisplayLayout::OverUnder) { eye.h*=.5f; eye.y+=eye.eye*eye.h; }
                        else if (layout==ui::DisplayLayout::SideBySide) { eye.w*=.5f; eye.x+=eye.eye*eye.w; }
                    }
                }
                eye.pose=Compose(SpaceWorldPose(p.space),v.pose); eye.fov=v.fov;
                if (ui::g_uiState.showFullRender) {
                    auto chain=rt::g_swapchains.find(v.subImage.swapchain);
                    if (chain!=rt::g_swapchains.end() && v.subImage.imageRect.extent.width>0 && v.subImage.imageRect.extent.height>0) {
                        eye.u0=-(float)v.subImage.imageRect.offset.x/v.subImage.imageRect.extent.width;
                        eye.v0=-(float)v.subImage.imageRect.offset.y/v.subImage.imageRect.extent.height;
                        eye.u1=eye.u0+(float)chain->second.width/v.subImage.imageRect.extent.width;
                        eye.v1=eye.v0+(float)chain->second.height/v.subImage.imageRect.extent.height;
                    }
                }
                g.projectionEyes.push_back(eye);
            }
        } else if (layer->type==XR_TYPE_COMPOSITION_LAYER_QUAD) {
            const auto& q=*(const XrCompositionLayerQuad*)layer;
            AddQuadSurface(g,(int)i,q,rt::QuadWorldPose(q,rt::g_headYaw,rt::g_headPitch,rt::g_headRoll));
        } else {
            g.supported=false;
        }
    }
    return g;
}

static void WriteUiResult(ui_auto::Pointer& p, bool success, const char* error="") {
    if (!ui_auto::ValidId(p.id)) return;
    if (!ui_auto::g_completedCommands.insert(p.id).second) {p.pending=false;return;}
    char result[2048];
    const int length=snprintf(result,sizeof(result),"{\"command_id\":\"%s\",\"session_id\":\"%s\",\"request_digest\":\"%s\",\"success\":%s,\"error\":\"%s\","
              "\"applied_frame\":%llu,\"frame_id\":%llu,\"sync_id\":%llu,\"button_down\":%s}\n",
            p.id.c_str(),p.session.empty() ? ui_auto::g_session.c_str():p.session.c_str(),p.requestDigest.c_str(),success ? "true":"false",error,p.appliedFrame,
            mcp::g_runtimeFrameCount.load(),ui_auto::g_sync,p.down ? "true":"false");
    if (length>0 && length<(int)sizeof(result))
        flicker::PublishRuntimeResult("ui_pointer_result."+p.id+".json",std::string(result,(size_t)length));
    p.pending=false;
}

static void ClearUiButtons(int hand) {
    auto& ctrl=hand ? rt::g_rightController:rt::g_leftController;
    ctrl.triggerPressed=ctrl.primaryPressed=ctrl.secondaryPressed=ctrl.menuPressed=false;
    ctrl.triggerValue=0; ctrl.thumbstick={0,0};
}

static void ReleaseUiPointers(const char* reason) {
    for (int hand=0;hand<2;++hand) {
        auto& p=ui_auto::g_pointer[hand];
        if (p.owned) ClearUiButtons(hand);
        p.down=false; p.owned=false;
        if (p.pending) WriteUiResult(p,false,reason);
    }
}

static void ResetUiAutomation() {
    ReleaseUiPointers("Session ended");
    static unsigned serial=0;
    ui_auto::g_session=std::to_string(GetCurrentProcessId())+"-"+std::to_string(GetTickCount64())+"-"+std::to_string(++serial);
    ui_auto::g_sync=0; ui_auto::g_frame={}; ui_auto::g_captures.clear();
    ui_auto::g_completedCommands.clear();
    for (auto& action:rt::g_actions) action.second.slots={};
}

static bool CompatibleCapture(const ui_auto::Frame& a, int surfaceId) {
    const auto& b=ui_auto::g_frame;
    const XrPosef head{rt::QuatFromYawPitchRoll(rt::g_headYaw,rt::g_headPitch,rt::g_headRoll),rt::g_headPos};
    if (!a.supported || rt::g_poseSweepEnabled || a.session!=ui_auto::g_session ||
        a.layout!=ui::DisplayLayoutName(ui::g_uiState.displayLayout) || !ui_auto::NearPose(a.head,head) ||
        a.width!=b.width || a.height!=b.height || a.eyes.size()!=b.eyes.size()) return false;
    for (size_t i=0;i<a.eyes.size();++i) {
        const auto& x=a.eyes[i]; const auto& y=b.eyes[i];
        if (x.eye!=y.eye || x.x!=y.x || x.y!=y.y || x.w!=y.w || x.h!=y.h ||
            !ui_auto::NearPose(x.pose,y.pose) || memcmp(&x.fov,&y.fov,sizeof(x.fov))!=0) return false;
        const auto fov=rt::GetViewFov(x.eye);
        if (memcmp(&x.fov,&fov,sizeof(fov))!=0) return false;
    }
    if (a.projectionEyes.size()!=b.projectionEyes.size()) return false;
    for (size_t i=0;i<a.projectionEyes.size();++i) {
        const auto& x=a.projectionEyes[i]; const auto& y=b.projectionEyes[i];
        if (x.eye!=y.eye || !ui_auto::NearPose(x.pose,y.pose) || memcmp(&x.fov,&y.fov,sizeof(x.fov)) ||
            x.u0!=y.u0 || x.v0!=y.v0 || x.u1!=y.u1 || x.v1!=y.v1) return false;
    }
    if (surfaceId>=0) {
        const ui_auto::Surface *x=nullptr,*y=nullptr;
        for (const auto& s:a.surfaces) if (s.id==surfaceId) x=&s;
        for (const auto& s:b.surfaces) if (s.id==surfaceId) y=&s;
        if (!x || !y || x->visibility!=y->visibility || x->width!=y->width || x->height!=y->height ||
            !ui_auto::NearPose(x->pose,y->pose)) return false;
    }
    return true;
}

static void PollUiPointerCommand() {
    using namespace ui_auto;
    static ULONGLONG lastPrune=0;
    const auto now=GetTickCount64();
    if (now-lastPrune>=500) {
        for (auto it=g_captures.begin();it!=g_captures.end();) {
            if (now-it->second.savedAt>30000) it=g_captures.erase(it); else ++it;
        }
        lastPrune=now;
    }
    // Expiry is checked even when the directory watcher has nothing to report.
    for (int h=0;h<2;++h) if (auto& p=g_pointer[h]; p.owned && (GetTickCount64()>p.expires || rt::g_session.state!=XR_SESSION_STATE_FOCUSED)) {
        ClearUiButtons(h);
        p.down=false; p.owned=false;
        if (p.pending) WriteUiResult(p,false,"Pointer lease expired or session lost focus");
    }
    for (int h=0;h<2;++h) {
        auto& p=g_pointer[h];
        if (!NeedsPrepressValidation(p)) continue;
        const auto capture=g_captures.find(p.captureId);
        if (capture==g_captures.end() || !CompatibleCapture(capture->second.geometry,p.surfaceId)) {
            ClearUiButtons(h); p.down=false; p.owned=false;
            WriteUiResult(p,false,"Camera/layout/surface changed before press; recapture");
        }
    }
    if (!mcp::g_commandsDue) return;
    const std::string path=mcp::GetSimulatorDataPath()+"\\ui_pointer_command.json";
    char buffer[4096]; if (!mcp::ReadPendingCommand(path.c_str(),buffer,sizeof(buffer))) return;
    json::Object o(buffer);
    Pointer incoming;
    incoming.id=o.string("command_id"); incoming.session=o.string("session_id"); incoming.action=o.string("action"); incoming.button=o.string("button","trigger");
    incoming.requestDigest=o.string("request_digest");
    if (!incoming.requestDigest.empty() && !ValidId(incoming.requestDigest)) return;
    if (!ValidId(incoming.id)) return;
    if (!ValidId(incoming.session)) { incoming.session.clear(); WriteUiResult(incoming,false,"Invalid session ID"); return; }
    // A replayed request must never activate twice, even after an MCP restart.
    if (g_completedCommands.count(incoming.id)) return;
    if (o.string("session_id")!=g_session) { WriteUiResult(incoming,false,"Session changed; recapture"); return; }
    const int hand=o.number("hand",1);
    if (hand<0 || hand>1) { WriteUiResult(incoming,false,"Invalid hand"); return; }
    auto& p=g_pointer[hand];
    if (incoming.action=="release") {
        if (p.owned) ClearUiButtons(hand);
        p.down=false; p.owned=false;
        if (p.pending) WriteUiResult(p,false,"Pointer released");
        WriteUiResult(incoming,true); return;
    }
    if (incoming.action!="hover" && incoming.action!="click" && incoming.action!="hold" && incoming.action!="scroll") { WriteUiResult(incoming,false,"Invalid action"); return; }
    if (incoming.button!="trigger" && incoming.button!="select" && incoming.button!="primary" && incoming.button!="secondary" && incoming.button!="menu") {
        WriteUiResult(incoming,false,"Invalid button"); return;
    }
    if (p.pending) {
        if (p.id==incoming.id) return;
        WriteUiResult(incoming,false,"Controller already has a pending interaction"); return;
    }
    if (p.owned && p.down && (incoming.action!="hold" || incoming.button!=p.button)) {
        WriteUiResult(incoming,false,"Release the held pointer before changing interaction or button"); return;
    }
    auto capture=g_captures.find(o.string("capture_id"));
    if (capture==g_captures.end() || GetTickCount64()-capture->second.savedAt>30000 ||
        !CompatibleCapture(capture->second.geometry,o.number("surface_id",-1))) {
        WriteUiResult(incoming,false,"Capture expired or camera/layout/surface changed; recapture"); return;
    }
    if (rt::g_session.state!=XR_SESSION_STATE_FOCUSED) { WriteUiResult(incoming,false,"Session is not focused"); return; }
    incoming.pose={{o.number("qx",0.0f),o.number("qy",0.0f),o.number("qz",0.0f),o.number("qw",1.0f)},
                   {o.number("px",0.0f),o.number("py",0.0f),o.number("pz",0.0f)}};
    const auto& q=incoming.pose.orientation; const auto& v=incoming.pose.position;
    const float norm=q.x*q.x+q.y*q.y+q.z*q.z+q.w*q.w;
    if (!std::isfinite(norm) || std::fabs(norm-1)>0.001f || !std::isfinite(v.x) || !std::isfinite(v.y) || !std::isfinite(v.z)) {
        WriteUiResult(incoming,false,"Invalid aim pose"); return;
    }
    incoming.stick={o.number("stick_x",0.f),o.number("stick_y",0.f)};
    if (!std::isfinite(incoming.stick.x) || !std::isfinite(incoming.stick.y) ||
        std::fabs(incoming.stick.x)>1.f || std::fabs(incoming.stick.y)>1.f) {
        WriteUiResult(incoming,false,"Invalid thumbstick vector"); return;
    }
    incoming.dwell=o.number("dwell_frames",2); incoming.hold=o.number("hold_frames",2);
    if (incoming.dwell<1 || incoming.dwell>90 || incoming.hold<1 || incoming.hold>90) {
        WriteUiResult(incoming,false,"Invalid sync-cycle duration"); return;
    }
    incoming.owned=true; incoming.pending=true;
    incoming.down=incoming.action=="hold";
    incoming.captureId=o.string("capture_id"); incoming.surfaceId=o.number("surface_id",-1);
    incoming.appliedFrame=mcp::g_runtimeFrameCount.load()+1;
    incoming.expires=GetTickCount64()+10000;
    p=std::move(incoming);
}

static void ApplyUiPointerButtons() {
    for (int hand=0;hand<2;++hand) {
        const auto& p=ui_auto::g_pointer[hand]; if (!p.owned) continue;
        auto& ctrl=hand ? rt::g_rightController : rt::g_leftController;
        ctrl.triggerPressed=p.down && (p.button=="trigger" || p.button=="select");
        ctrl.triggerValue=ctrl.triggerPressed ? 1.0f:0.0f;
        ctrl.primaryPressed=p.down && p.button=="primary";
        ctrl.secondaryPressed=p.down && p.button=="secondary";
        ctrl.menuPressed=p.down && p.button=="menu";
        ctrl.gripPressed=ctrl.thumbstickPressed=false;
        ctrl.gripValue=0; ctrl.thumbstick=ui_auto::PointerThumbstick(p);
    }
}

static void AdvanceUiPointersAfterSync(const bool observed[2]) {
    for (int hand=0;hand<2;++hand) {
        auto& p=ui_auto::g_pointer[hand];
        if (ui_auto::AdvancePointerAfterSync(p,observed[hand])) WriteUiResult(p,true);
    }
}
