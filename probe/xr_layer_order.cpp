// Real GPU regression fixture: world, menu quad, stereo foreground, then a quad.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define XR_NO_PROTOTYPES
#define XR_USE_PLATFORM_WIN32
#define XR_USE_GRAPHICS_API_D3D11
#define XR_USE_GRAPHICS_API_D3D12
#include <windows.h>
#include <d3d11.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <wrl/client.h>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <vector>
#include <string>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
#include "direct_runtime.h"
using Microsoft::WRL::ComPtr;
#define CHECK(c) do { auto result=(c); if(XR_FAILED(result)){printf("FAIL %s: %d\n",#c,int(result));return 1;} } while(0)

int main() {
    setvbuf(stdout,nullptr,_IONBF,0);
    if (!InitDirectRuntime()) return 1;
    const bool d12=getenv("XR_UI_BACKEND") && strcmp(getenv("XR_UI_BACKEND"),"d3d12")==0;
    const char* ext=d12?XR_KHR_D3D12_ENABLE_EXTENSION_NAME:XR_KHR_D3D11_ENABLE_EXTENSION_NAME;
    XrInstanceCreateInfo ci{XR_TYPE_INSTANCE_CREATE_INFO};
    strcpy_s(ci.applicationInfo.applicationName,"Stereo layer order regression");
    ci.applicationInfo.apiVersion=XR_MAKE_VERSION(1,0,0);ci.enabledExtensionCount=1;ci.enabledExtensionNames=&ext;
    XrInstance instance;CHECK(xrCreateInstance(&ci,&instance));
    XrSystemGetInfo si{XR_TYPE_SYSTEM_GET_INFO};si.formFactor=XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY;
    XrSystemId system;CHECK(xrGetSystem(instance,&si,&system));
    LUID luid{};
    if (d12) {
        PFN_xrGetD3D12GraphicsRequirementsKHR requirements=nullptr;
        CHECK(xrGetInstanceProcAddr(instance,"xrGetD3D12GraphicsRequirementsKHR",(PFN_xrVoidFunction*)&requirements));
        XrGraphicsRequirementsD3D12KHR req{XR_TYPE_GRAPHICS_REQUIREMENTS_D3D12_KHR};CHECK(requirements(instance,system,&req));luid=req.adapterLuid;
    } else {
        PFN_xrGetD3D11GraphicsRequirementsKHR requirements=nullptr;
        CHECK(xrGetInstanceProcAddr(instance,"xrGetD3D11GraphicsRequirementsKHR",(PFN_xrVoidFunction*)&requirements));
        XrGraphicsRequirementsD3D11KHR req{XR_TYPE_GRAPHICS_REQUIREMENTS_D3D11_KHR};CHECK(requirements(instance,system,&req));luid=req.adapterLuid;
    }
    ComPtr<IDXGIFactory4> factory;ComPtr<IDXGIAdapter> adapter;
    ComPtr<ID3D11Device> device;ComPtr<ID3D11DeviceContext> context;
    ComPtr<ID3D12Device> device12;ComPtr<ID3D12CommandQueue> queue;
    ComPtr<ID3D12CommandAllocator> allocator;ComPtr<ID3D12GraphicsCommandList> commands;
    ComPtr<ID3D12DescriptorHeap> heap;ComPtr<ID3D12Fence> fence;UINT64 fenceValue=0;
    HANDLE fenceEvent=nullptr;
    if(FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory))) || FAILED(factory->EnumAdapterByLuid(luid,IID_PPV_ARGS(&adapter))))return 1;
    XrGraphicsBindingD3D11KHR binding{XR_TYPE_GRAPHICS_BINDING_D3D11_KHR};
    XrGraphicsBindingD3D12KHR binding12{XR_TYPE_GRAPHICS_BINDING_D3D12_KHR};
    if(d12){
        if(FAILED(D3D12CreateDevice(adapter.Get(),D3D_FEATURE_LEVEL_11_0,IID_PPV_ARGS(&device12))))return 1;
        D3D12_COMMAND_QUEUE_DESC q{};q.Type=D3D12_COMMAND_LIST_TYPE_DIRECT;
        D3D12_DESCRIPTOR_HEAP_DESC h{};h.Type=D3D12_DESCRIPTOR_HEAP_TYPE_RTV;h.NumDescriptors=2;
        if(FAILED(device12->CreateCommandQueue(&q,IID_PPV_ARGS(&queue))) ||
           FAILED(device12->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,IID_PPV_ARGS(&allocator))) ||
           FAILED(device12->CreateCommandList(0,D3D12_COMMAND_LIST_TYPE_DIRECT,allocator.Get(),nullptr,IID_PPV_ARGS(&commands))) ||
           FAILED(device12->CreateDescriptorHeap(&h,IID_PPV_ARGS(&heap))) ||
           FAILED(device12->CreateFence(0,D3D12_FENCE_FLAG_NONE,IID_PPV_ARGS(&fence))))return 1;
        commands->Close();fenceEvent=CreateEventW(nullptr,FALSE,FALSE,nullptr);
        binding12.device=device12.Get();binding12.queue=queue.Get();
    }else{
        if(FAILED(D3D11CreateDevice(adapter.Get(),D3D_DRIVER_TYPE_UNKNOWN,nullptr,0,nullptr,0,D3D11_SDK_VERSION,&device,nullptr,&context)))return 1;
        binding.device=device.Get();
    }
    XrSessionCreateInfo sci{XR_TYPE_SESSION_CREATE_INFO};sci.systemId=system;sci.next=d12?(void*)&binding12:(void*)&binding;
    XrSession session;CHECK(xrCreateSession(instance,&sci,&session));
    XrReferenceSpaceCreateInfo ri{XR_TYPE_REFERENCE_SPACE_CREATE_INFO};ri.referenceSpaceType=XR_REFERENCE_SPACE_TYPE_LOCAL;ri.poseInReferenceSpace.orientation.w=1;
    XrSpace space;CHECK(xrCreateReferenceSpace(session,&ri,&space));
    XrSwapchain chains[3]{};std::vector<XrSwapchainImageD3D11KHR> images[3];std::vector<XrSwapchainImageD3D12KHR> images12[3];
    for(int c=0;c<3;++c){
        XrSwapchainCreateInfo s{XR_TYPE_SWAPCHAIN_CREATE_INFO};s.usageFlags=XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT|XR_SWAPCHAIN_USAGE_SAMPLED_BIT;
        s.format=DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;s.width=s.height=128;s.arraySize=2;s.sampleCount=s.faceCount=s.mipCount=1;
        CHECK(xrCreateSwapchain(session,&s,&chains[c]));uint32_t n=0;CHECK(xrEnumerateSwapchainImages(chains[c],0,&n,nullptr));
        if(d12){images12[c].resize(n,{XR_TYPE_SWAPCHAIN_IMAGE_D3D12_KHR});CHECK(xrEnumerateSwapchainImages(chains[c],n,&n,(XrSwapchainImageBaseHeader*)images12[c].data()));}
        else{images[c].resize(n,{XR_TYPE_SWAPCHAIN_IMAGE_D3D11_KHR});CHECK(xrEnumerateSwapchainImages(chains[c],n,&n,(XrSwapchainImageBaseHeader*)images[c].data()));}
    }
    bool running=false;while(!running){XrEventDataBuffer e{XR_TYPE_EVENT_DATA_BUFFER};
        if(xrPollEvent(instance,&e)==XR_SUCCESS && e.type==XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED &&
           ((XrEventDataSessionStateChanged*)&e)->state==XR_SESSION_STATE_READY){XrSessionBeginInfo b{XR_TYPE_SESSION_BEGIN_INFO};b.primaryViewConfigurationType=XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;CHECK(xrBeginSession(session,&b));running=true;}}
    const char* folder=getenv("SIMXR_DATA_DIR");const std::string stop=std::string(folder?folder:".")+"/layer_probe_stop";
    std::vector<unsigned char> pixels(128*128*4);
    for(int f=0;f<12000 && GetFileAttributesA(stop.c_str())==INVALID_FILE_ATTRIBUTES;++f){
        XrFrameState frame{XR_TYPE_FRAME_STATE};CHECK(xrWaitFrame(session,nullptr,&frame));CHECK(xrBeginFrame(session,nullptr));
        XrViewLocateInfo li{XR_TYPE_VIEW_LOCATE_INFO};li.viewConfigurationType=XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;li.displayTime=frame.predictedDisplayTime;li.space=space;
        XrViewState vs{XR_TYPE_VIEW_STATE};XrView views[2]{{XR_TYPE_VIEW},{XR_TYPE_VIEW}};uint32_t count=0;CHECK(xrLocateViews(session,&li,&vs,2,&count,views));
        for(int c=0;c<3;++c){uint32_t ix=0;CHECK(xrAcquireSwapchainImage(chains[c],nullptr,&ix));XrSwapchainImageWaitInfo wi{XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO};wi.timeout=XR_INFINITE_DURATION;CHECK(xrWaitSwapchainImage(chains[c],&wi));
            if(d12){if(FAILED(allocator->Reset())||FAILED(commands->Reset(allocator.Get(),nullptr)))return 1;}
            for(int eye=0;eye<2;++eye){for(int y=0;y<128;++y)for(int x=0;x<128;++x){auto* p=&pixels[(y*128+x)*4];
                p[0]=c==1?255:0;p[1]=0;p[2]=c==0?255:0;p[3]=c==2?0:255;
                if(c==2 && x>=36 && x<92 && y>=36 && y<92){p[0]=eye?255:0;p[1]=255;p[2]=0;p[3]=255;}
                // Half-transparent premultiplied white over the red menu: linear .5 -> sRGB 188.
                if(c==2 && x>=20 && x<32 && y>=48 && y<80){p[0]=p[1]=p[2]=188;p[3]=128;}}
                if(!d12)context->UpdateSubresource(images[c][ix].texture,eye,nullptr,pixels.data(),128*4,0);
                else{
                    auto handle=heap->GetCPUDescriptorHandleForHeapStart();handle.ptr+=eye*device12->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
                    D3D12_RENDER_TARGET_VIEW_DESC view{};view.Format=DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;view.ViewDimension=D3D12_RTV_DIMENSION_TEXTURE2DARRAY;
                    view.Texture2DArray.FirstArraySlice=eye;view.Texture2DArray.ArraySize=1;
                    device12->CreateRenderTargetView(images12[c][ix].texture,&view,handle);
                    const float clear[]{c==1?1.f:0.f,0.f,c==0?1.f:0.f,c==2?0.f:1.f};commands->ClearRenderTargetView(handle,clear,0,nullptr);
                    if(c==2){const float solid[]{eye?1.f:0.f,1.f,0.f,1.f};const D3D12_RECT rect{36,36,92,92};commands->ClearRenderTargetView(handle,solid,1,&rect);
                        const float half[]{.5f,.5f,.5f,.5f};const D3D12_RECT bar{20,48,32,80};commands->ClearRenderTargetView(handle,half,1,&bar);}
                }
            }
            if(d12){if(FAILED(commands->Close()))return 1;ID3D12CommandList* lists[]{commands.Get()};queue->ExecuteCommandLists(1,lists);
                if(FAILED(queue->Signal(fence.Get(),++fenceValue)))return 1;
                if(fence->GetCompletedValue()<fenceValue){fence->SetEventOnCompletion(fenceValue,fenceEvent);if(WaitForSingleObject(fenceEvent,5000)!=WAIT_OBJECT_0)return 1;}}

            CHECK(xrReleaseSwapchainImage(chains[c],nullptr));}
        XrCompositionLayerProjectionView pv[2][2]{};
        XrCompositionLayerProjection projection[2]{};
        for(int i=0;i<2;++i){projection[i].type=XR_TYPE_COMPOSITION_LAYER_PROJECTION;projection[i].space=space;projection[i].viewCount=2;projection[i].views=pv[i];
            projection[i].layerFlags=i?XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT:0;
            for(int e=0;e<2;++e){pv[i][e].type=XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW;pv[i][e].pose=views[e].pose;pv[i][e].fov=views[e].fov;pv[i][e].subImage={chains[i?2:0],{{0,0},{128,128}},uint32_t(e)};}}
        XrCompositionLayerQuad q{XR_TYPE_COMPOSITION_LAYER_QUAD};q.space=space;q.eyeVisibility=XR_EYE_VISIBILITY_BOTH;q.pose={{0,0,0,1},{0,1.7f,-2}};q.size={6,6};q.subImage={chains[1],{{0,0},{128,128}},0};
        XrCompositionLayerQuad top=q;top.size={.12f,.12f};top.subImage.swapchain=chains[0];
        const XrCompositionLayerBaseHeader* layers[]{(XrCompositionLayerBaseHeader*)&projection[0],(XrCompositionLayerBaseHeader*)&q,(XrCompositionLayerBaseHeader*)&projection[1],(XrCompositionLayerBaseHeader*)&top};
        XrFrameEndInfo end{XR_TYPE_FRAME_END_INFO};end.displayTime=frame.predictedDisplayTime;end.environmentBlendMode=XR_ENVIRONMENT_BLEND_MODE_OPAQUE;end.layerCount=4;end.layers=layers;CHECK(xrEndFrame(session,&end));
        if(f==5)printf("READY\n");
    }
    CHECK(xrEndSession(session));for(auto chain:chains)CHECK(xrDestroySwapchain(chain));CHECK(xrDestroySpace(space));CHECK(xrDestroySession(session));CHECK(xrDestroyInstance(instance));if(fenceEvent)CloseHandle(fenceEvent);return 0;
}
