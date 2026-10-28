// OpenXR Simulator frame-time viewer: a separate process that reads a game's
// frame timeline out of shared memory, so watching never costs the game a
// thread, a lock or a swapchain - and the view survives the game hanging.
//
//   openxr_sim_frametime.exe [--pid <pid>] [capture.xrft [--baseline other.xrft]]
//
// With --pid it attaches to that process (waiting for it if needed) and, if a
// viewer for it is already open, brings that one forward instead. A .xrft path
// opens that capture; dropping one on the window does the same. The headless
// modes (headless.h) run without a window at all.
#include "headless.h"
#include "viewer_app.h"

#include "imgui.h"
#include "imgui_impl_dx11.h"
#include "imgui_impl_win32.h"
#include "implot.h"

#include <d3d11.h>
#include <dwmapi.h>
#include <shellapi.h>

#include <cstdlib>

extern IMGUI_IMPL_API LRESULT ImGui_ImplWin32_WndProcHandler(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam);

namespace {

constexpr wchar_t kWindowClass[] = L"OpenXRSimulatorFrameTiming";
constexpr wchar_t kPidProperty[] = L"OpenXRSim.Pid";

ID3D11Device* g_device = nullptr;
ID3D11DeviceContext* g_context = nullptr;
IDXGISwapChain* g_swapChain = nullptr;
ID3D11RenderTargetView* g_target = nullptr;
UINT g_resizeWidth = 0, g_resizeHeight = 0;
bool g_occluded = false;
WINDOWPLACEMENT g_placement{sizeof(WINDOWPLACEMENT)};  // captured while the window still exists
bool g_havePlacement = false;
std::wstring g_droppedCapture;  // a file dropped on the window, opened next frame

void CreateTarget() {
    ID3D11Texture2D* back = nullptr;
    g_swapChain->GetBuffer(0, IID_PPV_ARGS(&back));
    if (back) {
        g_device->CreateRenderTargetView(back, nullptr, &g_target);
        back->Release();
    }
}

void ReleaseTarget() {
    if (g_target) g_target->Release();
    g_target = nullptr;
}

bool CreateDevice(HWND hwnd) {
    DXGI_SWAP_CHAIN_DESC desc{};
    desc.BufferCount = 2;
    desc.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.Flags = DXGI_SWAP_CHAIN_FLAG_ALLOW_MODE_SWITCH;
    desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    desc.OutputWindow = hwnd;
    desc.SampleDesc.Count = 1;
    desc.Windowed = TRUE;
    desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_0};
    D3D_FEATURE_LEVEL level;
    HRESULT hr = D3D11CreateDeviceAndSwapChain(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, levels, 2,
                                               D3D11_SDK_VERSION, &desc, &g_swapChain, &g_device, &level, &g_context);
    if (hr == DXGI_ERROR_UNSUPPORTED) {
        hr = D3D11CreateDeviceAndSwapChain(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, levels, 2,
                                           D3D11_SDK_VERSION, &desc, &g_swapChain, &g_device, &level, &g_context);
    }
    if (FAILED(hr)) return false;
    CreateTarget();
    return true;
}

void ReleaseDevice() {
    ReleaseTarget();
    if (g_swapChain) g_swapChain->Release();
    if (g_context) g_context->Release();
    if (g_device) g_device->Release();
    g_swapChain = nullptr;
    g_context = nullptr;
    g_device = nullptr;
}

LRESULT WINAPI WndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    if (ImGui_ImplWin32_WndProcHandler(hwnd, msg, wParam, lParam)) return TRUE;
    switch (msg) {
        case WM_SIZE:
            if (wParam != SIZE_MINIMIZED) {
                g_resizeWidth = LOWORD(lParam);
                g_resizeHeight = HIWORD(lParam);
            }
            return 0;
        case WM_SYSCOMMAND:
            if ((wParam & 0xfff0) == SC_KEYMENU) return 0;  // no Alt menu
            break;
        case WM_DROPFILES: {
            const HDROP drop = reinterpret_cast<HDROP>(wParam);
            wchar_t path[MAX_PATH]{};
            if (DragQueryFileW(drop, 0, path, MAX_PATH)) g_droppedCapture = path;
            DragFinish(drop);
            return 0;
        }
        case WM_DESTROY:
            g_havePlacement = GetWindowPlacement(hwnd, &g_placement) != FALSE;
            RemovePropW(hwnd, kPidProperty);
            PostQuitMessage(0);
            return 0;
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}

struct CommandLine {
    DWORD pid = 0;
    std::wstring capture;   // a .xrft to open
    std::wstring baseline;  // a .xrft to compare it with
};

CommandLine ParseCommandLine(int count, wchar_t** args) {
    CommandLine line;
    for (int i = 1; i < count; ++i) {
        if (wcscmp(args[i], L"--pid") == 0 && i + 1 < count) {
            line.pid = DWORD(wcstoul(args[++i], nullptr, 10));
        } else if (wcscmp(args[i], L"--baseline") == 0 && i + 1 < count) {
            line.baseline = args[++i];
        } else if (args[i][0] != L'-') {
            line.capture = args[i];
        }
    }
    return line;
}

// A viewer already watching `pid` comes forward instead of opening a second.
bool ActivateExisting(DWORD pid) {
    struct Search { DWORD pid; HWND found; } search{pid, nullptr};
    EnumWindows([](HWND hwnd, LPARAM param) -> BOOL {
        auto* s = reinterpret_cast<Search*>(param);
        wchar_t name[64];
        if (GetClassNameW(hwnd, name, 64) && wcscmp(name, kWindowClass) == 0 &&
            DWORD(uintptr_t(GetPropW(hwnd, kPidProperty))) == s->pid) {
            s->found = hwnd;
            return FALSE;
        }
        return TRUE;
    }, reinterpret_cast<LPARAM>(&search));
    if (!search.found) return false;
    if (IsIconic(search.found)) ShowWindow(search.found, SW_RESTORE);
    SetForegroundWindow(search.found);
    return true;
}

} // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, LPWSTR, int) {
    // Watching must not compete with the game being watched.
    SetPriorityClass(GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS);
    int argc = 0;
    wchar_t** argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    int exitCode = 0;
    if (argv && viewer::RunHeadless(argc, argv, exitCode)) {
        LocalFree(argv);
        return exitCode;
    }
    const CommandLine commandLine = argv ? ParseCommandLine(argc, argv) : CommandLine{};
    LocalFree(argv);
    const DWORD pid = commandLine.pid;
    if (pid && commandLine.capture.empty() && ActivateExisting(pid)) return 0;

    ImGui_ImplWin32_EnableDpiAwareness();
    const float scale = ImGui_ImplWin32_GetDpiScaleForMonitor(MonitorFromPoint(POINT{0, 0}, MONITOR_DEFAULTTOPRIMARY));
    viewer::Settings settings = viewer::LoadSettings();

    WNDCLASSEXW wc{sizeof(wc)};
    wc.style = CS_CLASSDC;
    wc.lpfnWndProc = WndProc;
    wc.hInstance = instance;
    wc.hIcon = LoadIconW(nullptr, IDI_APPLICATION);
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.lpszClassName = kWindowClass;
    RegisterClassExW(&wc);
    int x = CW_USEDEFAULT, y = CW_USEDEFAULT;
    int w = int(1600 * scale), h = int(1000 * scale);
    if (settings.windowW > 200 && settings.windowH > 200) {
        x = settings.windowX;
        y = settings.windowY;
        w = settings.windowW;
        h = settings.windowH;
    }
    HWND hwnd = CreateWindowExW(0, kWindowClass, L"Frame Timing - OpenXR Simulator", WS_OVERLAPPEDWINDOW,
                                x, y, w, h, nullptr, nullptr, instance, nullptr);
    if (!CreateDevice(hwnd)) {
        ReleaseDevice();
        MessageBoxW(nullptr, L"Could not create a Direct3D 11 device.", L"Frame Timing", MB_ICONERROR);
        return 1;
    }
    DragAcceptFiles(hwnd, TRUE);
    ShowWindow(hwnd, settings.maximized ? SW_SHOWMAXIMIZED : SW_SHOWDEFAULT);
    UpdateWindow(hwnd);

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImPlot::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.IniFilename = nullptr;  // one fixed layout; the viewer keeps its own settings
    viewer::ui::LoadFonts();
    ImGui_ImplWin32_Init(hwnd);
    ImGui_ImplDX11_Init(g_device, g_context);

    {
        // Applies the theme (and with it the dark or light title bar) before the first frame.
        viewer::App app(pid, hwnd, scale, settings);
        if (!commandLine.capture.empty()) app.OpenCapture(commandLine.capture);
        if (!commandLine.baseline.empty()) app.CompareWith(commandLine.baseline);
        bool done = false;
        while (!done) {
            MSG msg;
            while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
                TranslateMessage(&msg);
                DispatchMessageW(&msg);
                if (msg.message == WM_QUIT) done = true;
            }
            if (done) break;
            app.Poll();
            if (!g_droppedCapture.empty()) {
                app.OpenCapture(g_droppedCapture);
                g_droppedCapture.clear();
            }

            if ((g_occluded && g_swapChain->Present(0, DXGI_PRESENT_TEST) == DXGI_STATUS_OCCLUDED) || IsIconic(hwnd)) {
                Sleep(50);
                continue;
            }
            g_occluded = false;
            if (g_resizeWidth && g_resizeHeight) {
                ReleaseTarget();
                g_swapChain->ResizeBuffers(0, g_resizeWidth, g_resizeHeight, DXGI_FORMAT_UNKNOWN, 0);
                g_resizeWidth = g_resizeHeight = 0;
                CreateTarget();
            }

            app.BeginFrame();
            ImGui_ImplDX11_NewFrame();
            ImGui_ImplWin32_NewFrame();
            ImGui::NewFrame();
            app.Draw();
            ImGui::Render();
            const ImVec4 page = ImGui::ColorConvertU32ToFloat4(app.Theme().page);
            const float clear[4] = {page.x, page.y, page.z, 1.0f};
            g_context->OMSetRenderTargets(1, &g_target, nullptr);
            g_context->ClearRenderTargetView(g_target, clear);
            ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());
            g_occluded = g_swapChain->Present(1, 0) == DXGI_STATUS_OCCLUDED;
            // About 30 Hz while the game has focus: enough to watch, little to share the GPU with.
            if (GetForegroundWindow() != hwnd) Sleep(16);
        }

        if (g_havePlacement) {
            viewer::Settings& s = app.UserSettings();
            const RECT& r = g_placement.rcNormalPosition;
            s.windowX = r.left;
            s.windowY = r.top;
            s.windowW = r.right - r.left;
            s.windowH = r.bottom - r.top;
            s.maximized = g_placement.showCmd == SW_SHOWMAXIMIZED;
        }
    }

    ImGui_ImplDX11_Shutdown();
    ImGui_ImplWin32_Shutdown();
    ImPlot::DestroyContext();
    ImGui::DestroyContext();
    ReleaseDevice();
    DestroyWindow(hwnd);
    UnregisterClassW(kWindowClass, instance);
    return 0;
}
