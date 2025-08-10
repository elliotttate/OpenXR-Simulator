// Minimal OpenXR Simulator Runtime (D3D11-only v0)
// - Implements enough of the runtime interface to let OpenXR apps start and render into runtime-owned D3D11 swapchains
// - Opens a desktop window and presents the app's submitted images side-by-side

#define XR_USE_PLATFORM_WIN32
#define XR_USE_GRAPHICS_API_D3D11

#include <windows.h>
#include <wrl/client.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <dxgi.h>
#include <dxgi1_6.h>
#include <string>
#include <vector>
#include <unordered_map>
#include <optional>
#include <cstring>
#include <cstdlib>
#include <thread>
#include <mutex>
#include <atomic>
#include <deque>
#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdio>

#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
#include <loader_interfaces.h>

using Microsoft::WRL::ComPtr;

#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "d3dcompiler.lib")

// Simple logging (debug output + file log)
static FILE* g_LogFile = nullptr;
static void EnsureLogFile() {
    if (g_LogFile) return;
    char base[MAX_PATH]{};
    DWORD len = GetEnvironmentVariableA("LOCALAPPDATA", base, (DWORD)sizeof(base));
    char path[MAX_PATH]{};
    if (len > 0 && len < sizeof(base)) {
        snprintf(path, sizeof(path), "%s\\OpenXR-Simulator", base);
        CreateDirectoryA(path, nullptr);
        snprintf(path, sizeof(path), "%s\\OpenXR-Simulator\\openxr_simulator.log", base);
    } else {
        snprintf(path, sizeof(path), ".\\openxr_simulator.log");
    }
    fopen_s(&g_LogFile, path, "a");
}
static void Log(const char* msg) {
    OutputDebugStringA(msg);
    EnsureLogFile();
    if (g_LogFile) { fputs(msg, g_LogFile); if (msg[0] && msg[strlen(msg)-1] != '\n') fputc('\n', g_LogFile); fflush(g_LogFile);} }
static void Log(const std::string& msg) { Log(msg.c_str()); }
static void Logf(const char* fmt, ...) {
    char buf[2048];
    va_list ap; va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    Log(buf);
}

static XrQuaternionf QuatFromYawPitch(float yaw, float pitch) {
    const float cy = cosf(yaw * 0.5f);
    const float sy = sinf(yaw * 0.5f);
    const float cp = cosf(pitch * 0.5f);
    const float sp = sinf(pitch * 0.5f);
    XrQuaternionf q{};
    q.x = sp * cy;
    q.y = cp * sy;
    q.z = -sp * sy;
    q.w = cp * cy;
    return q;
}


// Helper function to convert a typed format to typeless
static DXGI_FORMAT ToTypeless(DXGI_FORMAT format) {
    switch (format) {
        // R8G8B8A8 family
        case DXGI_FORMAT_R8G8B8A8_UNORM:
        case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB:
        case DXGI_FORMAT_R8G8B8A8_UINT:
        case DXGI_FORMAT_R8G8B8A8_SINT:
        case DXGI_FORMAT_R8G8B8A8_SNORM:
            return DXGI_FORMAT_R8G8B8A8_TYPELESS;
            
        // B8G8R8A8 family
        case DXGI_FORMAT_B8G8R8A8_UNORM:
        case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB:
            return DXGI_FORMAT_B8G8R8A8_TYPELESS;
            
        // R16G16B16A16 family
        case DXGI_FORMAT_R16G16B16A16_FLOAT:
        case DXGI_FORMAT_R16G16B16A16_UNORM:
        case DXGI_FORMAT_R16G16B16A16_UINT:
        case DXGI_FORMAT_R16G16B16A16_SNORM:
        case DXGI_FORMAT_R16G16B16A16_SINT:
            return DXGI_FORMAT_R16G16B16A16_TYPELESS;
            
        // R32G32B32A32 family
        case DXGI_FORMAT_R32G32B32A32_FLOAT:
        case DXGI_FORMAT_R32G32B32A32_UINT:
        case DXGI_FORMAT_R32G32B32A32_SINT:
            return DXGI_FORMAT_R32G32B32A32_TYPELESS;
            
        // R10G10B10A2 family
        case DXGI_FORMAT_R10G10B10A2_UNORM:
        case DXGI_FORMAT_R10G10B10A2_UINT:
            return DXGI_FORMAT_R10G10B10A2_TYPELESS;
            
        // Already typeless or depth formats - return as-is
        default:
            return format;
    }
}

// Runtime state
namespace rt {

struct Swapchain;

// Forward declarations
void PushState(XrSession s, XrSessionState newState);

// Global adapter LUID that we'll use consistently
static LUID g_adapterLuid = {};
static bool g_adapterLuidSet = false;

// Global persistent window that survives session creation/destruction
static HWND g_persistentWindow = nullptr;
static std::mutex g_windowMutex;
static ComPtr<IDXGISwapChain1> g_persistentSwapchain;
static UINT g_persistentWidth = 1920;
static UINT g_persistentHeight = 540;
static bool g_windowClassRegistered = false;

struct Instance {
    XrInstance handle{(XrInstance)1};
    std::vector<std::string> enabledExtensions;
};

struct Session {
    XrSession handle{(XrSession)1};
    XrSessionState state{XR_SESSION_STATE_IDLE};
    ComPtr<ID3D11Device> d3d11Device;
    ComPtr<ID3D11DeviceContext> d3d11Context;

    // Blit resources
    ComPtr<ID3D11VertexShader> blitVS;
    ComPtr<ID3D11PixelShader> blitPS;
    ComPtr<ID3D11SamplerState> samplerState;
    ComPtr<ID3D11RasterizerState> noCullRS;  // Rasterizer state with culling disabled

    // Desktop preview window (no thread - handled on main thread)
    HWND hwnd{nullptr};
    std::atomic<bool> isFocused{false};
    ComPtr<IDXGISwapChain1> previewSwapchain;
    UINT previewWidth{1920};
    UINT previewHeight{540};
    DXGI_FORMAT previewFormat{DXGI_FORMAT_UNKNOWN};  // Track format for matching
    std::mutex previewMutex;
};

struct Swapchain {
    XrSwapchain handle{(XrSwapchain)1};
    DXGI_FORMAT format{DXGI_FORMAT_R8G8B8A8_UNORM};
    uint32_t width{0}, height{0}, arraySize{2};
    uint32_t mipCount{1};
    std::vector<ComPtr<ID3D11Texture2D>> images;
    uint32_t nextIndex{0};
    uint32_t lastAcquired{UINT32_MAX};  // Initialize to invalid
    uint32_t lastReleased{UINT32_MAX};  // Initialize to invalid
    uint32_t imageCount{3};
};

static Instance g_instance{};
static Session g_session{};
static std::unordered_map<XrSwapchain, Swapchain> g_swapchains;

// Head tracking state for mouse look and WASD movement
static XrVector3f g_headPos = {0.0f, 1.7f, 0.0f};  // Start at standing eye height
static float g_headYaw = 0.0f;    // Rotation around Y axis (left/right)
static float g_headPitch = 0.0f;  // Rotation around X axis (up/down)
static bool g_mouseCapture = false;
static POINT g_lastMousePos = {0, 0};

// Helper function to create quaternion from yaw and pitch
XrQuaternionf QuatFromYawPitch(float yaw, float pitch) {
    // Create rotation: first yaw around Y, then pitch around X
    float cy = cosf(yaw * 0.5f);
    float sy = sinf(yaw * 0.5f);
    float cp = cosf(pitch * 0.5f);
    float sp = sinf(pitch * 0.5f);
    
    // Combine rotations (yaw * pitch)
    XrQuaternionf q;
    q.w = cy * cp;
    q.x = cy * sp;
    q.y = sy * cp;
    q.z = -sy * sp;
    return q;
}

// Rotate a vector by a quaternion (q * v * q^-1)
static inline XrVector3f RotateVectorByQuaternion(const XrQuaternionf& q, const XrVector3f& v) {
    XrQuaternionf qv{ v.x, v.y, v.z, 0.0f };
    XrQuaternionf qinv{ -q.x, -q.y, -q.z, q.w };
    auto mul = [](const XrQuaternionf& a, const XrQuaternionf& b) -> XrQuaternionf {
        return XrQuaternionf{
            a.w*b.x + a.x*b.w + a.y*b.z - a.z*b.y,
            a.w*b.y - a.x*b.z + a.y*b.w + a.z*b.x,
            a.w*b.z + a.x*b.y - a.y*b.x + a.z*b.w,
            a.w*b.w - a.x*b.x - a.y*b.y - a.z*b.z
        };
    };
    XrQuaternionf t = mul(q, qv);
    XrQuaternionf r = mul(t, qinv);
    return XrVector3f{ r.x, r.y, r.z };
}

// Initialize shader resources for blitting
bool InitBlitResources(Session& s) {
    if (s.blitVS && s.blitPS && s.samplerState && s.noCullRS) return true;

    // Compile shaders
    const char* shaderSource = R"(
        Texture2D txDiffuse : register(t0);
        SamplerState samLinear : register(s0);

        struct VS_OUTPUT {
            float4 Pos : SV_POSITION;
            float2 Tex : TEXCOORD;
        };

        // Vertex Shader (generates fullscreen quad with correct UV mapping)
        VS_OUTPUT VSMain(uint vertexId : SV_VertexID) {
            VS_OUTPUT output;
            // Generate (0,0), (2,0), (0,2), (2,2) pattern
            float2 xy = float2((vertexId << 1) & 2, vertexId & 2);
            
            // Correct clip-space position
            output.Pos = float4(xy * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
            
            // Normalized UVs (0-1 range, not 0-2)
            output.Tex = xy * 0.5;
            
            return output;
        }

        // Pixel Shader - GPU handles sRGB conversion automatically with proper formats
        float4 PSMain(VS_OUTPUT input) : SV_TARGET {
            return txDiffuse.Sample(samLinear, input.Tex);
        }
    )";

    ComPtr<ID3DBlob> vsBlob, psBlob, errorBlob;
    HRESULT hr;
    UINT compileFlags = D3DCOMPILE_OPTIMIZATION_LEVEL3;

    // Compile VS
    hr = D3DCompile(shaderSource, strlen(shaderSource), "BlitShader", nullptr, nullptr, 
                    "VSMain", "vs_5_0", compileFlags, 0, vsBlob.GetAddressOf(), errorBlob.GetAddressOf());
    if (FAILED(hr)) {
        Logf("[SimXR] Failed to compile VS: %s", errorBlob ? (char*)errorBlob->GetBufferPointer() : "Unknown error");
        return false;
    }
    hr = s.d3d11Device->CreateVertexShader(vsBlob->GetBufferPointer(), vsBlob->GetBufferSize(), 
                                           nullptr, s.blitVS.GetAddressOf());
    if (FAILED(hr)) { Logf("[SimXR] Failed to create VS: 0x%08X", hr); return false; }

    // Compile PS
    hr = D3DCompile(shaderSource, strlen(shaderSource), "BlitShader", nullptr, nullptr, 
                    "PSMain", "ps_5_0", compileFlags, 0, psBlob.GetAddressOf(), errorBlob.GetAddressOf());
    if (FAILED(hr)) {
        Logf("[SimXR] Failed to compile PS: %s", errorBlob ? (char*)errorBlob->GetBufferPointer() : "Unknown error");
        return false;
    }
    hr = s.d3d11Device->CreatePixelShader(psBlob->GetBufferPointer(), psBlob->GetBufferSize(), 
                                          nullptr, s.blitPS.GetAddressOf());
    if (FAILED(hr)) { Logf("[SimXR] Failed to create PS: 0x%08X", hr); return false; }

    // Create Sampler State
    D3D11_SAMPLER_DESC sampDesc = {};
    sampDesc.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sampDesc.AddressU = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampDesc.AddressV = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampDesc.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampDesc.ComparisonFunc = D3D11_COMPARISON_NEVER;
    sampDesc.MinLOD = 0; 
    sampDesc.MaxLOD = D3D11_FLOAT32_MAX;
    hr = s.d3d11Device->CreateSamplerState(&sampDesc, s.samplerState.GetAddressOf());
    if (FAILED(hr)) { Logf("[SimXR] Failed to create SamplerState: 0x%08X", hr); return false; }

    // Create Rasterizer State with culling disabled
    D3D11_RASTERIZER_DESC rsDesc{};
    rsDesc.FillMode = D3D11_FILL_SOLID;
    rsDesc.CullMode = D3D11_CULL_NONE;  // Disable culling to prevent triangles from being discarded
    rsDesc.FrontCounterClockwise = FALSE;
    rsDesc.DepthClipEnable = TRUE;
    rsDesc.ScissorEnable = FALSE;
    rsDesc.MultisampleEnable = FALSE;
    rsDesc.AntialiasedLineEnable = FALSE;
    hr = s.d3d11Device->CreateRasterizerState(&rsDesc, s.noCullRS.GetAddressOf());
    if (FAILED(hr)) { Logf("[SimXR] Failed to create RasterizerState: 0x%08X", hr); return false; }

    Log("[SimXR] Blit resources initialized successfully.");
    return true;
}

static LRESULT CALLBACK WndProc(HWND hWnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
        case WM_CLOSE:
            if (rt::g_session.handle != XR_NULL_HANDLE) {
                rt::PushState(rt::g_session.handle, XR_SESSION_STATE_EXITING);
            }
            Log("[SimXR] WndProc: WM_CLOSE received");
            DestroyWindow(hWnd);
            return 0;
        case WM_DESTROY:
            Log("[SimXR] WndProc: WM_DESTROY received -> PostQuitMessage");
            PostQuitMessage(0);
            return 0;
        case WM_ACTIVATE:
            if (LOWORD(wParam) != WA_INACTIVE) {
                rt::g_session.isFocused = true;
                Log("[SimXR] WndProc: WM_ACTIVATE -> focused");
                // Push FOCUSED state if we were VISIBLE
                if (rt::g_session.state == XR_SESSION_STATE_VISIBLE) {
                    rt::PushState(rt::g_session.handle, XR_SESSION_STATE_FOCUSED);
                }
            } else {
                rt::g_session.isFocused = false;
                Log("[SimXR] WndProc: WM_ACTIVATE -> unfocused");
                rt::g_mouseCapture = false;  // Release mouse capture when window loses focus
                ReleaseCapture();
                // Push VISIBLE state if we were FOCUSED
                if (rt::g_session.state == XR_SESSION_STATE_FOCUSED) {
                    rt::PushState(rt::g_session.handle, XR_SESSION_STATE_VISIBLE);
                }
            }
            return 0;
        case WM_LBUTTONDOWN:
            Logf("[SimXR] WM_LBUTTONDOWN: focused=%d", rt::g_session.isFocused.load());
            if (rt::g_session.isFocused) {
                rt::g_mouseCapture = true;
                SetCapture(hWnd);
                GetCursorPos(&rt::g_lastMousePos);
                ShowCursor(FALSE);
                Log("[SimXR] Mouse captured for look control");
            }
            return 0;
        case WM_LBUTTONUP:
            if (rt::g_mouseCapture) {
                rt::g_mouseCapture = false;
                ReleaseCapture();
                ShowCursor(TRUE);
            }
            return 0;
        case WM_MOUSEMOVE:
            if (rt::g_mouseCapture) {
                POINT currentPos;
                GetCursorPos(&currentPos);
                
                // Calculate delta
                int deltaX = currentPos.x - rt::g_lastMousePos.x;
                int deltaY = currentPos.y - rt::g_lastMousePos.y;
                
                // Update yaw and pitch (with sensitivity)
                const float sensitivity = 0.002f;
                rt::g_headYaw -= deltaX * sensitivity;
                rt::g_headPitch -= deltaY * sensitivity;  // Inverted for natural feel
                
                // Clamp pitch to avoid gimbal lock
                const float maxPitch = 1.5f;  // ~85 degrees
                if (rt::g_headPitch > maxPitch) rt::g_headPitch = maxPitch;
                if (rt::g_headPitch < -maxPitch) rt::g_headPitch = -maxPitch;
                
                // Reset cursor to center of window to avoid hitting screen edges
                RECT rect;
                GetWindowRect(hWnd, &rect);
                int centerX = (rect.left + rect.right) / 2;
                int centerY = (rect.top + rect.bottom) / 2;
                SetCursorPos(centerX, centerY);
                rt::g_lastMousePos.x = centerX;
                rt::g_lastMousePos.y = centerY;
            }
            return 0;
        default:
            break;
    }
    return DefWindowProc(hWnd, msg, wParam, lParam);
}

static void ensurePreview(Session& s) {
    if (s.hwnd) return;
    WNDCLASSW wc{}; wc.lpfnWndProc = WndProc; wc.hInstance = GetModuleHandleW(nullptr); wc.lpszClassName = L"OpenXR Simulator";
    RegisterClassW(&wc);
    s.hwnd = CreateWindowExW(0, wc.lpszClassName, L"OpenXR Simulator", WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                             CW_USEDEFAULT, CW_USEDEFAULT, (int)s.previewWidth, (int)s.previewHeight, nullptr, nullptr, wc.hInstance, nullptr);
    Logf("[SimXR] ensurePreview: hwnd=%p size=%ux%u", s.hwnd, s.previewWidth, s.previewHeight);
    
    // Make sure window is shown and updated
    if (s.hwnd) {
        ShowWindow(s.hwnd, SW_SHOW);
        UpdateWindow(s.hwnd);
        
        // Check if window has focus
        if (GetForegroundWindow() == s.hwnd) {
            s.isFocused = true;
            Log("[SimXR] Window created with focus");
        } else {
            s.isFocused = false;
            Log("[SimXR] Window created without focus");
        }
    }

    ComPtr<IDXGIDevice> dxgiDev; s.d3d11Device.As(&dxgiDev);
    ComPtr<IDXGIAdapter> adapter; dxgiDev->GetAdapter(adapter.GetAddressOf());
    ComPtr<IDXGIFactory2> factory; adapter->GetParent(IID_PPV_ARGS(factory.GetAddressOf()));
    DXGI_SWAP_CHAIN_DESC1 desc{}; desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM; desc.Width = s.previewWidth; desc.Height = s.previewHeight;
    desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT; desc.BufferCount = 2; desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD; desc.SampleDesc.Count = 1;
    HRESULT hr = factory->CreateSwapChainForHwnd(s.d3d11Device.Get(), s.hwnd, &desc, nullptr, nullptr, s.previewSwapchain.GetAddressOf());
    Logf("[SimXR] ensurePreview: CreateSwapChainForHwnd hr=0x%08X swapchain=%p", (unsigned)hr, s.previewSwapchain.Get());
}

// Window thread functions removed - window now handled on main thread

} // namespace rt

// ----------------- OpenXR runtime exports -----------------

static XrResult XRAPI_PTR xrGetInstanceProcAddr_runtime(XrInstance, const char* name, PFN_xrVoidFunction* fn);

extern "C" XRAPI_ATTR XrResult XRAPI_CALL xrNegotiateLoaderRuntimeInterface(const XrNegotiateLoaderInfo* loaderInfo,
                                                                            XrNegotiateRuntimeRequest* runtimeRequest) {
    try {
        EnsureLogFile();
        Log("\n[SimXR] ========== OpenXR Simulator Runtime Starting ==========\n");
        if (!loaderInfo || !runtimeRequest) {
            Log("[SimXR] xrNegotiateLoaderRuntimeInterface: ERROR - null parameters");
            return XR_ERROR_INITIALIZATION_FAILED;
        }
        
        Logf("[SimXR] xrNegotiateLoaderRuntimeInterface: loaderInfo=%p, runtimeRequest=%p", loaderInfo, runtimeRequest);
        Logf("[SimXR]   Loader minInterfaceVersion=%u, maxInterfaceVersion=%u, minApiVersion=0x%X, maxApiVersion=0x%X",
             loaderInfo->minInterfaceVersion, loaderInfo->maxInterfaceVersion,
             loaderInfo->minApiVersion, loaderInfo->maxApiVersion);
        
        runtimeRequest->runtimeInterfaceVersion = XR_CURRENT_LOADER_RUNTIME_VERSION;
        runtimeRequest->getInstanceProcAddr = xrGetInstanceProcAddr_runtime;
        runtimeRequest->runtimeApiVersion = XR_CURRENT_API_VERSION;
        
        Logf("[SimXR] xrNegotiateLoaderRuntimeInterface: SUCCESS - runtimeApiVersion=0x%X (%u)", 
             runtimeRequest->runtimeApiVersion, runtimeRequest->runtimeApiVersion);
        return XR_SUCCESS;
    } catch (...) {
        Log("[SimXR] xrNegotiateLoaderRuntimeInterface: EXCEPTION caught!");
        return XR_ERROR_INITIALIZATION_FAILED;
    }
}
// xrGetD3D11GraphicsRequirementsKHR (XR_KHR_D3D11_enable)
static XrResult XRAPI_PTR xrGetD3D11GraphicsRequirementsKHR_runtime(
    XrInstance instance, XrSystemId systemId, XrGraphicsRequirementsD3D11KHR* req) {
    Logf("[SimXR] xrGetD3D11GraphicsRequirementsKHR called: instance=%p, systemId=%llu, req=%p",
         instance, (unsigned long long)systemId, req);
    if (!req) {
        Log("[SimXR] xrGetD3D11GraphicsRequirementsKHR: ERROR - null req");
        return XR_ERROR_VALIDATION_FAILURE;
    }
    
    Logf("[SimXR] xrGetD3D11GraphicsRequirementsKHR: req struct size = %zu, expected = %zu",
         sizeof(*req), sizeof(XrGraphicsRequirementsD3D11KHR));
    
    // Check if the struct type is already set (Unity might pre-fill it)
    if (req->type != 0) {
        Logf("[SimXR] xrGetD3D11GraphicsRequirementsKHR: req->type already set to %d", req->type);
    }
    
    // Zero initialize the entire structure first
    memset(req, 0, sizeof(XrGraphicsRequirementsD3D11KHR));
    req->type = XR_TYPE_GRAPHICS_REQUIREMENTS_D3D11_KHR;
    req->next = nullptr;
    
    Microsoft::WRL::ComPtr<IDXGIFactory1> f;
    HRESULT hr = CreateDXGIFactory1(IID_PPV_ARGS(f.GetAddressOf()));
    if (FAILED(hr)) { 
        Logf("[SimXR] CreateDXGIFactory1 failed: 0x%08X", hr); 
        return XR_ERROR_RUNTIME_FAILURE; 
    }
    
    Microsoft::WRL::ComPtr<IDXGIAdapter1> bestAdapter;
    DXGI_ADAPTER_DESC1 bestDesc{};
    bool foundHardware = false;
    
    // Find the best hardware adapter
    for (UINT i = 0; ; ++i) {
        Microsoft::WRL::ComPtr<IDXGIAdapter1> adapt;
        if (f->EnumAdapters1(i, adapt.GetAddressOf()) == DXGI_ERROR_NOT_FOUND)
            break;
            
        DXGI_ADAPTER_DESC1 d{}; 
        adapt->GetDesc1(&d);
        
        // Skip software adapters
        if (d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) 
            continue;
            
        // Use the first hardware adapter we find
        if (!foundHardware) {
            bestAdapter = adapt;
            bestDesc = d;
            foundHardware = true;
            
            wchar_t* descStr = d.Description;
            char descAscii[128];
            wcstombs(descAscii, descStr, sizeof(descAscii));
            descAscii[sizeof(descAscii)-1] = '\0';
            Logf("[SimXR] Found hardware adapter: %s", descAscii);
            Logf("[SimXR]   LUID: High=%ld, Low=%lu", 
                 (long)d.AdapterLuid.HighPart, 
                 (unsigned long)d.AdapterLuid.LowPart);
            Logf("[SimXR]   Dedicated Video Memory: %llu MB", 
                 (unsigned long long)(d.DedicatedVideoMemory / (1024*1024)));
        }
    }
    
    if (foundHardware) {
        req->adapterLuid = bestDesc.AdapterLuid;
        req->minFeatureLevel = D3D_FEATURE_LEVEL_11_0;
        
        // Save this LUID for later validation
        rt::g_adapterLuid = bestDesc.AdapterLuid;
        rt::g_adapterLuidSet = true;
        
        Logf("[SimXR] xrGetD3D11GraphicsRequirementsKHR: Returning:");
        Logf("[SimXR]   type = %d (expected %d)", req->type, XR_TYPE_GRAPHICS_REQUIREMENTS_D3D11_KHR);
        Logf("[SimXR]   next = %p", req->next);
        Logf("[SimXR]   adapterLuid.HighPart = %ld (0x%08X)", 
             (long)req->adapterLuid.HighPart, (unsigned)req->adapterLuid.HighPart);
        Logf("[SimXR]   adapterLuid.LowPart = %lu (0x%08X)", 
             (unsigned long)req->adapterLuid.LowPart, (unsigned)req->adapterLuid.LowPart);
        Logf("[SimXR]   minFeatureLevel = 0x%X (D3D_FEATURE_LEVEL_11_0 = 0x%X)", 
             req->minFeatureLevel, D3D_FEATURE_LEVEL_11_0);
        
        Log("[SimXR] xrGetD3D11GraphicsRequirementsKHR: SUCCESS - Returning XR_SUCCESS");
        return XR_SUCCESS;
    }
    
    // No hardware adapter found, this is an error for VR
    Log("[SimXR] xrGetD3D11GraphicsRequirementsKHR: ERROR - No hardware graphics adapter found");
    return XR_ERROR_SYSTEM_INVALID;
}

// --- Minimal implementations ---

static const char* kSupportedExtensions[] = { 
    XR_KHR_D3D11_ENABLE_EXTENSION_NAME,
    "XR_KHR_win32_convert_performance_counter_time"  // Unity often requires this
    // Only list extensions we actually implement
};

static XrResult XRAPI_PTR xrEnumerateApiLayerProperties_runtime(uint32_t propertyCapacityInput,
                                                                uint32_t* propertyCountOutput,
                                                                XrApiLayerProperties* properties) {
    Log("[SimXR] xrEnumerateApiLayerProperties called");
    // Runtime doesn't provide API layers, only extensions
    if (propertyCountOutput) *propertyCountOutput = 0;
    return XR_SUCCESS;
}
static XrResult XRAPI_PTR xrEnumerateInstanceExtensionProperties_runtime(const char* layerName, uint32_t propertyCapacityInput,
                                                                         uint32_t* propertyCountOutput,
                                                                         XrExtensionProperties* properties) {
    if (layerName && layerName[0] != '\0') return XR_ERROR_LAYER_INVALID;
    const uint32_t count = (uint32_t)(sizeof(kSupportedExtensions)/sizeof(kSupportedExtensions[0]));
    if (propertyCountOutput) *propertyCountOutput = count;
    if (properties && propertyCapacityInput) {
        for (uint32_t i = 0; i < propertyCapacityInput && i < count; ++i) {
            properties[i].type = XR_TYPE_EXTENSION_PROPERTIES;
            properties[i].next = nullptr;
            std::strncpy(properties[i].extensionName, kSupportedExtensions[i], XR_MAX_EXTENSION_NAME_SIZE - 1);
            properties[i].extensionName[XR_MAX_EXTENSION_NAME_SIZE - 1] = '\0';
            properties[i].extensionVersion = 1;
            Logf("[SimXR] ext[%u]=%s", i, properties[i].extensionName);
        }
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrCreateInstance_runtime(const XrInstanceCreateInfo* createInfo, XrInstance* instance) {
    if (!createInfo || !instance) return XR_ERROR_VALIDATION_FAILURE;
    // applicationName may not be null-terminated
    char appName[XR_MAX_APPLICATION_NAME_SIZE + 1] = {0};
    memcpy(appName, createInfo->applicationInfo.applicationName, XR_MAX_APPLICATION_NAME_SIZE);
    Logf("[SimXR] xrCreateInstance: app=%s version=%u", 
         appName,
         createInfo->applicationInfo.applicationVersion);
    rt::g_instance = {};
    rt::g_instance.enabledExtensions.clear();
    
    // Validate that all requested extensions are supported
    const uint32_t supportedCount = (uint32_t)(sizeof(kSupportedExtensions)/sizeof(kSupportedExtensions[0]));
    for (uint32_t i = 0; i < createInfo->enabledExtensionCount; ++i) {
        bool supported = false;
        for (uint32_t j = 0; j < supportedCount; ++j) {
            if (strcmp(createInfo->enabledExtensionNames[i], kSupportedExtensions[j]) == 0) {
                supported = true;
                break;
            }
        }
        if (!supported) {
            Logf("[SimXR] xrCreateInstance: ERROR - Unsupported extension %s", createInfo->enabledExtensionNames[i]);
            return XR_ERROR_EXTENSION_NOT_PRESENT;
        }
        rt::g_instance.enabledExtensions.emplace_back(createInfo->enabledExtensionNames[i]);
        Logf("[SimXR]   enabledExt[%u]=%s", i, createInfo->enabledExtensionNames[i]);
    }
    rt::g_instance.handle = (XrInstance)1;  // Set a valid handle
    *instance = rt::g_instance.handle;
    Log("[SimXR] xrCreateInstance: SUCCESS");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrDestroyInstance_runtime(XrInstance instance) { 
    Logf("[SimXR] xrDestroyInstance called: instance=%p", instance);
    
    // Clear the global instance
    if (instance == rt::g_instance.handle) {
        Log("[SimXR] xrDestroyInstance: Clearing global instance");
        rt::g_instance = {};
        
        // DON'T destroy the window - keep it alive for Unity's rapid recreation
        // The window will stay alive until the process exits
        Log("[SimXR] xrDestroyInstance: Keeping window alive for potential recreation");
    }
    
    Log("[SimXR] xrDestroyInstance: SUCCESS - Returning XR_SUCCESS");
    Log("[SimXR] ========== Instance Destroyed - Waiting for new instance ==========");
    return XR_SUCCESS; 
}

static XrResult XRAPI_PTR xrGetInstanceProperties_runtime(XrInstance, XrInstanceProperties* props) {
    if (!props) return XR_ERROR_VALIDATION_FAILURE;
    props->type = XR_TYPE_INSTANCE_PROPERTIES;
    props->next = nullptr;
    props->runtimeVersion = XR_MAKE_VERSION(1, 0, 27);
    strncpy(props->runtimeName, "OpenXR Simulator Runtime", XR_MAX_RUNTIME_NAME_SIZE - 1);
    props->runtimeName[XR_MAX_RUNTIME_NAME_SIZE - 1] = '\0';
    Log("[SimXR] xrGetInstanceProperties: returning OpenXR Simulator Runtime");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetSystem_runtime(XrInstance, const XrSystemGetInfo* info, XrSystemId* systemId) {
    if (!info || !systemId) return XR_ERROR_VALIDATION_FAILURE;
    Logf("[SimXR] xrGetSystem: formFactor=%d", info->formFactor);
    if (info->formFactor != XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY) {
        Log("[SimXR] xrGetSystem: ERROR - form factor not HMD");
        return XR_ERROR_FORM_FACTOR_UNSUPPORTED;
    }
    *systemId = (XrSystemId)1; 
    Log("[SimXR] xrGetSystem: SUCCESS -> systemId=1"); 
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetSystemProperties_runtime(XrInstance, XrSystemId, XrSystemProperties* props) {
    if (!props) return XR_ERROR_VALIDATION_FAILURE;
    props->type = XR_TYPE_SYSTEM_PROPERTIES;
    props->next = nullptr;
    strncpy(props->systemName, "OpenXR Simulator", XR_MAX_SYSTEM_NAME_SIZE - 1);
    props->systemName[XR_MAX_SYSTEM_NAME_SIZE - 1] = '\0';
    props->systemId = 1;
    props->vendorId = 0;  // 0 = unknown vendor (more standard than 0xFFFF)
    props->graphicsProperties.maxSwapchainImageWidth = 4096;
    props->graphicsProperties.maxSwapchainImageHeight = 4096;
    props->graphicsProperties.maxLayerCount = 16;
    props->trackingProperties.positionTracking = XR_TRUE;
    props->trackingProperties.orientationTracking = XR_TRUE;
    Log("[SimXR] xrGetSystemProperties: returning OpenXR Simulator");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrEnumerateViewConfigurations_runtime(XrInstance, XrSystemId, uint32_t capacity, uint32_t* count, XrViewConfigurationType* types) {
    Logf("[SimXR] xrEnumerateViewConfigurations called: capacity=%u", capacity);
    if (count) *count = 1;
    if (capacity >= 1 && types) {
        types[0] = XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;
        Log("[SimXR] xrEnumerateViewConfigurations: Returning PRIMARY_STEREO");
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrEnumerateViewConfigurationViews_runtime(XrInstance, XrSystemId, XrViewConfigurationType viewType, uint32_t capacity, uint32_t* count, XrViewConfigurationView* views) {
    Logf("[SimXR] xrEnumerateViewConfigurationViews called: viewType=%d, capacity=%u", (int)viewType, capacity);
    if (count) *count = 2;
    if (capacity >= 2 && views) {
        for (uint32_t i = 0; i < 2; ++i) {
            views[i].type = XR_TYPE_VIEW_CONFIGURATION_VIEW;
            views[i].next = nullptr;
            views[i].recommendedImageRectWidth = 1280;
            views[i].recommendedImageRectHeight = 720;
            views[i].recommendedSwapchainSampleCount = 1;
            views[i].maxImageRectWidth = 4096; views[i].maxImageRectHeight = 4096; views[i].maxSwapchainSampleCount = 1;
        }
        Log("[SimXR] xrEnumerateViewConfigurationViews: Returned 2 views (1280x720 recommended)");
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrEnumerateEnvironmentBlendModes_runtime(
    XrInstance, XrSystemId, XrViewConfigurationType, uint32_t capacity, uint32_t* count, XrEnvironmentBlendMode* modes) {
    if (count) *count = 1;
    if (capacity >= 1 && modes) modes[0] = XR_ENVIRONMENT_BLEND_MODE_OPAQUE;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrCreateSession_runtime(XrInstance instance, const XrSessionCreateInfo* info, XrSession* session) {
    static int sessionCount = 0;
    sessionCount++;
    Log("[SimXR] ============================================");
    Logf("[SimXR] xrCreateSession called (call #%d, instance=%llu)", sessionCount, (unsigned long long)instance);
    Log("[SimXR] ============================================");
    if (!info || !session) return XR_ERROR_VALIDATION_FAILURE;
    
    // Check if we already have an active session
    if (rt::g_session.handle != XR_NULL_HANDLE && rt::g_session.state != XR_SESSION_STATE_IDLE) {
        Logf("[SimXR] xrCreateSession: ERROR - Session already exists (handle=%llu, state=%d)", 
            (unsigned long long)rt::g_session.handle, rt::g_session.state);
        // For now, reset the existing session to allow the new one
        // Reset session manually
        rt::g_session.handle = XR_NULL_HANDLE;
        rt::g_session.state = XR_SESSION_STATE_IDLE;
        rt::g_session.d3d11Device.Reset();
        rt::g_session.d3d11Context.Reset();
        rt::g_session.previewSwapchain.Reset();
        rt::g_session.previewWidth = 1920;
        rt::g_session.previewHeight = 540;
        rt::g_session.isFocused = false;
    }
    // Accept D3D11 only for v0
    const XrBaseInStructure* entry = reinterpret_cast<const XrBaseInStructure*>(info->next);
    while (entry) {
        if (entry->type == XR_TYPE_GRAPHICS_BINDING_D3D11_KHR) {
            const auto* b = reinterpret_cast<const XrGraphicsBindingD3D11KHR*>(entry);
            
            // Log the device details
            ComPtr<IDXGIDevice> dxgiDevice;
            if (SUCCEEDED(b->device->QueryInterface(IID_PPV_ARGS(&dxgiDevice)))) {
                ComPtr<IDXGIAdapter> adapter;
                if (SUCCEEDED(dxgiDevice->GetAdapter(&adapter))) {
                    DXGI_ADAPTER_DESC desc;
                    adapter->GetDesc(&desc);
                    Logf("[SimXR] xrCreateSession: App D3D11 device LUID=%llu/%llu", 
                         (unsigned long long)desc.AdapterLuid.HighPart,
                         (unsigned long long)desc.AdapterLuid.LowPart);
                }
            }
            
            // Use sessionCount to generate unique handles
            rt::g_session.handle = (XrSession)(uintptr_t)(0x1000 + sessionCount);
            rt::g_session.d3d11Device = b->device;
            rt::g_session.state = XR_SESSION_STATE_IDLE;
            b->device->GetImmediateContext(rt::g_session.d3d11Context.GetAddressOf());
            // Window will be created lazily on first frame
            *session = rt::g_session.handle;
            Logf("[SimXR] xrCreateSession: SUCCESS (D3D11, handle=%llu)", (unsigned long long)rt::g_session.handle);
            // Push READY event into queue
            rt::PushState(rt::g_session.handle, XR_SESSION_STATE_READY);
            return XR_SUCCESS;
        }
        entry = entry->next;
    }
    Log("[SimXR] xrCreateSession: ERROR - No D3D11 graphics binding found");
    return XR_ERROR_GRAPHICS_DEVICE_INVALID;
}


static XrResult XRAPI_PTR xrDestroySession_runtime(XrSession s) {
    Logf("[SimXR] xrDestroySession called (handle=%llu)", (unsigned long long)s);
    if (s != rt::g_session.handle) {
        Logf("[SimXR] xrDestroySession: ERROR - Invalid handle (expected %llu)", 
             (unsigned long long)rt::g_session.handle);
        return XR_ERROR_HANDLE_INVALID;
    }
    
    // Transfer window and swapchain to global persistent storage
    // Unity likes to create/destroy sessions rapidly for compatibility checks
    {
        std::lock_guard<std::mutex> lock(rt::g_windowMutex);
        if (rt::g_session.hwnd && !rt::g_persistentWindow) {
            rt::g_persistentWindow = rt::g_session.hwnd;
            rt::g_persistentSwapchain = rt::g_session.previewSwapchain;
            rt::g_persistentWidth = rt::g_session.previewWidth;
            rt::g_persistentHeight = rt::g_session.previewHeight;
            Log("[SimXR] xrDestroySession: Preserving window and swapchain for next session");
        } else if (rt::g_session.hwnd == rt::g_persistentWindow) {
            // Already using persistent window, just update the swapchain
            rt::g_persistentSwapchain = rt::g_session.previewSwapchain;
            rt::g_persistentWidth = rt::g_session.previewWidth;
            rt::g_persistentHeight = rt::g_session.previewHeight;
            Log("[SimXR] xrDestroySession: Updating persistent swapchain");
        }
    }
    
    // Reset session but don't destroy the window
    rt::g_session.handle = XR_NULL_HANDLE;
    rt::g_session.state = XR_SESSION_STATE_IDLE;
    rt::g_session.d3d11Device.Reset();
    rt::g_session.d3d11Context.Reset();
    rt::g_session.hwnd = nullptr;  // Clear from session but window still exists
    rt::g_session.previewWidth = 1920;
    rt::g_session.previewHeight = 540;
    rt::g_session.isFocused = false;
    Log("[SimXR] xrDestroySession: SUCCESS");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrEnumerateSwapchainFormats_runtime(XrSession, uint32_t capacity, uint32_t* count, int64_t* formats) {
    // List more formats that Unity might use
    const int64_t supportedFormats[] = {
        DXGI_FORMAT_R8G8B8A8_UNORM_SRGB,  // Unity often prefers sRGB
        DXGI_FORMAT_R8G8B8A8_UNORM,
        DXGI_FORMAT_B8G8R8A8_UNORM_SRGB,
        DXGI_FORMAT_B8G8R8A8_UNORM,
        DXGI_FORMAT_R16G16B16A16_FLOAT,   // HDR format
        DXGI_FORMAT_R32G32B32A32_FLOAT,   // High precision
        DXGI_FORMAT_D32_FLOAT,            // Depth buffer
        DXGI_FORMAT_D24_UNORM_S8_UINT,    // Depth + stencil
        DXGI_FORMAT_D16_UNORM             // 16-bit depth
    };
    const uint32_t formatCount = sizeof(supportedFormats) / sizeof(supportedFormats[0]);
    
    if (count) *count = formatCount;
    if (capacity > 0 && formats) {
        uint32_t copyCount = (capacity < formatCount) ? capacity : formatCount;
        for (uint32_t i = 0; i < copyCount; ++i) {
            formats[i] = supportedFormats[i];
        }
        Logf("[SimXR] xrEnumerateSwapchainFormats: Returned %u formats (first: %d)", copyCount, (int)formats[0]);
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrCreateSwapchain_runtime(XrSession, const XrSwapchainCreateInfo* ci, XrSwapchain* sc) {
    Log("[SimXR] ============================================");
    Logf("[SimXR] xrCreateSwapchain called: format=%d, size=%ux%u, arraySize=%u, mipCount=%u, sampleCount=%u, usageFlags=0x%X",
         ci ? (int)ci->format : -1, 
         ci ? ci->width : 0, 
         ci ? ci->height : 0, 
         ci ? ci->arraySize : 0, 
         ci ? ci->mipCount : 0, 
         ci ? ci->sampleCount : 0,
         ci ? ci->usageFlags : 0);
    
    // Log specific usage flags
    if (ci && ci->usageFlags) {
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT) 
            Log("[SimXR]   - COLOR_ATTACHMENT");
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT) 
            Log("[SimXR]   - DEPTH_STENCIL_ATTACHMENT");
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_UNORDERED_ACCESS_BIT) 
            Log("[SimXR]   - UNORDERED_ACCESS");
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_TRANSFER_SRC_BIT) 
            Log("[SimXR]   - TRANSFER_SRC");
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_TRANSFER_DST_BIT) 
            Log("[SimXR]   - TRANSFER_DST");
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_SAMPLED_BIT) 
            Log("[SimXR]   - SAMPLED");
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_MUTABLE_FORMAT_BIT) 
            Log("[SimXR]   - MUTABLE_FORMAT");
    }
    
    Log("[SimXR] ============================================");
    if (!ci || !sc) return XR_ERROR_VALIDATION_FAILURE;
    rt::Swapchain chain{}; 
    chain.handle = (XrSwapchain)(uintptr_t)(rt::g_swapchains.size() + 2);
    chain.format = (DXGI_FORMAT)ci->format;  // Store the original requested format
    chain.width = ci->width; 
    chain.height = ci->height; 
    chain.arraySize = ci->arraySize;
    chain.lastAcquired = UINT32_MAX;  // No image acquired yet
    chain.lastReleased = UINT32_MAX;  // No image released yet
    // Create textures on app device
    D3D11_TEXTURE2D_DESC td{};
    
    // Determine if this is a depth format
    bool isDepthFormat = (chain.format == DXGI_FORMAT_D32_FLOAT || 
                          chain.format == DXGI_FORMAT_D24_UNORM_S8_UINT || 
                          chain.format == DXGI_FORMAT_D16_UNORM);
    
    // Use typeless format for color textures to allow both UNORM and SRGB views
    // Keep depth formats as-is since they don't have the same issue
    td.Format = isDepthFormat ? chain.format : ToTypeless(chain.format); 
    td.Width = chain.width; 
    td.Height = chain.height; 
    td.ArraySize = chain.arraySize ? chain.arraySize : 1;  // Ensure at least 1
    td.MipLevels = ci->mipCount ? ci->mipCount : 1;  // Ensure at least 1
    chain.mipCount = td.MipLevels; 
    td.SampleDesc.Count = ci->sampleCount ? ci->sampleCount : 1;
    td.SampleDesc.Quality = 0;  // Must be 0 for non-MSAA
    // Set bind flags based on format type
    
    if (isDepthFormat) {
        td.BindFlags = D3D11_BIND_DEPTH_STENCIL;
        // Depth formats can also be shader resources if typeless
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_SAMPLED_BIT) {
            td.BindFlags |= D3D11_BIND_SHADER_RESOURCE;
        }
    } else {
        td.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
        
        // Add unordered access if requested
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_UNORDERED_ACCESS_BIT) {
            td.BindFlags |= D3D11_BIND_UNORDERED_ACCESS;
        }
    }
    
    td.Usage = D3D11_USAGE_DEFAULT;
    td.CPUAccessFlags = 0;
    
    // Set MiscFlags based on what Unity might need
    td.MiscFlags = 0;
    
    // Log the texture description for debugging
    Logf("[SimXR] Creating swapchain textures: Format=%d, %ux%u, Array=%u, Mips=%u, Samples=%u",
         td.Format, td.Width, td.Height, td.ArraySize, td.MipLevels, td.SampleDesc.Count);
    chain.imageCount = 3;
    for (uint32_t i = 0; i < chain.imageCount; ++i) {
        ComPtr<ID3D11Texture2D> tex; 
        HRESULT hr = rt::g_session.d3d11Device->CreateTexture2D(&td, nullptr, tex.GetAddressOf());
        if (FAILED(hr)) { 
            Logf("[SimXR] CreateTexture2D[%u] FAILED: hr=0x%08X", i, (unsigned)hr);
            Logf("[SimXR]   Format=%d, Size=%ux%u, Array=%u, Mips=%u, Samples=%u, BindFlags=0x%X",
                 td.Format, td.Width, td.Height, td.ArraySize, td.MipLevels, td.SampleDesc.Count, td.BindFlags);
            
            // Try to provide more specific error info
            if (hr == E_INVALIDARG) {
                Log("[SimXR]   ERROR: E_INVALIDARG - Invalid texture parameters");
                // Check common issues
                if (td.ArraySize == 0) Log("[SimXR]   - ArraySize is 0");
                if (td.Width == 0 || td.Height == 0) Log("[SimXR]   - Invalid dimensions");
                if (td.MipLevels == 0) Log("[SimXR]   - MipLevels is 0");
            }
            return XR_ERROR_RUNTIME_FAILURE; 
        }
        Logf("[SimXR] Created swapchain texture[%u]: %p", i, tex.Get());
        chain.images.push_back(std::move(tex));
    }
    rt::g_swapchains.emplace(chain.handle, std::move(chain));
    *sc = chain.handle;
    Logf("[SimXR] xrCreateSwapchain: sc=%p fmt=%d %ux%u array=%u samples=%u", *sc, (int)ci->format, ci->width, ci->height, ci->arraySize, ci->sampleCount);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrEnumerateSwapchainImages_runtime(XrSwapchain sc, uint32_t capacity, uint32_t* count, XrSwapchainImageBaseHeader* images) {
    auto it = rt::g_swapchains.find(sc); if (it == rt::g_swapchains.end()) return XR_ERROR_HANDLE_INVALID;
    const uint32_t n = (uint32_t)it->second.images.size();
    if (count) *count = n;
    if (capacity >= n && images) {
        auto* arr = reinterpret_cast<XrSwapchainImageD3D11KHR*>(images);
        for (uint32_t i = 0; i < n; ++i) { arr[i].type = XR_TYPE_SWAPCHAIN_IMAGE_D3D11_KHR; arr[i].texture = it->second.images[i].Get(); }
    }
    Logf("[SimXR] xrEnumerateSwapchainImages: sc=%p count=%u", sc, n);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrAcquireSwapchainImage_runtime(XrSwapchain sc, const XrSwapchainImageAcquireInfo*, uint32_t* index) {
    auto it = rt::g_swapchains.find(sc); if (it == rt::g_swapchains.end()) return XR_ERROR_HANDLE_INVALID;
    auto& ch = it->second;
    uint32_t i = ch.nextIndex;
    ch.nextIndex = (ch.nextIndex + 1) % ch.imageCount;
    ch.lastAcquired = i;  // Track what we just gave to the app
    if (index) *index = i; 
    
    static int acquireCount = 0;
    if (++acquireCount % 60 == 1) {  // Log every 60 calls
        Logf("[SimXR] xrAcquireSwapchainImage: sc=%p idx=%u (format=%d, %ux%u)", 
             sc, i, (int)ch.format, ch.width, ch.height);
    }
    return XR_SUCCESS;
}
static XrResult XRAPI_PTR xrWaitSwapchainImage_runtime(XrSwapchain, const XrSwapchainImageWaitInfo*) { return XR_SUCCESS; }
static XrResult XRAPI_PTR xrReleaseSwapchainImage_runtime(XrSwapchain sc, const XrSwapchainImageReleaseInfo*) {
    auto& ch = rt::g_swapchains[sc];
    // The app just released the image it acquired earlier
    ch.lastReleased = ch.lastAcquired;
    
    static int releaseCount = 0;
    if (++releaseCount % 60 == 1) {  // Log every 60 calls
        Logf("[SimXR] xrReleaseSwapchainImage: sc=%p released=%u", sc, ch.lastReleased);
    }
    return XR_SUCCESS;
}

// Helper struct to save and restore D3D11 context state using RAII
struct D3D11StateBackup {
    D3D11StateBackup(ID3D11DeviceContext* ctx) : ctx_(ctx) {
        // IA
        ctx_->IAGetInputLayout(&ia_input_layout);
        ctx_->IAGetPrimitiveTopology(&ia_primitive_topology);
        // RS
        rs_num_viewports = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
        ctx_->RSGetViewports(&rs_num_viewports, rs_viewports);
        rs_num_scissor_rects = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
        ctx_->RSGetScissorRects(&rs_num_scissor_rects, rs_scissor_rects);
        ctx_->RSGetState(&rs_state);
        // OM
        ctx_->OMGetRenderTargets(D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, om_rtvs, &om_dsv);
        ctx_->OMGetBlendState(&om_blend_state, om_blend_factor, &om_sample_mask);
        ctx_->OMGetDepthStencilState(&om_depth_stencil_state, &om_stencil_ref);
        // Shaders - MUST initialize class instance counts before calling GetShader
        ps_num_class_instances = 256;  // Initialize to array capacity
        ctx_->PSGetShader(&ps_shader, ps_class_instances, &ps_num_class_instances);
        ctx_->PSGetSamplers(0, D3D11_COMMONSHADER_SAMPLER_SLOT_COUNT, ps_samplers);
        ctx_->PSGetShaderResources(0, D3D11_COMMONSHADER_INPUT_RESOURCE_SLOT_COUNT, ps_srvs);
        vs_num_class_instances = 256;  // Initialize to array capacity
        ctx_->VSGetShader(&vs_shader, vs_class_instances, &vs_num_class_instances);
    }

    ~D3D11StateBackup() {
        // Restore state
        ctx_->IASetInputLayout(ia_input_layout);
        ctx_->IASetPrimitiveTopology(ia_primitive_topology);
        ctx_->RSSetViewports(rs_num_viewports, rs_viewports);
        ctx_->RSSetScissorRects(rs_num_scissor_rects, rs_scissor_rects);
        ctx_->RSSetState(rs_state);
        ctx_->OMSetRenderTargets(D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, om_rtvs, om_dsv);
        ctx_->OMSetBlendState(om_blend_state, om_blend_factor, om_sample_mask);
        ctx_->OMSetDepthStencilState(om_depth_stencil_state, om_stencil_ref);
        ctx_->PSSetShader(ps_shader, ps_class_instances, ps_num_class_instances);
        ctx_->PSSetSamplers(0, D3D11_COMMONSHADER_SAMPLER_SLOT_COUNT, ps_samplers);
        ctx_->PSSetShaderResources(0, D3D11_COMMONSHADER_INPUT_RESOURCE_SLOT_COUNT, ps_srvs);
        ctx_->VSSetShader(vs_shader, vs_class_instances, vs_num_class_instances);

        // Release COM references
        if (ia_input_layout) ia_input_layout->Release();
        if (rs_state) rs_state->Release();
        for (UINT i = 0; i < D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT; ++i) if (om_rtvs[i]) om_rtvs[i]->Release();
        if (om_dsv) om_dsv->Release();
        if (om_blend_state) om_blend_state->Release();
        if (om_depth_stencil_state) om_depth_stencil_state->Release();
        if (ps_shader) ps_shader->Release();
        for (UINT i = 0; i < ps_num_class_instances; ++i) if (ps_class_instances[i]) ps_class_instances[i]->Release();
        for (UINT i = 0; i < D3D11_COMMONSHADER_SAMPLER_SLOT_COUNT; ++i) if (ps_samplers[i]) ps_samplers[i]->Release();
        for (UINT i = 0; i < D3D11_COMMONSHADER_INPUT_RESOURCE_SLOT_COUNT; ++i) if (ps_srvs[i]) ps_srvs[i]->Release();
        if (vs_shader) vs_shader->Release();
        for (UINT i = 0; i < vs_num_class_instances; ++i) if (vs_class_instances[i]) vs_class_instances[i]->Release();
    }

private:
    ID3D11DeviceContext* ctx_;
    // IA State
    ID3D11InputLayout* ia_input_layout = nullptr;
    D3D11_PRIMITIVE_TOPOLOGY ia_primitive_topology = D3D11_PRIMITIVE_TOPOLOGY_UNDEFINED;
    // RS State  
    UINT rs_num_viewports = 0, rs_num_scissor_rects = 0;
    D3D11_VIEWPORT rs_viewports[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE];
    D3D11_RECT rs_scissor_rects[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE];
    ID3D11RasterizerState* rs_state = nullptr;
    // OM State
    ID3D11RenderTargetView* om_rtvs[D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT] = { nullptr };
    ID3D11DepthStencilView* om_dsv = nullptr;
    ID3D11BlendState* om_blend_state = nullptr;
    FLOAT om_blend_factor[4] = { 0.0f };
    UINT om_sample_mask = 0;
    ID3D11DepthStencilState* om_depth_stencil_state = nullptr;
    UINT om_stencil_ref = 0;
    // PS State
    ID3D11PixelShader* ps_shader = nullptr;
    ID3D11ClassInstance* ps_class_instances[256] = { nullptr };
    UINT ps_num_class_instances = 0;
    ID3D11SamplerState* ps_samplers[D3D11_COMMONSHADER_SAMPLER_SLOT_COUNT] = { nullptr };
    ID3D11ShaderResourceView* ps_srvs[D3D11_COMMONSHADER_INPUT_RESOURCE_SLOT_COUNT] = { nullptr };
    // VS State
    ID3D11VertexShader* vs_shader = nullptr;
    ID3D11ClassInstance* vs_class_instances[256] = { nullptr };
    UINT vs_num_class_instances = 0;
};

namespace rt {
    static XrSessionState g_state = XR_SESSION_STATE_IDLE;
    static std::vector<XrEventDataBuffer> g_eventQueue;
    void PushState(XrSession s, XrSessionState ns) {
        g_state = ns;
        const char* stateName = "UNKNOWN";
        switch(ns) {
            case XR_SESSION_STATE_IDLE: stateName = "IDLE"; break;
            case XR_SESSION_STATE_READY: stateName = "READY"; break;
            case XR_SESSION_STATE_SYNCHRONIZED: stateName = "SYNCHRONIZED"; break;
            case XR_SESSION_STATE_VISIBLE: stateName = "VISIBLE"; break;
            case XR_SESSION_STATE_FOCUSED: stateName = "FOCUSED"; break;
            case XR_SESSION_STATE_STOPPING: stateName = "STOPPING"; break;
            case XR_SESSION_STATE_LOSS_PENDING: stateName = "LOSS_PENDING"; break;
            case XR_SESSION_STATE_EXITING: stateName = "EXITING"; break;
        }
        Logf("[SimXR] PushState: Session %llu -> %s", (unsigned long long)s, stateName);
        
        XrEventDataSessionStateChanged e{XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED};
        e.session = s; e.state = ns; e.time = 0;
        
        XrEventDataBuffer buf{};
        buf.type = XR_TYPE_EVENT_DATA_BUFFER;  // Set the base type
        std::memcpy(&buf, &e, sizeof(e));
        g_eventQueue.push_back(buf);
        Logf("[SimXR] Event queue now has %zu events", g_eventQueue.size());
    }
}
static XrResult XRAPI_PTR xrPollEvent_runtime(XrInstance, XrEventDataBuffer* b) {
    static int pollCount = 0;
    pollCount++;
    
    if (pollCount <= 5) {  // Log first few polls
        Logf("[SimXR] xrPollEvent called (#%d), queue size=%zu", pollCount, rt::g_eventQueue.size());
    }
    
    if (!b) return XR_ERROR_VALIDATION_FAILURE;
    if (rt::g_eventQueue.empty()) {
        if (pollCount <= 5) {
            Log("[SimXR] xrPollEvent: No events available (XR_EVENT_UNAVAILABLE)");
        }
        return XR_EVENT_UNAVAILABLE;
    }
    *b = rt::g_eventQueue.front();
    rt::g_eventQueue.erase(rt::g_eventQueue.begin());
    
    // Log what event we're delivering
    const XrEventDataBaseHeader* header = reinterpret_cast<const XrEventDataBaseHeader*>(b);
    if (header->type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
        const XrEventDataSessionStateChanged* stateEvent = reinterpret_cast<const XrEventDataSessionStateChanged*>(b);
        const char* stateName = "UNKNOWN";
        switch(stateEvent->state) {
            case XR_SESSION_STATE_IDLE: stateName = "IDLE"; break;
            case XR_SESSION_STATE_READY: stateName = "READY"; break;
            case XR_SESSION_STATE_SYNCHRONIZED: stateName = "SYNCHRONIZED"; break;
            case XR_SESSION_STATE_VISIBLE: stateName = "VISIBLE"; break;
            case XR_SESSION_STATE_FOCUSED: stateName = "FOCUSED"; break;
            case XR_SESSION_STATE_STOPPING: stateName = "STOPPING"; break;
            case XR_SESSION_STATE_LOSS_PENDING: stateName = "LOSS_PENDING"; break;
            case XR_SESSION_STATE_EXITING: stateName = "EXITING"; break;
        }
        Logf("[SimXR] xrPollEvent: Delivering SESSION_STATE_CHANGED -> %s (session=%llu, %zu events left)", 
            stateName, (unsigned long long)stateEvent->session, rt::g_eventQueue.size());
    } else {
        Logf("[SimXR] xrPollEvent: Delivering event type %d (%zu events left)", header->type, rt::g_eventQueue.size());
    }
    return XR_SUCCESS;
}
static XrResult XRAPI_PTR xrBeginSession_runtime(XrSession s, const XrSessionBeginInfo*) { 
    Log("[SimXR] ============================================");
    Logf("[SimXR] xrBeginSession called (session=%llu)", (unsigned long long)s);
    Log("[SimXR] Session started - moving to SYNCHRONIZED/VISIBLE states");
    Log("[SimXR] ============================================");
    rt::PushState(s, XR_SESSION_STATE_SYNCHRONIZED); 
    rt::PushState(s, XR_SESSION_STATE_VISIBLE);
    // Only push FOCUSED if window is actually active/focused
    if (rt::g_session.hwnd && rt::g_session.isFocused) {
        rt::PushState(s, XR_SESSION_STATE_FOCUSED);
    }
    return XR_SUCCESS; 
}
static XrResult XRAPI_PTR xrEndSession_runtime(XrSession s) { Log("[SimXR] xrEndSession"); rt::PushState(s, XR_SESSION_STATE_STOPPING); rt::PushState(s, XR_SESSION_STATE_IDLE); return XR_SUCCESS; }
static XrResult XRAPI_PTR xrRequestExitSession_runtime(XrSession s) { rt::PushState(s, XR_SESSION_STATE_EXITING); return XR_SUCCESS; }
static XrResult XRAPI_PTR xrWaitFrame_runtime(XrSession, const XrFrameWaitInfo*, XrFrameState* s) {
    if (!s) return XR_ERROR_VALIDATION_FAILURE;
    // Message pump so the preview window stays responsive
    MSG msg; while (PeekMessage(&msg, nullptr, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessage(&msg); }
    static LARGE_INTEGER freq = [](){ LARGE_INTEGER f; QueryPerformanceFrequency(&f); return f; }();
    static double periodSec = 1.0 / 90.0;
    static long long periodNs = (long long)(periodSec * 1e9);
    static double nextTick = [](){ LARGE_INTEGER t; QueryPerformanceCounter(&t); return (double)t.QuadPart; }();
    
    // Handle WASD keyboard input for movement (relative to head orientation)
    if (rt::g_session.isFocused) {
        const float moveSpeed = 3.0f;  // meters per second
        float deltaTime = (float)periodSec;

        XrQuaternionf headQ = rt::QuatFromYawPitch(rt::g_headYaw, rt::g_headPitch);
        XrVector3f fwd = rt::RotateVectorByQuaternion(headQ, XrVector3f{0.0f, 0.0f, -1.0f});
        XrVector3f right = rt::RotateVectorByQuaternion(headQ, XrVector3f{1.0f, 0.0f, 0.0f});

        if (GetAsyncKeyState('W') & 0x8000) {
            rt::g_headPos.x += fwd.x * moveSpeed * deltaTime;
            rt::g_headPos.y += fwd.y * moveSpeed * deltaTime;
            rt::g_headPos.z += fwd.z * moveSpeed * deltaTime;
        }
        if (GetAsyncKeyState('S') & 0x8000) {
            rt::g_headPos.x -= fwd.x * moveSpeed * deltaTime;
            rt::g_headPos.y -= fwd.y * moveSpeed * deltaTime;
            rt::g_headPos.z -= fwd.z * moveSpeed * deltaTime;
        }
        if (GetAsyncKeyState('A') & 0x8000) {
            rt::g_headPos.x -= right.x * moveSpeed * deltaTime;
            rt::g_headPos.y -= right.y * moveSpeed * deltaTime;
            rt::g_headPos.z -= right.z * moveSpeed * deltaTime;
        }
        if (GetAsyncKeyState('D') & 0x8000) {
            rt::g_headPos.x += right.x * moveSpeed * deltaTime;
            rt::g_headPos.y += right.y * moveSpeed * deltaTime;
            rt::g_headPos.z += right.z * moveSpeed * deltaTime;
        }
        if (GetAsyncKeyState('Q') & 0x8000) {
            rt::g_headPos.y -= moveSpeed * deltaTime;
        }
        if (GetAsyncKeyState('E') & 0x8000) {
            rt::g_headPos.y += moveSpeed * deltaTime;
        }
    }
    
    for (;;) {
        LARGE_INTEGER now; QueryPerformanceCounter(&now);
        double dt = (nextTick - (double)now.QuadPart) / (double)freq.QuadPart;
        if (dt <= 0.0) break;
        double ms = dt * 1000.0;
        if (ms > 5.0) ms = 5.0;
        if (ms < 0.0) ms = 0.0;
        Sleep((DWORD)ms);
    }
    nextTick += periodSec * (double)freq.QuadPart;
    LARGE_INTEGER now; QueryPerformanceCounter(&now);
    // Convert QPC to nanoseconds using double to avoid overflow on MSVC
    XrTime nowTime = (XrTime)((double)now.QuadPart * 1000000000.0 / (double)freq.QuadPart);
    s->type = XR_TYPE_FRAME_STATE; s->shouldRender = XR_TRUE; s->predictedDisplayPeriod = periodNs; s->predictedDisplayTime = nowTime + periodNs;
    return XR_SUCCESS;
}
static XrResult XRAPI_PTR xrBeginFrame_runtime(XrSession, const XrFrameBeginInfo*) { return XR_SUCCESS; }

static void ensurePreviewSized(rt::Session& s, UINT width, UINT height, DXGI_FORMAT format) {
    if (s.previewSwapchain && s.previewWidth == width && s.previewHeight == height && s.previewFormat == format) return;
    
    s.previewSwapchain.Reset(); 
    s.previewWidth = width; 
    s.previewHeight = height;
    s.previewFormat = format;

    // Register window class if not done (use global flag)
    if (!rt::g_windowClassRegistered) {
        WNDCLASSW wc{}; 
        wc.lpfnWndProc = rt::WndProc; 
        wc.hInstance = GetModuleHandleW(nullptr); 
        wc.lpszClassName = L"OpenXR Simulator";
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
        wc.hbrBackground = (HBRUSH)GetStockObject(BLACK_BRUSH);
        RegisterClassW(&wc);
        rt::g_windowClassRegistered = true;
    }

    if (!s.hwnd) {
        // Check if we have a persistent window from a previous session
        {
            std::lock_guard<std::mutex> lock(rt::g_windowMutex);
            if (rt::g_persistentWindow && IsWindow(rt::g_persistentWindow)) {
                s.hwnd = rt::g_persistentWindow;
                // DON'T clear g_persistentWindow - keep it for reference
                
                // Reuse swapchain if compatible
                if (rt::g_persistentSwapchain && rt::g_persistentWidth == width && 
                    rt::g_persistentHeight == height && s.previewFormat == format) {
                    s.previewSwapchain = rt::g_persistentSwapchain;
                    s.previewWidth = rt::g_persistentWidth;
                    s.previewHeight = rt::g_persistentHeight;
                    s.previewFormat = format;
                    Log("[SimXR] Reusing existing window AND swapchain from previous session");
                    return;  // Everything is already set up
                }
                
                Log("[SimXR] Reusing existing window from previous session (recreating swapchain)");
                
                // Just resize the existing window if needed
                RECT rc = { 0, 0, (LONG)width, (LONG)height };
                AdjustWindowRect(&rc, WS_OVERLAPPEDWINDOW, FALSE);
                SetWindowPos(s.hwnd, nullptr, 0, 0, rc.right - rc.left, rc.bottom - rc.top, SWP_NOMOVE | SWP_NOZORDER | SWP_SHOWWINDOW);
                
                // Make sure it's visible
                ShowWindow(s.hwnd, SW_SHOW);
                UpdateWindow(s.hwnd);
            }
        }
        
        // Create new window if we don't have one
        if (!s.hwnd) {
            RECT rc = { 0, 0, (LONG)width, (LONG)height };
            AdjustWindowRect(&rc, WS_OVERLAPPEDWINDOW, FALSE);
            s.hwnd = CreateWindowExW(0, L"OpenXR Simulator", L"OpenXR Simulator (Mouse Look + WASD)", WS_OVERLAPPEDWINDOW,
                                     100, 100, rc.right - rc.left, rc.bottom - rc.top, nullptr, nullptr, GetModuleHandleW(nullptr), nullptr);
            if (!s.hwnd) {
                Log("[SimXR] Failed to create preview window!");
                return;
            }
            ShowWindow(s.hwnd, SW_SHOW);
            UpdateWindow(s.hwnd);
            SetForegroundWindow(s.hwnd);
            SetWindowPos(s.hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW);
            Logf("[SimXR] Created new preview window: hwnd=%p size=%ux%u", s.hwnd, width, height);
            
            // Also save to persistent storage right away
            {
                std::lock_guard<std::mutex> lock(rt::g_windowMutex);
                rt::g_persistentWindow = s.hwnd;
                Log("[SimXR] Saved new window to persistent storage");
            }
        }
    } else {
        // Resize existing window
        RECT rc = { 0, 0, (LONG)width, (LONG)height };
        AdjustWindowRect(&rc, WS_OVERLAPPEDWINDOW, FALSE);
        SetWindowPos(s.hwnd, nullptr, 0, 0, rc.right - rc.left, rc.bottom - rc.top, SWP_NOMOVE | SWP_NOZORDER | SWP_SHOWWINDOW);
        Logf("[SimXR] Resized preview window: hwnd=%p size=%ux%u", s.hwnd, width, height);
    }
    ComPtr<IDXGIDevice> dxgiDev; s.d3d11Device.As(&dxgiDev);
    ComPtr<IDXGIAdapter> adapter; dxgiDev->GetAdapter(adapter.GetAddressOf());
    ComPtr<IDXGIFactory2> factory; adapter->GetParent(IID_PPV_ARGS(factory.GetAddressOf()));
    DXGI_SWAP_CHAIN_DESC1 desc{}; 
    desc.Format = format;  // Use the format from the XR swapchain
    desc.Width = width; 
    desc.Height = height;
    desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT; 
    desc.BufferCount = 2; 
    desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD; 
    desc.SampleDesc.Count = 1;
    HRESULT hr = factory->CreateSwapChainForHwnd(s.d3d11Device.Get(), s.hwnd, &desc, nullptr, nullptr, s.previewSwapchain.GetAddressOf());
    Logf("[SimXR] ensurePreviewSized: CreateSwapChainForHwnd hr=0x%08X swapchain=%p format=%d", (unsigned)hr, s.previewSwapchain.Get(), format);
    
    // Try fallback formats if the requested format fails
    if (FAILED(hr)) {
        Logf("[SimXR] ERROR: Failed to create swapchain with format %d, trying fallbacks", format);
        
        // If sRGB failed, try UNORM (but note this may cause gamma issues)
        if (format == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB) {
            desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
            hr = factory->CreateSwapChainForHwnd(s.d3d11Device.Get(), s.hwnd, &desc, nullptr, nullptr, s.previewSwapchain.GetAddressOf());
            if (SUCCEEDED(hr)) {
                s.previewFormat = DXGI_FORMAT_R8G8B8A8_UNORM;
                Logf("[SimXR] Fallback to RGBA_UNORM (may have gamma issues): hr=0x%08X", (unsigned)hr);
            }
        }
        
        // Try BGRA formats if RGBA failed
        if (FAILED(hr)) {
            desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM_SRGB;
            hr = factory->CreateSwapChainForHwnd(s.d3d11Device.Get(), s.hwnd, &desc, nullptr, nullptr, s.previewSwapchain.GetAddressOf());
            if (SUCCEEDED(hr)) {
                s.previewFormat = DXGI_FORMAT_B8G8R8A8_UNORM_SRGB;
                Logf("[SimXR] Fallback succeeded with BGRA_SRGB: hr=0x%08X", (unsigned)hr);
            } else {
                // Final fallback to BGRA UNORM
                desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
                hr = factory->CreateSwapChainForHwnd(s.d3d11Device.Get(), s.hwnd, &desc, nullptr, nullptr, s.previewSwapchain.GetAddressOf());
                if (SUCCEEDED(hr)) {
                    s.previewFormat = DXGI_FORMAT_B8G8R8A8_UNORM;
                    Logf("[SimXR] Fallback to BGRA_UNORM (may have gamma issues): hr=0x%08X", (unsigned)hr);
                } else {
                    Logf("[SimXR] ERROR: All swapchain formats failed! Last hr=0x%08X", (unsigned)hr);
                }
            }
        }
    }
}

static void blitViewToHalf(rt::Session& s, rt::Swapchain& chain, uint32_t srcIndex, uint32_t arraySlice, bool leftHalf,
                           const XrRect2Di& rect, ID3D11RenderTargetView* rtv) {
    if (!rtv) return;
    
    if (!rt::InitBlitResources(s)) {
        Log("[SimXR] Cannot blit, blit resources failed to initialize.");
        return;
    }

    // Check if we have a valid image
    if (srcIndex >= chain.images.size() || !chain.images[srcIndex]) {
        Logf("[SimXR] blitViewToHalf: Invalid srcIndex %u (size=%zu)", srcIndex, chain.images.size());
        return;
    }

    // Prepare source texture
    ComPtr<ID3D11Texture2D> sourceTexture = chain.images[srcIndex];
    D3D11_TEXTURE2D_DESC srcDesc;
    sourceTexture->GetDesc(&srcDesc);
    
    // Skip depth formats - they can't be rendered to the preview window
    bool isDepthFormat = (srcDesc.Format == DXGI_FORMAT_D32_FLOAT || 
                          srcDesc.Format == DXGI_FORMAT_D24_UNORM_S8_UINT || 
                          srcDesc.Format == DXGI_FORMAT_D16_UNORM);
    if (isDepthFormat) {
        // Depth swapchains are for depth testing, not preview rendering
        return;
    }
    
    // Choose a typed format for SRV and temp texture  
    // Preserve sRGB if original was sRGB to enable auto-conversion
    DXGI_FORMAT typedFormat = srcDesc.Format;
    switch (srcDesc.Format) {
        case DXGI_FORMAT_R8G8B8A8_TYPELESS:
            typedFormat = (chain.format == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB) ? DXGI_FORMAT_R8G8B8A8_UNORM_SRGB : DXGI_FORMAT_R8G8B8A8_UNORM;
            break;
        case DXGI_FORMAT_B8G8R8A8_TYPELESS:
            typedFormat = (chain.format == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB) ? DXGI_FORMAT_B8G8R8A8_UNORM_SRGB : DXGI_FORMAT_B8G8R8A8_UNORM;
            break;
        case DXGI_FORMAT_R16G16B16A16_TYPELESS: 
            typedFormat = DXGI_FORMAT_R16G16B16A16_FLOAT; 
            break;
        case DXGI_FORMAT_R32G32B32A32_TYPELESS: 
            typedFormat = DXGI_FORMAT_R32G32B32A32_FLOAT; 
            break;
        case DXGI_FORMAT_R10G10B10A2_TYPELESS: 
            typedFormat = DXGI_FORMAT_R10G10B10A2_UNORM; 
            break;
        default: 
            break; // Already typed or unknown
    }
    
    
    // ALWAYS create a temp texture to avoid SRV/RTV binding conflicts
    // The app may still have this texture bound as an RTV, and D3D11 will null the SRV if we try to use it directly
    ComPtr<ID3D11Texture2D> viewTexture;
    
    // Always make an SRV-only single-slice, single-sample texture
    // Use the typed format to avoid CreateShaderResourceView failures
    D3D11_TEXTURE2D_DESC tempDesc = {};
    tempDesc.Width = srcDesc.Width;
    tempDesc.Height = srcDesc.Height;
    tempDesc.MipLevels = 1;
    tempDesc.ArraySize = 1;
    tempDesc.Format = typedFormat;  // Make temp texture in same format as SRV (may be *_SRGB)
    tempDesc.SampleDesc.Count = 1;
    tempDesc.SampleDesc.Quality = 0;
    tempDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    tempDesc.Usage = D3D11_USAGE_DEFAULT;
    tempDesc.CPUAccessFlags = 0;
    tempDesc.MiscFlags = 0;

    HRESULT hr = s.d3d11Device->CreateTexture2D(&tempDesc, nullptr, viewTexture.GetAddressOf());
    if (FAILED(hr)) {
        Logf("[SimXR] Failed to create temp texture for blit: 0x%08X", hr);
        return;
    }

    // Copy the correct subresource (handles array slice)
    UINT srcSubresource = D3D11CalcSubresource(0, arraySlice, chain.mipCount);
    
    // Handle imageRect cropping for better visual accuracy
    // Apps can submit sub-rects, and sampling the full texture can show mostly black areas
    if (srcDesc.SampleDesc.Count == 1 && rect.extent.width > 0 && rect.extent.height > 0 &&
        (rect.extent.width < (int32_t)srcDesc.Width || rect.extent.height < (int32_t)srcDesc.Height)) {
        
        // Recreate temp texture with rect size for cropped copying
        tempDesc.Width  = rect.extent.width;
        tempDesc.Height = rect.extent.height;
        tempDesc.Format = typedFormat;  // Ensure format stays correct
        viewTexture.Reset();
        HRESULT hr = s.d3d11Device->CreateTexture2D(&tempDesc, nullptr, viewTexture.GetAddressOf());
        if (FAILED(hr)) {
            Logf("[SimXR] Failed to create cropped temp texture: 0x%08X", hr);
            return;
        }
        
        // Copy only the specified rect
        D3D11_BOX box{};
        box.left = rect.offset.x;
        box.top = rect.offset.y;
        box.right = rect.offset.x + rect.extent.width;
        box.bottom = rect.offset.y + rect.extent.height;
        box.front = 0; 
        box.back = 1;
        
        s.d3d11Context->CopySubresourceRegion(viewTexture.Get(), 0, 0, 0, 0, sourceTexture.Get(), srcSubresource, &box);
        
        Logf("[SimXR] Applied imageRect cropping: %dx%d from (%d,%d)", 
             rect.extent.width, rect.extent.height, rect.offset.x, rect.offset.y);
    } else {
        // Copy full texture or handle MSAA
        if (srcDesc.SampleDesc.Count > 1) {
            // If app used MSAA, resolve it first using the typed format
            s.d3d11Context->ResolveSubresource(viewTexture.Get(), 0, sourceTexture.Get(), srcSubresource, typedFormat);
        } else {
            // Otherwise just copy the full texture
            s.d3d11Context->CopySubresourceRegion(viewTexture.Get(), 0, 0, 0, 0, sourceTexture.Get(), srcSubresource, nullptr);
        }
    }

    // Create Shader Resource View
    D3D11_SHADER_RESOURCE_VIEW_DESC srvDesc = {};
    srvDesc.Format = typedFormat; // Use the proper typed format
    srvDesc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    srvDesc.Texture2D.MipLevels = 1;
    srvDesc.Texture2D.MostDetailedMip = 0;

    ComPtr<ID3D11ShaderResourceView> srv;
    hr = s.d3d11Device->CreateShaderResourceView(viewTexture.Get(), &srvDesc, srv.GetAddressOf());
    if (FAILED(hr)) {
        Logf("[SimXR] Failed to create SRV: 0x%08X", hr);
        return;
    }

    // Set viewport for left or right half
    D3D11_VIEWPORT vp = {};
    vp.TopLeftX = leftHalf ? 0.0f : (float)s.previewWidth / 2.0f;
    vp.TopLeftY = 0.0f;
    vp.Width = (float)s.previewWidth / 2.0f;
    vp.Height = (float)s.previewHeight;
    vp.MinDepth = 0.0f;
    vp.MaxDepth = 1.0f;
    s.d3d11Context->RSSetViewports(1, &vp);

    // Set shaders and resources
    s.d3d11Context->VSSetShader(s.blitVS.Get(), nullptr, 0);
    s.d3d11Context->PSSetShader(s.blitPS.Get(), nullptr, 0);

    ID3D11ShaderResourceView* srvs[] = { srv.Get() };
    s.d3d11Context->PSSetShaderResources(0, 1, srvs);
    ID3D11SamplerState* samplers[] = { s.samplerState.Get() };
    s.d3d11Context->PSSetSamplers(0, 1, samplers);

    // Set pipeline state
    s.d3d11Context->IASetInputLayout(nullptr);
    s.d3d11Context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
    s.d3d11Context->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
    s.d3d11Context->OMSetDepthStencilState(nullptr, 0);
    s.d3d11Context->RSSetState(s.noCullRS.Get());  // Use no-cull rasterizer state to prevent triangle culling

    // Bind render target
    ID3D11RenderTargetView* rtvs[1] = { rtv };
    s.d3d11Context->OMSetRenderTargets(1, rtvs, nullptr);

    // Draw fullscreen quad
    s.d3d11Context->Draw(4, 0);
    
    // Unbind SRV to avoid conflicts with future RTV usage
    ID3D11ShaderResourceView* nullSRV[1] = { nullptr };
    s.d3d11Context->PSSetShaderResources(0, 1, nullSRV);
    
    Logf("[SimXR] blitViewToHalf (Shader): srcIdx=%u slice=%u left=%d typedFmt=%d", 
         srcIndex, arraySlice, leftHalf ? 1 : 0, typedFormat);
}

static void presentProjection(rt::Session& s, const XrCompositionLayerProjection& proj) {
    Log("[SimXR] ============================================");
    Logf("[SimXR] presentProjection called: viewCount=%u", proj.viewCount);
    Log("[SimXR] RENDERING FRAME TO PREVIEW WINDOW");
    Log("[SimXR] ============================================");
    if (proj.viewCount < 1) {
        Log("[SimXR] presentProjection: No views, returning");
        return;
    }
    const auto& vL = proj.views[0];
    auto itL = rt::g_swapchains.find(vL.subImage.swapchain); 
    if (itL == rt::g_swapchains.end()) {
        Log("[SimXR] presentProjection: Left swapchain not found");
        return;
    }
    auto& chL = itL->second;
    uint32_t width = chL.width, height = chL.height;
    const rt::Swapchain* chRPtr = &chL;
    if (proj.viewCount > 1) {
        const auto& vR = proj.views[1];
        auto itR = rt::g_swapchains.find(vR.subImage.swapchain);
        if (itR != rt::g_swapchains.end()) {
            chRPtr = &itR->second;
            if (itR->second.width > width) width = itR->second.width;
            if (itR->second.height > height) height = itR->second.height;
        }
    }
    {
        std::lock_guard<std::mutex> lock(s.previewMutex);
        
        // Try to use sRGB format for proper gamma correction
        // If Unity renders to sRGB, we should display in sRGB too
        DXGI_FORMAT displayFormat = DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
        ensurePreviewSized(s, width * 2, height, displayFormat);
        
        if (!s.previewSwapchain) return;

        // Save D3D11 context state - will auto-restore when stateBackup goes out of scope
        D3D11StateBackup stateBackup(s.d3d11Context.Get());

        // Get the backbuffer and create RTV
        ComPtr<ID3D11Texture2D> bb;
        if (FAILED(s.previewSwapchain->GetBuffer(0, IID_PPV_ARGS(bb.GetAddressOf())))) {
            Log("[SimXR] Failed to get preview swapchain buffer.");
            return;
        }
        
        // Create explicit sRGB RTV for proper gamma encoding, even if backbuffer is UNORM
        DXGI_FORMAT bbFmt = s.previewFormat; // what we created the preview swapchain with
        DXGI_FORMAT rtvFmt = bbFmt;
        if (bbFmt == DXGI_FORMAT_R8G8B8A8_UNORM)       rtvFmt = DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
        else if (bbFmt == DXGI_FORMAT_B8G8R8A8_UNORM)  rtvFmt = DXGI_FORMAT_B8G8R8A8_UNORM_SRGB;

        D3D11_RENDER_TARGET_VIEW_DESC rtvDesc = {};
        rtvDesc.Format = rtvFmt;
        rtvDesc.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2D;
        rtvDesc.Texture2D.MipSlice = 0;

        ComPtr<ID3D11RenderTargetView> rtv;
        HRESULT hr = s.d3d11Device->CreateRenderTargetView(bb.Get(), &rtvDesc, rtv.GetAddressOf());
        if (FAILED(hr)) {
            // Fallback to nullptr desc if explicit sRGB RTV fails
            Logf("[SimXR] Explicit sRGB RTV failed (0x%08X), falling back to auto format", hr);
            if (FAILED(s.d3d11Device->CreateRenderTargetView(bb.Get(), nullptr, rtv.GetAddressOf()))) {
                Log("[SimXR] Failed to create RTV for preview.");
                return;
            }
        } else {
            Logf("[SimXR] Created explicit sRGB RTV: backbuffer=%d rtv=%d", bbFmt, rtvFmt);
        }
        
        // Bind RTV and clear
        ID3D11RenderTargetView* rtvs[1] = { rtv.Get() };
        s.d3d11Context->OMSetRenderTargets(1, rtvs, nullptr);
        
        // Clear the render target to dark blue background
        // This ensures we see a clean background if blitting fails
        const float clearColor[4] = {0.1f, 0.1f, 0.2f, 1.0f}; // Dark blue background
        s.d3d11Context->ClearRenderTargetView(rtv.Get(), clearColor);
        
        // Now blit both views using shader (passing the RTV)
        // Always use the most recently released image
        uint32_t leftIdx = 0;
        if (chL.lastReleased != UINT32_MAX && chL.lastReleased < chL.imageCount) {
            leftIdx = chL.lastReleased;
        } else if (chL.lastAcquired != UINT32_MAX && chL.lastAcquired < chL.imageCount) {
            leftIdx = chL.lastAcquired;
        }
        
        static int blitCount = 0;
        if (++blitCount % 60 == 1) {  // Log every 60 frames
            Logf("[SimXR] Blitting left eye: idx=%u (lastReleased=%u, lastAcquired=%u, imageCount=%u)", 
                 leftIdx, chL.lastReleased, chL.lastAcquired, chL.imageCount);
        }
        
        blitViewToHalf(s, chL, leftIdx, vL.subImage.imageArrayIndex, true, vL.subImage.imageRect, rtv.Get());
        
        if (proj.viewCount > 1) {
            const auto& vR = proj.views[1];
            auto& chR = const_cast<rt::Swapchain&>(*chRPtr);
            
            uint32_t rightIdx = 0;
            if (chR.lastReleased != UINT32_MAX && chR.lastReleased < chR.imageCount) {
                rightIdx = chR.lastReleased;
            } else if (chR.lastAcquired != UINT32_MAX && chR.lastAcquired < chR.imageCount) {
                rightIdx = chR.lastAcquired;
            }
            
            blitViewToHalf(s, chR, rightIdx, vR.subImage.imageArrayIndex, false, vR.subImage.imageRect, rtv.Get());
        } else {
            // Mirror left view if only one provided  
            blitViewToHalf(s, chL, leftIdx, vL.subImage.imageArrayIndex, false, vL.subImage.imageRect, rtv.Get());
        }
        
        // State is restored here by D3D11StateBackup destructor

        if (s.previewSwapchain) {
            // Process window messages to keep the window responsive
            MSG msg;
            while (PeekMessageW(&msg, s.hwnd, 0, 0, PM_REMOVE)) {
                TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
            
            HRESULT hr = s.previewSwapchain->Present(1, 0);
            if (FAILED(hr)) {
                Logf("[SimXR] Present failed: 0x%08X", hr);
            } else {
                Log("[SimXR] Present preview swapchain");
            }
        }
    }
}

static XrResult XRAPI_PTR xrEndFrame_runtime(XrSession, const XrFrameEndInfo* info) {
    static int frameCount = 0;
    frameCount++;
    
    // Log every frame for first 10 frames, then every 60 frames
    bool shouldLog = (frameCount <= 10) || (frameCount % 60 == 1);
    
    if (shouldLog) {
        Logf("[SimXR] xrEndFrame called (frame #%d)", frameCount);
    }
    
    if (!info) {
        Log("[SimXR] xrEndFrame: ERROR - info is null");
        return XR_ERROR_VALIDATION_FAILURE;
    }
    
    if (shouldLog) {
        Logf("[SimXR] xrEndFrame: layers=%u", info->layerCount);
    }
    
    // Find first projection layer and present it.
    bool foundProjection = false;
    for (uint32_t i = 0; i < info->layerCount; ++i) {
        const XrCompositionLayerBaseHeader* base = info->layers[i];
        if (base && base->type == XR_TYPE_COMPOSITION_LAYER_PROJECTION) {
            if (shouldLog) {
                Log("[SimXR] xrEndFrame: Found projection layer, calling presentProjection");
            }
            const auto* proj = reinterpret_cast<const XrCompositionLayerProjection*>(base);
            presentProjection(rt::g_session, *proj);
            foundProjection = true;
            break;
        }
    }
    
    if (!foundProjection && shouldLog) {
        Log("[SimXR] xrEndFrame: WARNING - No projection layers found!");
    }
    
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrLocateViews_runtime(XrSession, const XrViewLocateInfo* li, XrViewState* vs, uint32_t cap, uint32_t* outCount, XrView* views) {
    if (outCount) *outCount = 2;
    if (vs) { 
        vs->type = XR_TYPE_VIEW_STATE; 
        // Set both VALID and TRACKED bits so Unity knows this is a real tracked HMD
        vs->viewStateFlags = XR_VIEW_STATE_ORIENTATION_VALID_BIT | 
                            XR_VIEW_STATE_POSITION_VALID_BIT | 
                            XR_VIEW_STATE_ORIENTATION_TRACKED_BIT | 
                            XR_VIEW_STATE_POSITION_TRACKED_BIT; 
    }
    if (cap < 2 || !views) return XR_SUCCESS;
    const float ipd = 0.064f;
    
    // Use dynamic head pose from mouse look
    XrQuaternionf orientation = rt::QuatFromYawPitch(rt::g_headYaw, rt::g_headPitch);
    
    // Helper function to rotate a vector by a quaternion
    auto rotateVector = [](XrQuaternionf q, XrVector3f v) -> XrVector3f {
        // quaternion-vector rotation: q * v * q^-1
        XrQuaternionf qv{ v.x, v.y, v.z, 0 };
        XrQuaternionf qinv{ -q.x, -q.y, -q.z, q.w };
        
        // Quaternion multiplication: q * qv
        XrQuaternionf temp{
            q.w*qv.x + q.x*qv.w + q.y*qv.z - q.z*qv.y,
            q.w*qv.y - q.x*qv.z + q.y*qv.w + q.z*qv.x,
            q.w*qv.z + q.x*qv.y - q.y*qv.x + q.z*qv.w,
            q.w*qv.w - q.x*qv.x - q.y*qv.y - q.z*qv.z
        };
        
        // temp * qinv
        XrQuaternionf result{
            temp.w*qinv.x + temp.x*qinv.w + temp.y*qinv.z - temp.z*qinv.y,
            temp.w*qinv.y - temp.x*qinv.z + temp.y*qinv.w + temp.z*qinv.x,
            temp.w*qinv.z + temp.x*qinv.y - temp.y*qinv.x + temp.z*qinv.w,
            temp.w*qinv.w - temp.x*qinv.x - temp.y*qinv.y - temp.z*qinv.z
        };
        
        return XrVector3f{ result.x, result.y, result.z };
    };

    for (uint32_t i = 0; i < 2; ++i) {
        views[i].type = XR_TYPE_VIEW;
        views[i].pose.orientation = orientation;
        
        // Apply IPD offset in full head orientation space (yaw+pitch)
        // This fixes stereo geometry and eliminates warping when pitching
        float eyeOffset = (i == 0 ? -ipd * 0.5f : ipd * 0.5f);
        XrVector3f localEyeOffset{ eyeOffset, 0.0f, 0.0f };
        XrVector3f rotatedOffset = rotateVector(orientation, localEyeOffset);
        
        views[i].pose.position = {
            rt::g_headPos.x + rotatedOffset.x,
            rt::g_headPos.y + rotatedOffset.y,
            rt::g_headPos.z + rotatedOffset.z
        };
        
        // Narrower FOV for less warping on desktop (tan(35°) ≈ 0.7 for ~70° horizontal)
        // This reduces the "fish-eye" effect when viewing VR content on flat screen
        views[i].fov = { -0.7f, 0.7f, 0.7f, -0.7f };
    }
    static int locateCount = 0;
    if (++locateCount % 90 == 1) {  // Log every 90 frames (~1 second)
        Logf("[SimXR] xrLocateViews: pos=(%.2f,%.2f,%.2f) yaw=%.2f pitch=%.2f", 
             rt::g_headPos.x, rt::g_headPos.y, rt::g_headPos.z, 
             rt::g_headYaw, rt::g_headPitch);
    }
    return XR_SUCCESS;
}

// Add missing space/action functions for compatibility
static XrResult XRAPI_PTR xrCreateReferenceSpace_runtime(XrSession, const XrReferenceSpaceCreateInfo* info, XrSpace* space) {
    if (!info || !space) return XR_ERROR_VALIDATION_FAILURE;
    static uintptr_t nextSpace = 100;
    *space = (XrSpace)(nextSpace++);
    Logf("[SimXR] xrCreateReferenceSpace: type=%d space=%p", info->referenceSpaceType, *space);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrDestroySpace_runtime(XrSpace space) {
    Logf("[SimXR] xrDestroySpace: space=%p", space);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrLocateSpace_runtime(XrSpace space, XrSpace baseSpace, XrTime time, XrSpaceLocation* location) {
    if (!location) return XR_ERROR_VALIDATION_FAILURE;
    location->type = XR_TYPE_SPACE_LOCATION;
    location->locationFlags = XR_SPACE_LOCATION_POSITION_VALID_BIT | XR_SPACE_LOCATION_ORIENTATION_VALID_BIT;
    location->pose.orientation = {0,0,0,1};
    location->pose.position = {0,0,0};
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrEnumerateReferenceSpaces_runtime(XrSession, uint32_t capacity, uint32_t* count, XrReferenceSpaceType* spaces) {
    if (count) *count = 3;
    if (capacity >= 3 && spaces) {
        spaces[0] = XR_REFERENCE_SPACE_TYPE_VIEW;
        spaces[1] = XR_REFERENCE_SPACE_TYPE_LOCAL;
        spaces[2] = XR_REFERENCE_SPACE_TYPE_STAGE;
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrCreateActionSpace_runtime(XrSession, const XrActionSpaceCreateInfo* info, XrSpace* space) {
    if (!info || !space) return XR_ERROR_VALIDATION_FAILURE;
    static uintptr_t nextSpace = 200;
    *space = (XrSpace)(nextSpace++);
    Log("[SimXR] xrCreateActionSpace");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrCreateActionSet_runtime(XrInstance, const XrActionSetCreateInfo* info, XrActionSet* set) {
    if (!info || !set) return XR_ERROR_VALIDATION_FAILURE;
    static uintptr_t nextSet = 300;
    *set = (XrActionSet)(nextSet++);
    // actionSetName may not be null-terminated
    char setName[XR_MAX_ACTION_SET_NAME_SIZE + 1] = {0};
    memcpy(setName, info->actionSetName, XR_MAX_ACTION_SET_NAME_SIZE);
    Logf("[SimXR] xrCreateActionSet: name=%s", setName);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrDestroyActionSet_runtime(XrActionSet set) {
    Log("[SimXR] xrDestroyActionSet");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrCreateAction_runtime(XrActionSet, const XrActionCreateInfo* info, XrAction* action) {
    if (!info || !action) return XR_ERROR_VALIDATION_FAILURE;
    static uintptr_t nextAction = 400;
    *action = (XrAction)(nextAction++);
    // actionName may not be null-terminated
    char actName[XR_MAX_ACTION_NAME_SIZE + 1] = {0};
    memcpy(actName, info->actionName, XR_MAX_ACTION_NAME_SIZE);
    Logf("[SimXR] xrCreateAction: name=%s", actName);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrDestroyAction_runtime(XrAction action) {
    Log("[SimXR] xrDestroyAction");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrSuggestInteractionProfileBindings_runtime(XrInstance, const XrInteractionProfileSuggestedBinding* bindings) {
    if (!bindings) return XR_ERROR_VALIDATION_FAILURE;
    // interactionProfile is an XrPath (integer), not a C-string
    Logf("[SimXR] xrSuggestInteractionProfileBindings: profile=0x%llx",
         (unsigned long long)bindings->interactionProfile);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrAttachSessionActionSets_runtime(XrSession, const XrSessionActionSetsAttachInfo* info) {
    if (!info) return XR_ERROR_VALIDATION_FAILURE;
    Logf("[SimXR] xrAttachSessionActionSets: count=%u", info->countActionSets);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetActionStateBoolean_runtime(XrSession, const XrActionStateGetInfo* info, XrActionStateBoolean* state) {
    if (!info || !state) return XR_ERROR_VALIDATION_FAILURE;
    state->type = XR_TYPE_ACTION_STATE_BOOLEAN;
    state->currentState = XR_FALSE;
    state->changedSinceLastSync = XR_FALSE;
    state->lastChangeTime = 0;
    state->isActive = XR_FALSE;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetActionStateFloat_runtime(XrSession, const XrActionStateGetInfo* info, XrActionStateFloat* state) {
    if (!info || !state) return XR_ERROR_VALIDATION_FAILURE;
    state->type = XR_TYPE_ACTION_STATE_FLOAT;
    state->currentState = 0.0f;
    state->changedSinceLastSync = XR_FALSE;
    state->lastChangeTime = 0;
    state->isActive = XR_FALSE;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetActionStatePose_runtime(XrSession, const XrActionStateGetInfo* info, XrActionStatePose* state) {
    if (!info || !state) return XR_ERROR_VALIDATION_FAILURE;
    state->type = XR_TYPE_ACTION_STATE_POSE;
    state->isActive = XR_TRUE;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetActionStateVector2f_runtime(XrSession, const XrActionStateGetInfo* info, XrActionStateVector2f* state) {
    if (!info || !state) return XR_ERROR_VALIDATION_FAILURE;
    state->type = XR_TYPE_ACTION_STATE_VECTOR2F;
    state->currentState = {0.0f, 0.0f};
    state->changedSinceLastSync = XR_FALSE;
    state->lastChangeTime = 0;
    state->isActive = XR_FALSE;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrSyncActions_runtime(XrSession, const XrActionsSyncInfo* info) {
    if (!info) return XR_ERROR_VALIDATION_FAILURE;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrStringToPath_runtime(XrInstance, const char* pathString, XrPath* path) {
    if (!pathString || !path) return XR_ERROR_VALIDATION_FAILURE;
    // Simple hash as path ID
    size_t hash = 5381;
    for (const char* c = pathString; *c; ++c) {
        hash = ((hash << 5) + hash) + *c;
    }
    *path = (XrPath)hash;
    Logf("[SimXR] xrStringToPath: %s -> %llu", pathString, (unsigned long long)*path);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrPathToString_runtime(XrInstance, XrPath path, uint32_t bufferCapacityInput, uint32_t* bufferCountOutput, char* buffer) {
    const char* str = "/unknown/path";
    size_t len = strlen(str) + 1;
    if (bufferCountOutput) *bufferCountOutput = (uint32_t)len;
    if (buffer && bufferCapacityInput > 0) {
        strncpy(buffer, str, bufferCapacityInput - 1);
        buffer[bufferCapacityInput - 1] = '\0';
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetCurrentInteractionProfile_runtime(XrSession, XrPath topLevelUserPath, XrInteractionProfileState* interactionProfile) {
    if (!interactionProfile) return XR_ERROR_VALIDATION_FAILURE;
    interactionProfile->type = XR_TYPE_INTERACTION_PROFILE_STATE;
    interactionProfile->interactionProfile = XR_NULL_PATH;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrEnumerateBoundSourcesForAction_runtime(XrSession, const XrBoundSourcesForActionEnumerateInfo* info, uint32_t sourceCapacityInput, uint32_t* sourceCountOutput, XrPath* sources) {
    if (sourceCountOutput) *sourceCountOutput = 0;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetInputSourceLocalizedName_runtime(XrSession, const XrInputSourceLocalizedNameGetInfo* info, uint32_t bufferCapacityInput, uint32_t* bufferCountOutput, char* buffer) {
    const char* name = "Unknown";
    size_t len = strlen(name) + 1;
    if (bufferCountOutput) *bufferCountOutput = (uint32_t)len;
    if (buffer && bufferCapacityInput > 0) {
        strncpy(buffer, name, bufferCapacityInput - 1);
        buffer[bufferCapacityInput - 1] = '\0';
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrDestroySwapchain_runtime(XrSwapchain sc) {
    auto it = rt::g_swapchains.find(sc);
    if (it == rt::g_swapchains.end()) return XR_ERROR_HANDLE_INVALID;
    rt::g_swapchains.erase(it);
    Logf("[SimXR] xrDestroySwapchain: sc=%p", sc);
    return XR_SUCCESS;
}

// Missing functions Unity needs
static XrResult XRAPI_PTR xrResultToString_runtime(XrInstance, XrResult value, char buffer[XR_MAX_RESULT_STRING_SIZE]) {
    const char* str = "XR_SUCCESS";
    if (value != XR_SUCCESS) str = "XR_ERROR";
    strncpy(buffer, str, XR_MAX_RESULT_STRING_SIZE - 1);
    buffer[XR_MAX_RESULT_STRING_SIZE - 1] = '\0';
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrStructureTypeToString_runtime(XrInstance, XrStructureType value, char buffer[XR_MAX_STRUCTURE_NAME_SIZE]) {
    snprintf(buffer, XR_MAX_STRUCTURE_NAME_SIZE, "XrStructureType_%d", (int)value);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetReferenceSpaceBoundsRect_runtime(XrSession, XrReferenceSpaceType, XrExtent2Df* bounds) {
    if (!bounds) return XR_ERROR_VALIDATION_FAILURE;
    bounds->width = 3.0f;
    bounds->height = 3.0f;
    Log("[SimXR] xrGetReferenceSpaceBoundsRect: 3x3 meters");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetViewConfigurationProperties_runtime(XrInstance, XrSystemId, XrViewConfigurationType type, 
                                                                   XrViewConfigurationProperties* props) {
    if (!props) return XR_ERROR_VALIDATION_FAILURE;
    props->type = XR_TYPE_VIEW_CONFIGURATION_PROPERTIES;
    props->viewConfigurationType = type;
    props->fovMutable = XR_FALSE;
    Log("[SimXR] xrGetViewConfigurationProperties");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrApplyHapticFeedback_runtime(XrSession, const XrHapticActionInfo* info, const XrHapticBaseHeader* haptic) {
    // Just stub for now
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrStopHapticFeedback_runtime(XrSession, const XrHapticActionInfo* info) {
    // Just stub for now
    return XR_SUCCESS;
}

// Time conversion functions for XR_KHR_win32_convert_performance_counter_time
static XrResult XRAPI_PTR xrConvertWin32PerformanceCounterToTimeKHR_runtime(XrInstance instance,
                                                                            const LARGE_INTEGER* performanceCounter,
                                                                            XrTime* time) {
    if (!performanceCounter || !time) return XR_ERROR_VALIDATION_FAILURE;
    
    LARGE_INTEGER freq;
    QueryPerformanceFrequency(&freq);
    
    // Convert to nanoseconds
    *time = (performanceCounter->QuadPart * 1000000000) / freq.QuadPart;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrConvertTimeToWin32PerformanceCounterKHR_runtime(XrInstance instance,
                                                                             XrTime time,
                                                                             LARGE_INTEGER* performanceCounter) {
    if (!performanceCounter) return XR_ERROR_VALIDATION_FAILURE;
    
    LARGE_INTEGER freq;
    QueryPerformanceFrequency(&freq);
    
    // Convert from nanoseconds  
    performanceCounter->QuadPart = (time * freq.QuadPart) / 1000000000;
    return XR_SUCCESS;
}

// ----------------------------------------------

struct NameFn { const char* name; PFN_xrVoidFunction fn; };

static const NameFn kFnTable[] = {
    {"xrGetInstanceProcAddr", (PFN_xrVoidFunction)xrGetInstanceProcAddr_runtime},
    {"xrEnumerateApiLayerProperties", (PFN_xrVoidFunction)xrEnumerateApiLayerProperties_runtime},
    {"xrEnumerateInstanceExtensionProperties", (PFN_xrVoidFunction)xrEnumerateInstanceExtensionProperties_runtime},
    {"xrCreateInstance", (PFN_xrVoidFunction)xrCreateInstance_runtime},
    {"xrDestroyInstance", (PFN_xrVoidFunction)xrDestroyInstance_runtime},
    {"xrGetInstanceProperties", (PFN_xrVoidFunction)xrGetInstanceProperties_runtime},
    {"xrGetSystem", (PFN_xrVoidFunction)xrGetSystem_runtime},
    {"xrGetSystemProperties", (PFN_xrVoidFunction)xrGetSystemProperties_runtime},
    {"xrEnumerateViewConfigurations", (PFN_xrVoidFunction)xrEnumerateViewConfigurations_runtime},
    {"xrEnumerateViewConfigurationViews", (PFN_xrVoidFunction)xrEnumerateViewConfigurationViews_runtime},
    {"xrEnumerateEnvironmentBlendModes", (PFN_xrVoidFunction)xrEnumerateEnvironmentBlendModes_runtime},
    {"xrCreateSession", (PFN_xrVoidFunction)xrCreateSession_runtime},
    {"xrDestroySession", (PFN_xrVoidFunction)xrDestroySession_runtime},
    {"xrEnumerateSwapchainFormats", (PFN_xrVoidFunction)xrEnumerateSwapchainFormats_runtime},
    {"xrCreateSwapchain", (PFN_xrVoidFunction)xrCreateSwapchain_runtime},
    {"xrDestroySwapchain", (PFN_xrVoidFunction)xrDestroySwapchain_runtime},
    {"xrEnumerateSwapchainImages", (PFN_xrVoidFunction)xrEnumerateSwapchainImages_runtime},
    {"xrAcquireSwapchainImage", (PFN_xrVoidFunction)xrAcquireSwapchainImage_runtime},
    {"xrWaitSwapchainImage", (PFN_xrVoidFunction)xrWaitSwapchainImage_runtime},
    {"xrReleaseSwapchainImage", (PFN_xrVoidFunction)xrReleaseSwapchainImage_runtime},
    {"xrBeginSession", (PFN_xrVoidFunction)xrBeginSession_runtime},
    {"xrEndSession", (PFN_xrVoidFunction)xrEndSession_runtime},
    {"xrWaitFrame", (PFN_xrVoidFunction)xrWaitFrame_runtime},
    {"xrBeginFrame", (PFN_xrVoidFunction)xrBeginFrame_runtime},
    {"xrEndFrame", (PFN_xrVoidFunction)xrEndFrame_runtime},
    {"xrPollEvent", (PFN_xrVoidFunction)xrPollEvent_runtime},
    {"xrLocateViews", (PFN_xrVoidFunction)xrLocateViews_runtime},
    {"xrGetD3D11GraphicsRequirementsKHR", (PFN_xrVoidFunction)xrGetD3D11GraphicsRequirementsKHR_runtime},
    {"xrRequestExitSession", (PFN_xrVoidFunction)xrRequestExitSession_runtime},
    // Space functions
    {"xrCreateReferenceSpace", (PFN_xrVoidFunction)xrCreateReferenceSpace_runtime},
    {"xrDestroySpace", (PFN_xrVoidFunction)xrDestroySpace_runtime},
    {"xrLocateSpace", (PFN_xrVoidFunction)xrLocateSpace_runtime},
    {"xrEnumerateReferenceSpaces", (PFN_xrVoidFunction)xrEnumerateReferenceSpaces_runtime},
    {"xrCreateActionSpace", (PFN_xrVoidFunction)xrCreateActionSpace_runtime},
    // Action functions
    {"xrCreateActionSet", (PFN_xrVoidFunction)xrCreateActionSet_runtime},
    {"xrDestroyActionSet", (PFN_xrVoidFunction)xrDestroyActionSet_runtime},
    {"xrCreateAction", (PFN_xrVoidFunction)xrCreateAction_runtime},
    {"xrDestroyAction", (PFN_xrVoidFunction)xrDestroyAction_runtime},
    {"xrSuggestInteractionProfileBindings", (PFN_xrVoidFunction)xrSuggestInteractionProfileBindings_runtime},
    {"xrAttachSessionActionSets", (PFN_xrVoidFunction)xrAttachSessionActionSets_runtime},
    {"xrGetActionStateBoolean", (PFN_xrVoidFunction)xrGetActionStateBoolean_runtime},
    {"xrGetActionStateFloat", (PFN_xrVoidFunction)xrGetActionStateFloat_runtime},
    {"xrGetActionStatePose", (PFN_xrVoidFunction)xrGetActionStatePose_runtime},
    {"xrGetActionStateVector2f", (PFN_xrVoidFunction)xrGetActionStateVector2f_runtime},
    {"xrSyncActions", (PFN_xrVoidFunction)xrSyncActions_runtime},
    // Path functions
    {"xrStringToPath", (PFN_xrVoidFunction)xrStringToPath_runtime},
    {"xrPathToString", (PFN_xrVoidFunction)xrPathToString_runtime},
    // Interaction functions
    {"xrGetCurrentInteractionProfile", (PFN_xrVoidFunction)xrGetCurrentInteractionProfile_runtime},
    {"xrEnumerateBoundSourcesForAction", (PFN_xrVoidFunction)xrEnumerateBoundSourcesForAction_runtime},
    {"xrGetInputSourceLocalizedName", (PFN_xrVoidFunction)xrGetInputSourceLocalizedName_runtime},
    // Utility functions
    {"xrResultToString", (PFN_xrVoidFunction)xrResultToString_runtime},
    {"xrStructureTypeToString", (PFN_xrVoidFunction)xrStructureTypeToString_runtime},
    {"xrGetReferenceSpaceBoundsRect", (PFN_xrVoidFunction)xrGetReferenceSpaceBoundsRect_runtime},
    {"xrGetViewConfigurationProperties", (PFN_xrVoidFunction)xrGetViewConfigurationProperties_runtime},
    // Haptic functions
    {"xrApplyHapticFeedback", (PFN_xrVoidFunction)xrApplyHapticFeedback_runtime},
    {"xrStopHapticFeedback", (PFN_xrVoidFunction)xrStopHapticFeedback_runtime},
    // Time conversion functions
    {"xrConvertWin32PerformanceCounterToTimeKHR", (PFN_xrVoidFunction)xrConvertWin32PerformanceCounterToTimeKHR_runtime},
    {"xrConvertTimeToWin32PerformanceCounterKHR", (PFN_xrVoidFunction)xrConvertTimeToWin32PerformanceCounterKHR_runtime},
};

static XrResult XRAPI_PTR xrGetInstanceProcAddr_runtime(XrInstance instance, const char* name, PFN_xrVoidFunction* fn) {
    if (!name || !fn) {
        Logf("[SimXR] xrGetInstanceProcAddr: ERROR - name=%p, fn=%p", name, fn);
        return XR_ERROR_VALIDATION_FAILURE;
    }
    
    // Reduce logging verbosity for xrGetInstanceProcAddr
    static bool reduceLogging = false;
    static int callCount = 0;
    callCount++;
    
    for (auto& e : kFnTable) {
        if (strcmp(name, e.name) == 0) { 
            *fn = e.fn;
            if (callCount < 100 || strstr(name, "D3D11") || strstr(name, "Create") || strstr(name, "Destroy")) {
                Logf("[SimXR] xrGetInstanceProcAddr: %s -> FOUND", name);
            }
            return XR_SUCCESS; 
        }
    }
    
    if (callCount < 100 || strstr(name, "D3D11")) {
        Logf("[SimXR] xrGetInstanceProcAddr: %s -> NOT FOUND", name);
    }
    return XR_ERROR_FUNCTION_UNSUPPORTED;
}
