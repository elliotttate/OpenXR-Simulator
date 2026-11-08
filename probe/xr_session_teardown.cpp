// Teardown regression: after xrDestroySession + xrDestroyInstance the runtime must hold no reference
// to the application's D3D11 device. A leftover device child (blit shaders, states, the preview
// swapchain) keeps the app's device alive past its own final Release, and the runtime's static
// Session then destroys it from DllMain at process exit, where the driver's threads are already
// gone (FlatOut VR: second-chance AV in nvwgf2umx under LdrShutdownProcess).
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define XR_NO_PROTOTYPES
#define XR_USE_PLATFORM_WIN32
#define XR_USE_GRAPHICS_API_D3D11
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_4.h>
#include <wrl/client.h>
#include <cstdio>
#include <cstring>
#include <vector>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
#include "direct_runtime.h"
using Microsoft::WRL::ComPtr;
#define CHECK(c) do { auto result=(c); if(XR_FAILED(result)){printf("FAIL %s: %d\n",#c,int(result));return 1;} } while(0)

static ULONG RefCount(IUnknown* object) { object->AddRef(); return object->Release(); }

static int RunSession(XrInstance instance, XrSystemId system, ID3D11Device* device, ID3D11DeviceContext* context) {
    XrGraphicsBindingD3D11KHR binding{XR_TYPE_GRAPHICS_BINDING_D3D11_KHR}; binding.device=device;
    XrSessionCreateInfo sci{XR_TYPE_SESSION_CREATE_INFO}; sci.systemId=system; sci.next=&binding;
    XrSession session; CHECK(xrCreateSession(instance,&sci,&session));
    XrReferenceSpaceCreateInfo ri{XR_TYPE_REFERENCE_SPACE_CREATE_INFO}; ri.referenceSpaceType=XR_REFERENCE_SPACE_TYPE_LOCAL; ri.poseInReferenceSpace.orientation.w=1;
    XrSpace space; CHECK(xrCreateReferenceSpace(session,&ri,&space));
    XrSwapchain chains[2]{}; std::vector<XrSwapchainImageD3D11KHR> images[2];
    for (int c=0; c<2; ++c) {
        XrSwapchainCreateInfo s{XR_TYPE_SWAPCHAIN_CREATE_INFO}; s.usageFlags=XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT|XR_SWAPCHAIN_USAGE_SAMPLED_BIT;
        s.format=DXGI_FORMAT_R8G8B8A8_UNORM_SRGB; s.width=s.height=128; s.arraySize=c?1:2; s.sampleCount=s.faceCount=s.mipCount=1;
        CHECK(xrCreateSwapchain(session,&s,&chains[c])); uint32_t n=0; CHECK(xrEnumerateSwapchainImages(chains[c],0,&n,nullptr));
        images[c].resize(n,{XR_TYPE_SWAPCHAIN_IMAGE_D3D11_KHR}); CHECK(xrEnumerateSwapchainImages(chains[c],n,&n,(XrSwapchainImageBaseHeader*)images[c].data()));
    }
    for (bool running=false; !running;) {
        XrEventDataBuffer e{XR_TYPE_EVENT_DATA_BUFFER};
        if (xrPollEvent(instance,&e)==XR_SUCCESS && e.type==XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED &&
            ((XrEventDataSessionStateChanged*)&e)->state==XR_SESSION_STATE_READY) {
            XrSessionBeginInfo b{XR_TYPE_SESSION_BEGIN_INFO}; b.primaryViewConfigurationType=XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;
            CHECK(xrBeginSession(session,&b)); running=true;
        }
    }
    std::vector<unsigned char> pixels(128*128*4,200);
    for (int f=0; f<30; ++f) {
        XrFrameState frame{XR_TYPE_FRAME_STATE}; CHECK(xrWaitFrame(session,nullptr,&frame)); CHECK(xrBeginFrame(session,nullptr));
        XrViewLocateInfo li{XR_TYPE_VIEW_LOCATE_INFO}; li.viewConfigurationType=XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO; li.displayTime=frame.predictedDisplayTime; li.space=space;
        XrViewState vs{XR_TYPE_VIEW_STATE}; XrView views[2]{{XR_TYPE_VIEW},{XR_TYPE_VIEW}}; uint32_t count=0; CHECK(xrLocateViews(session,&li,&vs,2,&count,views));
        for (int c=0; c<2; ++c) {
            uint32_t ix=0; CHECK(xrAcquireSwapchainImage(chains[c],nullptr,&ix));
            XrSwapchainImageWaitInfo wi{XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO}; wi.timeout=XR_INFINITE_DURATION; CHECK(xrWaitSwapchainImage(chains[c],&wi));
            for (UINT slice=0; slice<(c?1u:2u); ++slice) context->UpdateSubresource(images[c][ix].texture,slice,nullptr,pixels.data(),128*4,0);
            CHECK(xrReleaseSwapchainImage(chains[c],nullptr));
        }
        XrCompositionLayerProjectionView pv[2]{};
        for (int e=0; e<2; ++e) { pv[e].type=XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW; pv[e].pose=views[e].pose; pv[e].fov=views[e].fov; pv[e].subImage={chains[0],{{0,0},{128,128}},uint32_t(e)}; }
        XrCompositionLayerProjection projection{XR_TYPE_COMPOSITION_LAYER_PROJECTION}; projection.space=space; projection.viewCount=2; projection.views=pv;
        XrCompositionLayerQuad quad{XR_TYPE_COMPOSITION_LAYER_QUAD}; quad.space=space; quad.eyeVisibility=XR_EYE_VISIBILITY_BOTH;
        quad.layerFlags=XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT; quad.pose={{0,0,0,1},{0,1.7f,-2}}; quad.size={1,1}; quad.subImage={chains[1],{{0,0},{128,128}},0};
        const XrCompositionLayerBaseHeader* layers[]{(XrCompositionLayerBaseHeader*)&projection,(XrCompositionLayerBaseHeader*)&quad};
        XrFrameEndInfo end{XR_TYPE_FRAME_END_INFO}; end.displayTime=frame.predictedDisplayTime; end.environmentBlendMode=XR_ENVIRONMENT_BLEND_MODE_OPAQUE; end.layerCount=2; end.layers=layers;
        CHECK(xrEndFrame(session,&end));
    }
    CHECK(xrRequestExitSession(session)); CHECK(xrEndSession(session));
    for (auto chain : chains) CHECK(xrDestroySwapchain(chain));
    CHECK(xrDestroySpace(space)); CHECK(xrDestroySession(session));
    return 0;
}

int main() {
    setvbuf(stdout,nullptr,_IONBF,0);
    if (!InitDirectRuntime()) return 1;
    const char* ext=XR_KHR_D3D11_ENABLE_EXTENSION_NAME;
    XrInstanceCreateInfo ci{XR_TYPE_INSTANCE_CREATE_INFO};
    strcpy_s(ci.applicationInfo.applicationName,"Session teardown regression");
    ci.applicationInfo.apiVersion=XR_MAKE_VERSION(1,0,0); ci.enabledExtensionCount=1; ci.enabledExtensionNames=&ext;
    XrInstance instance; CHECK(xrCreateInstance(&ci,&instance));
    XrSystemGetInfo si{XR_TYPE_SYSTEM_GET_INFO}; si.formFactor=XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY;
    XrSystemId system; CHECK(xrGetSystem(instance,&si,&system));
    PFN_xrGetD3D11GraphicsRequirementsKHR requirements=nullptr;
    CHECK(xrGetInstanceProcAddr(instance,"xrGetD3D11GraphicsRequirementsKHR",(PFN_xrVoidFunction*)&requirements));
    XrGraphicsRequirementsD3D11KHR req{XR_TYPE_GRAPHICS_REQUIREMENTS_D3D11_KHR}; CHECK(requirements(instance,system,&req));
    ComPtr<IDXGIFactory4> factory; ComPtr<IDXGIAdapter> adapter;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory))) || FAILED(factory->EnumAdapterByLuid(req.adapterLuid,IID_PPV_ARGS(&adapter)))) return 1;

    // Two sessions on two devices, as an app that recreates its renderer does: the second session must
    // not inherit device children of the first, and neither device may stay referenced at the end.
    ComPtr<ID3D11Device> devices[2]; ComPtr<ID3D11DeviceContext> contexts[2]; ULONG before[2]{};
    int failures=0;
    for (int d=0; d<2; ++d) {
        if (FAILED(D3D11CreateDevice(adapter.Get(),D3D_DRIVER_TYPE_UNKNOWN,nullptr,0,nullptr,0,D3D11_SDK_VERSION,&devices[d],nullptr,&contexts[d]))) return 1;
        before[d]=RefCount(devices[d].Get());
        if (RunSession(instance,system,devices[d].Get(),contexts[d].Get())) return 1;
        contexts[d]->ClearState(); contexts[d]->Flush();
        if (d==0) {
            // Between sessions the runtime may keep only its preview swapchain (one per window, by design).
            const ULONG between=RefCount(devices[0].Get());
            printf("device 0 after xrDestroySession: %lu references (before xrCreateSession: %lu)\n",between,before[0]);
        }
    }
    const ULONG afterSession1=RefCount(devices[1].Get());
    printf("device 1 after xrDestroySession: %lu references (before xrCreateSession: %lu)\n",afterSession1,before[1]);
    const ULONG staleOld=RefCount(devices[0].Get());
    if (staleOld!=before[0]) { printf("FAIL device 0 still referenced by the runtime after a later session: %lu, expected %lu\n",staleOld,before[0]); ++failures; }
    CHECK(xrDestroyInstance(instance));
    for (int d=0; d<2; ++d) {
        const ULONG after=RefCount(devices[d].Get());
        printf("device %d after xrDestroyInstance: %lu references (before xrCreateSession: %lu)\n",d,after,before[d]);
        if (after!=before[d]) { printf("FAIL device %d: the runtime holds %ld reference(s) after xrDestroyInstance\n",d,long(after)-long(before[d])); ++failures; }
    }
    // Instance recreate: a new instance and session on a surviving device start clean and end clean too.
    {
        XrInstance again; CHECK(xrCreateInstance(&ci,&again));
        XrSystemId system2; CHECK(xrGetSystem(again,&si,&system2));
        XrGraphicsRequirementsD3D11KHR req2{XR_TYPE_GRAPHICS_REQUIREMENTS_D3D11_KHR};
        CHECK(xrGetInstanceProcAddr(again,"xrGetD3D11GraphicsRequirementsKHR",(PFN_xrVoidFunction*)&requirements)); CHECK(requirements(again,system2,&req2));
        if (RunSession(again,system2,devices[1].Get(),contexts[1].Get())) return 1;
        contexts[1]->ClearState(); contexts[1]->Flush();
        CHECK(xrDestroyInstance(again));
        const ULONG after=RefCount(devices[1].Get());
        printf("device 1 after a second instance and session: %lu references (before: %lu)\n",after,before[1]);
        if (after!=before[1]) { printf("FAIL device 1: the runtime holds %ld reference(s) after the second instance\n",long(after)-long(before[1])); ++failures; }
    }
    printf(failures?"RESULT FAIL\n":"RESULT PASS\n");
    return failures?1:0;
}
