// Small real XR menu app for the D3D11 and OpenGL capture paths.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define XR_NO_PROTOTYPES
#define XR_USE_PLATFORM_WIN32
#define XR_USE_GRAPHICS_API_D3D11
#define XR_USE_GRAPHICS_API_OPENGL
#include <windows.h>
#include <d3d11.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <wrl/client.h>
#include <GL/gl.h>
#include <cstdio>
#include <cstring>
#include <cmath>
#include <vector>
#include <string>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
#include "direct_runtime.h"
#include "ui_fixture.h"
using Microsoft::WRL::ComPtr;
#define CHECK(call) do { const auto r=(call); if (XR_FAILED(r)) { printf("FAIL %s: %d\n",#call,(int)r); return 1; } } while(0)

int main() {
    setvbuf(stdout,nullptr,_IONBF,0);
    if (!InitDirectRuntime()) return 1;
    const bool gl=getenv("XR_UI_BACKEND") && strcmp(getenv("XR_UI_BACKEND"),"opengl")==0;
    const int hand=getenv("XR_PROBE_UI_LEFT") && strcmp(getenv("XR_PROBE_UI_LEFT"),"1")==0 ? 0:1;
    const char* ext=gl ? XR_KHR_OPENGL_ENABLE_EXTENSION_NAME:XR_KHR_D3D11_ENABLE_EXTENSION_NAME;
    XrInstanceCreateInfo create{XR_TYPE_INSTANCE_CREATE_INFO};
    strcpy_s(create.applicationInfo.applicationName,"UI raycast fixture");
    create.applicationInfo.apiVersion=XR_MAKE_VERSION(1,0,0);
    create.enabledExtensionCount=1; create.enabledExtensionNames=&ext;
    XrInstance instance; CHECK(xrCreateInstance(&create,&instance));
    auto path=[&](const char* text) { XrPath p{}; xrStringToPath(instance,text,&p); return p; };
    XrSystemGetInfo systemInfo{XR_TYPE_SYSTEM_GET_INFO}; systemInfo.formFactor=XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY;
    XrSystemId system; CHECK(xrGetSystem(instance,&systemInfo,&system));
    XrSessionCreateInfo sessionInfo{XR_TYPE_SESSION_CREATE_INFO}; sessionInfo.systemId=system;
    ComPtr<ID3D11Device> device; ComPtr<ID3D11DeviceContext> context;
    XrGraphicsBindingD3D11KHR d3dBinding{XR_TYPE_GRAPHICS_BINDING_D3D11_KHR};
    XrGraphicsBindingOpenGLWin32KHR glBinding{XR_TYPE_GRAPHICS_BINDING_OPENGL_WIN32_KHR};
    HWND window=nullptr;
    if (gl) {
        PFN_xrGetOpenGLGraphicsRequirementsKHR requirements=nullptr;
        CHECK(xrGetInstanceProcAddr(instance,"xrGetOpenGLGraphicsRequirementsKHR",(PFN_xrVoidFunction*)&requirements));
        XrGraphicsRequirementsOpenGLKHR req{XR_TYPE_GRAPHICS_REQUIREMENTS_OPENGL_KHR}; CHECK(requirements(instance,system,&req));
        window=CreateWindowA("STATIC","GL fixture context",WS_POPUP,0,0,64,64,nullptr,nullptr,GetModuleHandleW(nullptr),nullptr);
        glBinding.hDC=GetDC(window);
        PIXELFORMATDESCRIPTOR pfd{}; pfd.nSize=sizeof(pfd); pfd.nVersion=1;
        pfd.dwFlags=PFD_DRAW_TO_WINDOW|PFD_SUPPORT_OPENGL|PFD_DOUBLEBUFFER; pfd.iPixelType=PFD_TYPE_RGBA; pfd.cColorBits=32;
        if (!SetPixelFormat(glBinding.hDC,ChoosePixelFormat(glBinding.hDC,&pfd),&pfd)) return 1;
        glBinding.hGLRC=wglCreateContext(glBinding.hDC);
        if (!wglMakeCurrent(glBinding.hDC,glBinding.hGLRC)) return 1;
        sessionInfo.next=&glBinding;
    } else {
        PFN_xrGetD3D11GraphicsRequirementsKHR requirements=nullptr;
        CHECK(xrGetInstanceProcAddr(instance,"xrGetD3D11GraphicsRequirementsKHR",(PFN_xrVoidFunction*)&requirements));
        XrGraphicsRequirementsD3D11KHR req{XR_TYPE_GRAPHICS_REQUIREMENTS_D3D11_KHR}; CHECK(requirements(instance,system,&req));
        ComPtr<IDXGIFactory4> factory; ComPtr<IDXGIAdapter> adapter;
        if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory))) || FAILED(factory->EnumAdapterByLuid(req.adapterLuid,IID_PPV_ARGS(&adapter)))) return 1;
        if (FAILED(D3D11CreateDevice(adapter.Get(),D3D_DRIVER_TYPE_UNKNOWN,nullptr,0,nullptr,0,D3D11_SDK_VERSION,&device,nullptr,&context))) return 1;
        d3dBinding.device=device.Get(); sessionInfo.next=&d3dBinding;
    }
    XrSession session; CHECK(xrCreateSession(instance,&sessionInfo,&session));
    const XrPosef identity{{0,0,0,1},{0,0,0}};
    XrReferenceSpaceCreateInfo ref{XR_TYPE_REFERENCE_SPACE_CREATE_INFO}; ref.referenceSpaceType=XR_REFERENCE_SPACE_TYPE_STAGE; ref.poseInReferenceSpace=identity;
    XrSpace stage; CHECK(xrCreateReferenceSpace(session,&ref,&stage));
    XrActionSetCreateInfo setInfo{XR_TYPE_ACTION_SET_CREATE_INFO}; strcpy_s(setInfo.actionSetName,"ui"); strcpy_s(setInfo.localizedActionSetName,"UI");
    XrActionSet set; CHECK(xrCreateActionSet(instance,&setInfo,&set));
    XrPath hands[2]{path("/user/hand/left"),path("/user/hand/right")};
    XrActionCreateInfo actionInfo{XR_TYPE_ACTION_CREATE_INFO};
    actionInfo.countSubactionPaths=2; actionInfo.subactionPaths=hands;
    strcpy_s(actionInfo.actionName,"ray"); strcpy_s(actionInfo.localizedActionName,"Ray"); actionInfo.actionType=XR_ACTION_TYPE_POSE_INPUT;
    XrAction aim; CHECK(xrCreateAction(set,&actionInfo,&aim));
    strcpy_s(actionInfo.actionName,"activate_widget"); strcpy_s(actionInfo.localizedActionName,"Activate"); actionInfo.actionType=XR_ACTION_TYPE_BOOLEAN_INPUT;
    XrAction activate; CHECK(xrCreateAction(set,&actionInfo,&activate));
    XrActionSuggestedBinding bindings[]{ {aim,path("/user/hand/left/input/aim/pose")}, {aim,path("/user/hand/right/input/aim/pose")},
        {activate,path("/user/hand/left/input/x/click")},{activate,path("/user/hand/right/input/a/click")} };
    XrInteractionProfileSuggestedBinding suggestion{XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING};
    suggestion.interactionProfile=path("/interaction_profiles/oculus/touch_controller"); suggestion.countSuggestedBindings=4; suggestion.suggestedBindings=bindings;
    CHECK(xrSuggestInteractionProfileBindings(instance,&suggestion));
    XrSessionActionSetsAttachInfo attach{XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO}; attach.countActionSets=1; attach.actionSets=&set;
    CHECK(xrAttachSessionActionSets(session,&attach));
    XrActionSpaceCreateInfo aimInfo{XR_TYPE_ACTION_SPACE_CREATE_INFO}; aimInfo.action=aim; aimInfo.subactionPath=hands[hand]; aimInfo.poseInActionSpace=identity;
    XrSpace aimSpace; CHECK(xrCreateActionSpace(session,&aimInfo,&aimSpace));
    XrSwapchainCreateInfo chainInfo{XR_TYPE_SWAPCHAIN_CREATE_INFO};
    chainInfo.usageFlags=XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT|XR_SWAPCHAIN_USAGE_SAMPLED_BIT;
    chainInfo.format=gl ? 0x8C43:DXGI_FORMAT_R8G8B8A8_UNORM_SRGB; // GL_SRGB8_ALPHA8
    chainInfo.sampleCount=chainInfo.arraySize=chainInfo.faceCount=chainInfo.mipCount=1;
    chainInfo.width=768; chainInfo.height=512;
    const XrRect2Di subRect{{32,64},{640,360}};
    XrSwapchain chain; CHECK(xrCreateSwapchain(session,&chainInfo,&chain));
    // Recreating one chain must preserve the other live chain's identity and images.
    XrSwapchain survivor; CHECK(xrCreateSwapchain(session,&chainInfo,&survivor));
    CHECK(xrDestroySwapchain(chain)); CHECK(xrCreateSwapchain(session,&chainInfo,&chain));
    if (chain==survivor) { fprintf(stderr,"Recreated swapchain reused a live handle\n"); return 1; }
    uint32_t survivorImages=0; CHECK(xrEnumerateSwapchainImages(survivor,0,&survivorImages,nullptr));
    if (!survivorImages) return 1;
    CHECK(xrDestroySwapchain(survivor));
    uint32_t count; CHECK(xrEnumerateSwapchainImages(chain,0,&count,nullptr));
    std::vector<XrSwapchainImageD3D11KHR> d3dImages(count,{XR_TYPE_SWAPCHAIN_IMAGE_D3D11_KHR});
    std::vector<XrSwapchainImageOpenGLKHR> glImages(count,{XR_TYPE_SWAPCHAIN_IMAGE_OPENGL_KHR});
    CHECK(xrEnumerateSwapchainImages(chain,count,&count,gl ? (XrSwapchainImageBaseHeader*)glImages.data():(XrSwapchainImageBaseHeader*)d3dImages.data()));
    bool began=false;
    for (int tries=0;tries<200 && !began;++tries) {
        XrEventDataBuffer event{XR_TYPE_EVENT_DATA_BUFFER};
        while (xrPollEvent(instance,&event)==XR_SUCCESS) {
            if (event.type==XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED && ((XrEventDataSessionStateChanged*)&event)->state==XR_SESSION_STATE_READY) {
                XrSessionBeginInfo begin{XR_TYPE_SESSION_BEGIN_INFO}; begin.primaryViewConfigurationType=XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;
                CHECK(xrBeginSession(session,&begin)); began=true;
            }
            event={XR_TYPE_EVENT_DATA_BUFFER};
        }
        if (!began) Sleep(10);
    }
    if (!began) return 1;
    // A rejected premature end must leave the session running for the frame loop.
    if (xrEndSession(session)!=XR_ERROR_SESSION_NOT_STOPPING) return 1;
    UiFixture fixture;
    std::vector<uint8_t> pixels(chainInfo.width*chainInfo.height*4,0);
    for (int f=0;f<12000;++f) {
        if (fixture.data && GetFileAttributesA((std::string(fixture.data)+"\\ui_probe_stop").c_str())!=INVALID_FILE_ATTRIBUTES) break;
        XrFrameState frame{XR_TYPE_FRAME_STATE}; CHECK(xrWaitFrame(session,nullptr,&frame)); CHECK(xrBeginFrame(session,nullptr));
        XrActiveActionSet active{set,XR_NULL_PATH}; XrActionsSyncInfo sync{XR_TYPE_ACTIONS_SYNC_INFO}; sync.countActiveActionSets=1; sync.activeActionSets=&active;
        CHECK(xrSyncActions(session,&sync));
        fixture.Update(session,aimSpace,stage,frame.predictedDisplayTime,activate,hands[hand],aim);
        // Encoded sRGB pixels; the different states are deliberately distinct in screenshots.
        const uint32_t top=gl ? chainInfo.height-subRect.offset.y-subRect.extent.height:subRect.offset.y;
        for (uint32_t y=0;y<chainInfo.height;++y) for (uint32_t x=0;x<chainInfo.width;++x) {
            const bool button=x>=416 && x<576 && y>=top+126 && y<top+234;
            const bool tooltip=fixture.hoverFrames>=8 && x>=416 && x<576 && y>=top+76 && y<top+106;
            uint8_t* p=&pixels[(y*chainInfo.width+x)*4];
            p[0]=tooltip ? 230:button ? (fixture.clicks ? 63:fixture.hovered ? 249:63):44;
            p[1]=tooltip ? 230:button ? (fixture.clicks ? 231:fixture.hovered ? 211:137):48;
            p[2]=tooltip ? 230:button ? (fixture.clicks ? 108:fixture.hovered ? 39:243):56;
            p[3]=255;
        }
        uint32_t index; CHECK(xrAcquireSwapchainImage(chain,nullptr,&index));
        XrSwapchainImageWaitInfo wait{XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO}; wait.timeout=XR_INFINITE_DURATION; CHECK(xrWaitSwapchainImage(chain,&wait));
        if (gl) {
            glBindTexture(GL_TEXTURE_2D,glImages[index].image);
            // Quad GL readback flips texture rows to top-down before composition.
            std::vector<uint8_t> flipped(pixels.size());
            for (uint32_t y=0;y<chainInfo.height;++y) memcpy(flipped.data()+y*chainInfo.width*4,pixels.data()+(chainInfo.height-1-y)*chainInfo.width*4,chainInfo.width*4);
            glTexSubImage2D(GL_TEXTURE_2D,0,0,0,chainInfo.width,chainInfo.height,GL_RGBA,GL_UNSIGNED_BYTE,flipped.data());
            glFinish();
        } else context->UpdateSubresource(d3dImages[index].texture,0,nullptr,pixels.data(),chainInfo.width*4,0);
        XrSwapchainImageReleaseInfo release{XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO}; CHECK(xrReleaseSwapchainImage(chain,&release));
        XrCompositionLayerQuad quad{XR_TYPE_COMPOSITION_LAYER_QUAD}; quad.space=stage; quad.eyeVisibility=XR_EYE_VISIBILITY_BOTH;
        quad.pose=fixture.pose; quad.size=fixture.size; quad.subImage={chain,subRect,0};
        const auto* layer=(const XrCompositionLayerBaseHeader*)&quad;
        XrFrameEndInfo end{XR_TYPE_FRAME_END_INFO}; end.displayTime=frame.predictedDisplayTime;
        end.environmentBlendMode=XR_ENVIRONMENT_BLEND_MODE_OPAQUE; end.layerCount=1; end.layers=&layer;
        CHECK(xrEndFrame(session,&end)); fixture.Write(f);
    }
    CHECK(xrRequestExitSession(session)); CHECK(xrEndSession(session)); CHECK(xrDestroySwapchain(chain)); CHECK(xrDestroySpace(aimSpace)); CHECK(xrDestroySpace(stage));
    CHECK(xrDestroySession(session)); CHECK(xrDestroyInstance(instance));
    if (gl) { wglMakeCurrent(nullptr,nullptr); wglDeleteContext(glBinding.hGLRC); ReleaseDC(window,glBinding.hDC); DestroyWindow(window); }
    printf("%s UI probe: %d click(s), %d snapshot errors\n",gl ? "OpenGL":"D3D11",fixture.clicks,fixture.syncErrors);
    return fixture.syncErrors ? 1:0;
}
