// Isolated experiment. Does not change Fox, the runtime, the registry, or game saves.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define XR_NO_PROTOTYPES
#define XR_USE_PLATFORM_WIN32
#define XR_USE_GRAPHICS_API_D3D11
#include <windows.h>
#include <d3d11.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <wrl/client.h>
#include <cstdio>
#include <cstring>
#include <cmath>
#include <vector>
#include <string>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
#include "direct_runtime.h"
#include "hover_model.h"
using Microsoft::WRL::ComPtr;
#define CHECK(call) do { const auto result=(call); if (XR_FAILED(result)) { std::printf("FAIL %s: %d\n", #call, (int)result); return 1; } } while(0)

static XrVector3f rotate(const XrQuaternionf& q, XrVector3f v) {
	return {(1-2*(q.y*q.y+q.z*q.z))*v.x + 2*(q.x*q.y-q.z*q.w)*v.y + 2*(q.x*q.z+q.y*q.w)*v.z,
		2*(q.x*q.y+q.z*q.w)*v.x + (1-2*(q.x*q.x+q.z*q.z))*v.y + 2*(q.y*q.z-q.x*q.w)*v.z,
		2*(q.x*q.z-q.y*q.w)*v.x + 2*(q.y*q.z+q.x*q.w)*v.y + (1-2*(q.x*q.x+q.y*q.y))*v.z};
}
static XrVector3f add(XrVector3f a, XrVector3f b) { return {a.x+b.x,a.y+b.y,a.z+b.z}; }
static XrVector3f sub(XrVector3f a, XrVector3f b) { return {a.x-b.x,a.y-b.y,a.z-b.z}; }
static const XrPosef identity{{0,0,0,1},{0,0,0}};

static BOOL CALLBACK sizeOwnPreview(HWND window, LPARAM) {
	DWORD owner=0; GetWindowThreadProcessId(window,&owner);
	wchar_t name[80]{}; GetClassNameW(window,name,80);
	if (owner==GetCurrentProcessId() && wcscmp(name,L"OpenXR Simulator")==0) {
		// A desktop-constrained 691px eye has >3mm equivalent disparity noise.
		// Size only this probe's window, offscreen and without focus, for a
		// deterministic high-resolution stereo measurement.
		SetWindowLongPtrW(window,GWL_STYLE,WS_POPUP);
		SetWindowPos(window,nullptr,-10000,-10000,4096,2048,SWP_NOACTIVATE|SWP_NOZORDER|SWP_FRAMECHANGED);
	}
	return TRUE;
}

int main() {
	setvbuf(stdout,nullptr,_IONBF,0);
	if (!InitDirectRuntime()) return 1;
	const char* data=getenv("SIMXR_DATA_DIR");
	if (!data) return 1;
	const int hand=getenv("VR_HOVER_HAND") && strcmp(getenv("VR_HOVER_HAND"),"left")==0 ? 0:1;
	const bool reduced=getenv("VR_HOVER_REDUCED_MOTION") != nullptr;
	const char* extension=XR_KHR_D3D11_ENABLE_EXTENSION_NAME;
	XrInstanceCreateInfo create{XR_TYPE_INSTANCE_CREATE_INFO};
	strcpy_s(create.applicationInfo.applicationName,"VR stereo hover depth experiment");
	create.applicationInfo.apiVersion=XR_MAKE_VERSION(1,0,0);
	create.enabledExtensionCount=1; create.enabledExtensionNames=&extension;
	XrInstance instance; CHECK(xrCreateInstance(&create,&instance));
	auto path=[&](const char* name) { XrPath p{}; xrStringToPath(instance,name,&p); return p; };
	XrSystemGetInfo getSystem{XR_TYPE_SYSTEM_GET_INFO}; getSystem.formFactor=XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY;
	XrSystemId system; CHECK(xrGetSystem(instance,&getSystem,&system));
	XrSystemProperties systemProperties{XR_TYPE_SYSTEM_PROPERTIES}; CHECK(xrGetSystemProperties(instance,system,&systemProperties));
	if (systemProperties.graphicsProperties.maxLayerCount<2) return 1;
	PFN_xrGetD3D11GraphicsRequirementsKHR requirements=nullptr;
	CHECK(xrGetInstanceProcAddr(instance,"xrGetD3D11GraphicsRequirementsKHR",(PFN_xrVoidFunction*)&requirements));
	XrGraphicsRequirementsD3D11KHR graphicsRequirements{XR_TYPE_GRAPHICS_REQUIREMENTS_D3D11_KHR};
	CHECK(requirements(instance,system,&graphicsRequirements));
	ComPtr<IDXGIFactory4> factory; ComPtr<IDXGIAdapter> adapter;
	if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory))) || FAILED(factory->EnumAdapterByLuid(graphicsRequirements.adapterLuid,IID_PPV_ARGS(&adapter)))) return 1;
	ComPtr<ID3D11Device> device; ComPtr<ID3D11DeviceContext> context;
	if (FAILED(D3D11CreateDevice(adapter.Get(),D3D_DRIVER_TYPE_UNKNOWN,nullptr,0,nullptr,0,D3D11_SDK_VERSION,&device,nullptr,&context))) return 1;
	XrGraphicsBindingD3D11KHR binding{XR_TYPE_GRAPHICS_BINDING_D3D11_KHR}; binding.device=device.Get();
	XrSessionCreateInfo sessionCreate{XR_TYPE_SESSION_CREATE_INFO}; sessionCreate.systemId=system; sessionCreate.next=&binding;
	XrSession session; CHECK(xrCreateSession(instance,&sessionCreate,&session));
	XrReferenceSpaceCreateInfo reference{XR_TYPE_REFERENCE_SPACE_CREATE_INFO}; reference.referenceSpaceType=XR_REFERENCE_SPACE_TYPE_LOCAL; reference.poseInReferenceSpace=identity;
	XrSpace local; CHECK(xrCreateReferenceSpace(session,&reference,&local));
	reference.referenceSpaceType=XR_REFERENCE_SPACE_TYPE_VIEW;
	XrSpace view; CHECK(xrCreateReferenceSpace(session,&reference,&view));
	XrActionSetCreateInfo setCreate{XR_TYPE_ACTION_SET_CREATE_INFO}; strcpy_s(setCreate.actionSetName,"ui"); strcpy_s(setCreate.localizedActionSetName,"UI");
	XrActionSet actionSet; CHECK(xrCreateActionSet(instance,&setCreate,&actionSet));
	const XrPath hands[]{path("/user/hand/left"),path("/user/hand/right")};
	XrActionCreateInfo actionCreate{XR_TYPE_ACTION_CREATE_INFO}; actionCreate.countSubactionPaths=2; actionCreate.subactionPaths=hands;
	actionCreate.actionType=XR_ACTION_TYPE_POSE_INPUT; strcpy_s(actionCreate.actionName,"aim"); strcpy_s(actionCreate.localizedActionName,"Aim");
	XrAction aim; CHECK(xrCreateAction(actionSet,&actionCreate,&aim));
	actionCreate.actionType=XR_ACTION_TYPE_BOOLEAN_INPUT; strcpy_s(actionCreate.actionName,"press"); strcpy_s(actionCreate.localizedActionName,"Press");
	XrAction press; CHECK(xrCreateAction(actionSet,&actionCreate,&press));
	XrActionSuggestedBinding bindings[]{{aim,path("/user/hand/left/input/aim/pose")},{aim,path("/user/hand/right/input/aim/pose")},
		{press,path("/user/hand/left/input/trigger/value")},{press,path("/user/hand/right/input/trigger/value")}};
	XrInteractionProfileSuggestedBinding suggested{XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING};
	suggested.interactionProfile=path("/interaction_profiles/oculus/touch_controller"); suggested.countSuggestedBindings=4; suggested.suggestedBindings=bindings;
	CHECK(xrSuggestInteractionProfileBindings(instance,&suggested));
	XrSessionActionSetsAttachInfo attach{XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO}; attach.countActionSets=1; attach.actionSets=&actionSet;
	CHECK(xrAttachSessionActionSets(session,&attach));
	XrActionSpaceCreateInfo aimCreate{XR_TYPE_ACTION_SPACE_CREATE_INFO}; aimCreate.action=aim; aimCreate.subactionPath=hands[hand]; aimCreate.poseInActionSpace=identity;
	XrSpace aimSpace; CHECK(xrCreateActionSpace(session,&aimCreate,&aimSpace));
	XrSwapchainCreateInfo chainCreate{XR_TYPE_SWAPCHAIN_CREATE_INFO};
	chainCreate.usageFlags=XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT|XR_SWAPCHAIN_USAGE_SAMPLED_BIT;
	chainCreate.format=DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
	chainCreate.sampleCount=chainCreate.arraySize=chainCreate.faceCount=chainCreate.mipCount=1;
	chainCreate.width=chainCreate.height=1024;
	XrSwapchain atlas; CHECK(xrCreateSwapchain(session,&chainCreate,&atlas));
	uint32_t imageCount; CHECK(xrEnumerateSwapchainImages(atlas,0,&imageCount,nullptr));
	std::vector<XrSwapchainImageD3D11KHR> images(imageCount,{XR_TYPE_SWAPCHAIN_IMAGE_D3D11_KHR});
	CHECK(xrEnumerateSwapchainImages(atlas,imageCount,&imageCount,(XrSwapchainImageBaseHeader*)images.data()));
	bool began=false;
	for (int tries=0; tries<200 && !began; ++tries) {
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
	const XrExtent2Df panelSize{1.4f,.7875f};
	const XrRect2Di baseRect{{0,0},{1024,576}}, tileRect{{32,640},{256,172}};
	XrPosef panel=identity, buttonPose=identity;
	bool placed=false, previousDown=false; int clicks=0, presses=0, releases=0, hoverFrames=0;
	hover_depth::Lift lift;
	std::vector<uint8_t> pixels(1024*1024*4,0);
	ULONGLONG previousTick=GetTickCount64();
	for (int frameNumber=0; frameNumber<20000; ++frameNumber) {
		if (GetFileAttributesA((std::string(data)+"\\hover_depth_stop").c_str())!=INVALID_FILE_ATTRIBUTES) break;
		XrFrameState frame{XR_TYPE_FRAME_STATE}; CHECK(xrWaitFrame(session,nullptr,&frame)); CHECK(xrBeginFrame(session,nullptr));
		XrActiveActionSet active{actionSet,XR_NULL_PATH}; XrActionsSyncInfo sync{XR_TYPE_ACTIONS_SYNC_INFO}; sync.countActiveActionSets=1; sync.activeActionSets=&active;
		CHECK(xrSyncActions(session,&sync));
		if (!placed) {
			XrSpaceLocation head{XR_TYPE_SPACE_LOCATION}; CHECK(xrLocateSpace(view,local,frame.predictedDisplayTime,&head));
			// Snapshot at spawn: panel remains stable in LOCAL, including at standing head height.
			const auto forward=rotate(head.pose.orientation,{0,0,-1});
			const float yaw=atan2f(-forward.x,-forward.z);
			panel.orientation={0,sinf(yaw*.5f),0,cosf(yaw*.5f)};
			panel.position=add(head.pose.position,rotate(panel.orientation,{0,0,-1.2f}));
			placed=true;
		}
		XrSpaceLocation ray{XR_TYPE_SPACE_LOCATION}; CHECK(xrLocateSpace(aimSpace,local,frame.predictedDisplayTime,&ray));
		XrActionStateGetInfo get{XR_TYPE_ACTION_STATE_GET_INFO}; get.action=press; get.subactionPath=hands[hand];
		XrActionStateBoolean state{XR_TYPE_ACTION_STATE_BOOLEAN}; CHECK(xrGetActionStateBoolean(session,&get,&state));
		const XrQuaternionf inverse{-panel.orientation.x,-panel.orientation.y,-panel.orientation.z,panel.orientation.w};
		const auto origin=rotate(inverse,sub(ray.pose.position,panel.position));
		const auto direction=rotate(inverse,rotate(ray.pose.orientation,{0,0,-1}));
		const float distance=fabsf(direction.z)>1e-6f ? -origin.z/direction.z : -1.f;
		const float u=.5f+(origin.x+distance*direction.x)/panelSize.width;
		const float v=.5f-(origin.y+distance*direction.y)/panelSize.height;
		const float oldHeight=reduced ? 0.f:lift.meters;
		const float frontDistance=fabsf(direction.z)>1e-6f ? (oldHeight-origin.z)/direction.z : -1.f;
		const float frontU=.5f+(origin.x+frontDistance*direction.x)/panelSize.width;
		const float frontV=.5f-(origin.y+frontDistance*direction.y)/panelSize.height;
		// Keep original layout/hit bounds immutable, while also accepting the
		// visible raised face. This prevents a visible edge from becoming dead
		// when parallax moves it beyond its original base-plane rectangle.
		const bool hovered=(distance>0 && hover_depth::button.contains(u,v)) ||
			(oldHeight>.00001f && frontDistance>0 && hover_depth::button.contains(frontU,frontV));
		const bool down=state.isActive && state.currentState;
		hoverFrames=hovered ? hoverFrames+1:0;
		if (down && !previousDown) { ++presses; if (hovered) ++clicks; }
		if (!down && previousDown) ++releases;
		previousDown=down;
		const auto now=GetTickCount64(); const float dt=(float)(now-previousTick)*.001f; previousTick=now;
		lift.update(hovered,down,dt);
		const float height=reduced ? 0.f:lift.meters;
		buttonPose=panel;
		buttonPose.position=add(panel.position,rotate(panel.orientation,{(.725f-.5f)*panelSize.width,0,height}));
		std::fill(pixels.begin(),pixels.end(),0);
		auto fill=[&](int l,int t,int r,int b,int red,int green,int blue,int alpha=255) {
			for (int y=t; y<b; ++y) for (int x=l; x<r; ++x) {
				auto* p=&pixels[(y*1024+x)*4]; p[0]=(uint8_t)red; p[1]=(uint8_t)green; p[2]=(uint8_t)blue; p[3]=(uint8_t)alpha;
			}
		};
		fill(0,0,1024,576,35,40,49);
		fill(4,4,1020,572,25,29,36);
		fill(60,72,420,102,195,199,207);
		fill(60,142,420,153,80,88,99);
		fill(60,202,488,374,47,53,64);
		// A source-plane reference marker at the same local X as the button.
		fill(710,100,774,136,224,35,205);
		// Atlas guard space is transparent. Base has no duplicate button underneath.
		fill(32,640,288,812,hovered ? 255:63,hovered ? 194:137,hovered ? 34:243);
		fill(36,644,284,808,clicks ? 45:hovered ? 190:34,clicks ? 190:hovered ? 137:93,clicks ? 92:hovered ? 23:167);
		// Constant-color fiducial with a constant surround: binocular movement is
		// measurable without hover recoloring/antialiasing shifting its centroid.
		fill(140,665,180,701,8,8,8);
		fill(144,669,176,697,20,235,210);
		// High-contrast content moves with the whole isolated button, including its label.
		fill(92,705,228,716,230,233,237);
		fill(111,736,209,743,230,233,237);
		uint32_t index; CHECK(xrAcquireSwapchainImage(atlas,nullptr,&index));
		XrSwapchainImageWaitInfo wait{XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO}; wait.timeout=XR_INFINITE_DURATION;
		CHECK(xrWaitSwapchainImage(atlas,&wait));
		context->UpdateSubresource(images[index].texture,0,nullptr,pixels.data(),1024*4,0);
		XrSwapchainImageReleaseInfo release{XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO}; CHECK(xrReleaseSwapchainImage(atlas,&release));
		XrCompositionLayerQuad base{XR_TYPE_COMPOSITION_LAYER_QUAD}; base.space=local; base.eyeVisibility=XR_EYE_VISIBILITY_BOTH;
		base.layerFlags=XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT; base.pose=panel; base.size=panelSize; base.subImage={atlas,baseRect,0};
		XrCompositionLayerQuad button=base; button.pose=buttonPose; button.size={panelSize.width*.25f,panelSize.height*.30f}; button.subImage={atlas,tileRect,0};
		const XrCompositionLayerBaseHeader* layers[]{(XrCompositionLayerBaseHeader*)&base,(XrCompositionLayerBaseHeader*)&button};
		XrFrameEndInfo end{XR_TYPE_FRAME_END_INFO}; end.displayTime=frame.predictedDisplayTime;
		end.environmentBlendMode=XR_ENVIRONMENT_BLEND_MODE_OPAQUE; end.layerCount=2; end.layers=layers;
		CHECK(xrEndFrame(session,&end));
		if (frameNumber==2) EnumWindows(sizeOwnPreview,0);
		const auto temporary=std::string(data)+"\\hover_depth.json.tmp";
		FILE* out=nullptr;
		if (fopen_s(&out,temporary.c_str(),"w")==0 && out) {
			fprintf(out,"{\"frame\":%d,\"hovered\":%s,\"hover_frames\":%d,\"down\":%s,\"clicks\":%d,\"press_edges\":%d,\"release_edges\":%d,\"lift_m\":%.8f,\"hit\":[%.8f,%.8f],\"panel\":[%.8f,%.8f,%.8f],\"button\":[%.8f,%.8f,%.8f],\"max_layers\":%u,\"submitted_layers\":2,\"hand\":\"%s\"}\n",
				frameNumber,hovered ? "true":"false",hoverFrames,down ? "true":"false",clicks,presses,releases,height,u,v,
				panel.position.x,panel.position.y,panel.position.z,buttonPose.position.x,buttonPose.position.y,buttonPose.position.z,
				systemProperties.graphicsProperties.maxLayerCount,hand ? "right":"left");
			fclose(out); MoveFileExA(temporary.c_str(),(std::string(data)+"\\hover_depth.json").c_str(),MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH);
		}
		Sleep(4);
	}
	CHECK(xrEndSession(session)); CHECK(xrDestroySwapchain(atlas)); CHECK(xrDestroySpace(aimSpace)); CHECK(xrDestroySpace(view)); CHECK(xrDestroySpace(local));
	CHECK(xrDestroySession(session)); CHECK(xrDestroyInstance(instance));
	printf("Hover depth experiment exited normally: %d clicks, %d press edges, %d release edges\n",clicks,presses,releases);
}
