// Application-side UI oracle. No runtime targeting helpers are used to hit-test.
#pragma once

struct UiFixture {
    bool hovered=false, down=false, previousDown=false;
    int clicks=0, pressEdges=0, releaseEdges=0, hoverFrames=0, syncErrors=0;
    float hitX=0,hitY=0;
    const char* data=getenv("SIMXR_DATA_DIR");
    XrPosef pose{{0,sinf(0.11f),0,cosf(0.11f)},{0.32f,1.52f,-2.0f}};
    XrExtent2Df size{1.6f,0.9f};

    void CheckSpaceTransforms(XrSession session, XrSpace aim, XrSpace stage, XrTime time, XrAction action, XrPath hand) {
        // Known 90-degree base transform and nonzero action/reference offsets.
        // This oracle checks the actual XR API, independently of runtime helpers.
        const float s=std::sqrt(.5f);
        XrReferenceSpaceCreateInfo ref{XR_TYPE_REFERENCE_SPACE_CREATE_INFO}; ref.referenceSpaceType=XR_REFERENCE_SPACE_TYPE_STAGE;
        ref.poseInReferenceSpace={{0,s,0,s},{.7f,-.2f,.4f}};
        XrSpace base{},offset{},view{},shiftedView{};
        if (XR_FAILED(xrCreateReferenceSpace(session,&ref,&base))) { ++syncErrors; return; }
        XrActionSpaceCreateInfo a{XR_TYPE_ACTION_SPACE_CREATE_INFO}; a.action=action; a.subactionPath=hand;
        a.poseInActionSpace={{0,0,0,1},{.03f,.02f,-.04f}};
        if (XR_FAILED(xrCreateActionSpace(session,&a,&offset))) { ++syncErrors; xrDestroySpace(base); return; }
        XrSpaceLocation world{XR_TYPE_SPACE_LOCATION},relative{XR_TYPE_SPACE_LOCATION};
        xrLocateSpace(aim,stage,time,&world); xrLocateSpace(offset,base,time,&relative);
        const auto& q=world.pose.orientation;
        const float x=.03f,y=.02f,z=-.04f;
        const float wx=world.pose.position.x+(1-2*(q.y*q.y+q.z*q.z))*x+2*(q.x*q.y-q.z*q.w)*y+2*(q.x*q.z+q.y*q.w)*z;
        const float wy=world.pose.position.y+2*(q.x*q.y+q.z*q.w)*x+(1-2*(q.x*q.x+q.z*q.z))*y+2*(q.y*q.z-q.x*q.w)*z;
        const float wz=world.pose.position.z+2*(q.x*q.z-q.y*q.w)*x+2*(q.y*q.z+q.x*q.w)*y+(1-2*(q.x*q.x+q.y*q.y))*z;
        auto nearValue=[](float a,float b) { return std::fabs(a-b)<.0001f; };
        if (!nearValue(relative.pose.position.x,-(wz-.4f)) || !nearValue(relative.pose.position.y,wy+.2f) || !nearValue(relative.pose.position.z,wx-.7f)) ++syncErrors;
        const XrQuaternionf expected{s*(q.x-q.z),s*(q.y-q.w),s*(q.z+q.x),s*(q.w+q.y)};
        const auto& actual=relative.pose.orientation;
        if (!nearValue(std::fabs(expected.x*actual.x+expected.y*actual.y+expected.z*actual.z+expected.w*actual.w),1)) ++syncErrors;
        ref.referenceSpaceType=XR_REFERENCE_SPACE_TYPE_VIEW; ref.poseInReferenceSpace={{0,0,0,1},{0,0,0}};
        xrCreateReferenceSpace(session,&ref,&view);
        ref.poseInReferenceSpace.position={.13f,.02f,0}; xrCreateReferenceSpace(session,&ref,&shiftedView);
        XrSpaceLocation head{XR_TYPE_SPACE_LOCATION},shifted{XR_TYPE_SPACE_LOCATION};
        xrLocateSpace(view,stage,time,&head); xrLocateSpace(shiftedView,view,time,&shifted);
        if (!nearValue(shifted.pose.position.x,.13f) || !nearValue(shifted.pose.position.y,.02f) || !nearValue(shifted.pose.position.z,0)) ++syncErrors;
        XrViewLocateInfo locate{XR_TYPE_VIEW_LOCATE_INFO}; locate.viewConfigurationType=XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO; locate.displayTime=time;
        XrViewState state{XR_TYPE_VIEW_STATE}; XrView absolute[2]{{XR_TYPE_VIEW},{XR_TYPE_VIEW}},rebased[2]{{XR_TYPE_VIEW},{XR_TYPE_VIEW}};
        uint32_t count=2; locate.space=stage; xrLocateViews(session,&locate,&state,2,&count,absolute);
        locate.space=base; xrLocateViews(session,&locate,&state,2,&count,rebased);
        for (int e=0;e<2;++e) {
            if (!nearValue(rebased[e].pose.position.x,-(absolute[e].pose.position.z-.4f)) ||
                !nearValue(rebased[e].pose.position.y,absolute[e].pose.position.y+.2f) ||
                !nearValue(rebased[e].pose.position.z,absolute[e].pose.position.x-.7f)) ++syncErrors;
        }
        for (auto space:{base,offset,view,shiftedView}) if (space) xrDestroySpace(space);
    }

    void Update(XrSession session, XrSpace aimSpace, XrSpace stage, XrTime time, XrAction select, XrPath hand, XrAction aimAction) {
        static bool checked=false;
        if (!checked) { CheckSpaceTransforms(session,aimSpace,stage,time,aimAction,hand); checked=true; }
        XrSpaceLocation loc{XR_TYPE_SPACE_LOCATION};
        XrActionStateGetInfo get{XR_TYPE_ACTION_STATE_GET_INFO}; get.action=select; get.subactionPath=hand;
        XrActionStateBoolean state{XR_TYPE_ACTION_STATE_BOOLEAN}, again{XR_TYPE_ACTION_STATE_BOOLEAN};
        if (XR_FAILED(xrLocateSpace(aimSpace,stage,time,&loc)) || XR_FAILED(xrGetActionStateBoolean(session,&get,&state))) { ++syncErrors; return; }
        xrGetActionStateBoolean(session,&get,&again);
        if (state.currentState!=again.currentState || state.changedSinceLastSync!=again.changedSinceLastSync || state.lastChangeTime!=again.lastChangeTime) ++syncErrors;
        const auto& q=loc.pose.orientation;
        const float dx=-2*(q.x*q.z+q.w*q.y),dy=-2*(q.y*q.z-q.w*q.x),dz=-(1-2*(q.x*q.x+q.y*q.y));
        const float c=cosf(0.22f),s=sinf(0.22f);
        const auto& p=loc.pose.position;
        const float ox=c*(p.x-pose.position.x)-s*(p.z-pose.position.z);
        const float oy=p.y-pose.position.y;
        const float oz=s*(p.x-pose.position.x)+c*(p.z-pose.position.z);
        const float vx=c*dx-s*dz, vz=s*dx+c*dz;
        const float t=std::fabs(vz)>1e-6f ? -oz/vz:-1;
        hitX=ox+t*vx; hitY=oy+t*dy;
        // The drawn button occupies u=[.60,.85], v=[.35,.65] of the UI texture.
        hovered=t>0 && hitX>=size.width*0.10f && hitX<=size.width*0.35f && std::fabs(hitY)<=size.height*0.15f;
        hoverFrames=hovered ? hoverFrames+1:0;
        down=state.isActive && state.currentState;
        if (down!=previousDown) {
            if (!state.changedSinceLastSync || state.lastChangeTime<=0) ++syncErrors;
            if (down) { ++pressEdges; if (hovered) ++clicks; }
            else ++releaseEdges;
        } else if (state.changedSinceLastSync) ++syncErrors;
        previousDown=down;
    }
    void Draw(ID3D12GraphicsCommandList* cmd, D3D12_CPU_DESCRIPTOR_HANDLE rtv, const XrRect2Di& r) {
        D3D12_RECT all{r.offset.x,r.offset.y,r.offset.x+r.extent.width,r.offset.y+r.extent.height};
        const float bg[4]{0.025f,0.03f,0.04f,1}; cmd->ClearRenderTargetView(rtv,bg,1,&all);
        const float idle[4]{0.05f,0.25f,0.9f,1},hover[4]{0.95f,0.65f,0.02f,1},done[4]{0.05f,0.8f,0.15f,1};
        D3D12_RECT button{r.offset.x+(LONG)(r.extent.width*.60f),r.offset.y+(LONG)(r.extent.height*.35f),
                            r.offset.x+(LONG)(r.extent.width*.85f),r.offset.y+(LONG)(r.extent.height*.65f)};
        cmd->ClearRenderTargetView(rtv,clicks ? done:hovered ? hover:idle,1,&button);
        if (hoverFrames>=8) {
            const float tip[4]{0.8f,0.8f,0.8f,1};
            D3D12_RECT tooltip=button; tooltip.bottom=button.top-20; tooltip.top=tooltip.bottom-30;
            cmd->ClearRenderTargetView(rtv,tip,1,&tooltip);
        }
    }
    void Write(int frame) const {
        if (!data) return;
        const std::string path=std::string(data)+"\\ui_fixture.json",tmp=path+".tmp";
        FILE* f=nullptr; if (fopen_s(&f,tmp.c_str(),"w") || !f) return;
        fprintf(f,"{\"frame\":%d,\"hovered\":%s,\"hover_frames\":%d,\"button_down\":%s,\"clicks\":%d,\"press_edges\":%d,\"release_edges\":%d,\"sync_errors\":%d,\"hit\":[%.8f,%.8f]}\n",
                frame,hovered ? "true":"false",hoverFrames,down ? "true":"false",clicks,pressEdges,releaseEdges,syncErrors,hitX,hitY);
        fclose(f); MoveFileExA(tmp.c_str(),path.c_str(),MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH);
    }
};
