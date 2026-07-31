// UI Enhancements for OpenXR Simulator
// Provides dark theme, menu system, and view controls
#pragma once

#include <windows.h>
#include <dwmapi.h>
#include <string>
#include <functional>
#include <algorithm>

#pragma comment(lib, "dwmapi.lib")
#pragma comment(lib, "uxtheme.lib")

namespace ui {

// Menu command IDs
enum MenuCommand {
    // View Menu
    ID_VIEW_BOTH_EYES = 1001,
    ID_VIEW_LEFT_EYE = 1002,
    ID_VIEW_RIGHT_EYE = 1003,

    // Zoom levels
    ID_ZOOM_25 = 1101,
    ID_ZOOM_50 = 1102,
    ID_ZOOM_75 = 1103,
    ID_ZOOM_100 = 1104,
    ID_ZOOM_FIT = 1105,
    ID_ZOOM_IN = 1106,
    ID_ZOOM_OUT = 1107,

    // FOV options
    ID_FOV_70 = 1201,
    ID_FOV_90 = 1202,
    ID_FOV_110 = 1203,
    ID_FOV_SYMMETRIC = 1204,
    ID_FOV_ASYMMETRIC = 1205,
    ID_PROFILE_GENERIC = 1210,
    ID_PROFILE_QUEST2 = 1211,
    ID_PROFILE_QUEST3 = 1212,
    ID_PROFILE_INDEX = 1213,
    ID_IPD_0 = 1220,
    ID_IPD_58 = 1221,
    ID_IPD_64 = 1222,
    ID_IPD_70 = 1223,
    ID_IPD_80 = 1224,
    ID_IPD_DECREASE = 1225,
    ID_IPD_INCREASE = 1226,

    // Render options
    ID_VIEW_FULL_RENDER = 1250,

    // Display options
    ID_DISPLAY_SIDE_BY_SIDE = 1301,
    ID_DISPLAY_OVER_UNDER = 1302,
    ID_DISPLAY_ANAGLYPH = 1303,

    // Tools
    ID_TOOLS_SCREENSHOT = 1401,
    ID_TOOLS_RESET_VIEW = 1402,
    ID_TOOLS_TOGGLE_STATS = 1403,

    // Help
    ID_HELP_CONTROLS = 1501,
    ID_HELP_ABOUT = 1502
};

// View mode enum
enum class ViewMode {
    BothEyes,
    LeftEyeOnly,
    RightEyeOnly
};

// Display layout enum
enum class DisplayLayout {
    SideBySide,
    OverUnder,
    Anaglyph
};

// Headset profile enum
enum class HeadsetProfile {
    GenericSymmetric,
    Quest2,
    Quest3,
    ValveIndex
};

// UI State
struct UIState {
    ViewMode viewMode = ViewMode::BothEyes;
    DisplayLayout displayLayout = DisplayLayout::SideBySide;
    HeadsetProfile headsetProfile = HeadsetProfile::Quest3;
    bool showStats = false;
    float zoomLevel = 1.0f;  // 0.25 = 25%, 0.5 = 50%, 1.0 = 100%, etc.
    bool fitToWindow = false; // If true, auto-fit zoom to window size
    int windowWidth = 1280;
    int windowHeight = 720;

    // FOV settings
    int fovDegrees = 90;     // FOV in degrees for generic symmetric mode
    bool useAsymmetricFov = true;
    float ipdMeters = 0.064f;

    // Render options
    bool showFullRender = false;  // If true, show full swapchain instead of imageRect crop
};

inline UIState g_uiState;

// Native per-eye panel resolution for the active profile. Two jobs: it is what
// xrEnumerateViewConfigurationViews recommends, and it is the shape the preview maps
// each eye onto. The second matters more. These headsets have frusta that are taller
// than they are wide, so an app that renders one into a 16:9 buffer has non-square
// pixels -- correct, and what a real compositor un-squeezes when it maps the buffer
// onto the panel. Blitting that buffer 1:1 instead is what looks stretched.
inline void GetHeadsetPanelResolution(uint32_t& width, uint32_t& height) {
    switch (g_uiState.headsetProfile) {
        case HeadsetProfile::Quest2:     width = 1832; height = 1920; break;
        case HeadsetProfile::ValveIndex: width = 1440; height = 1600; break;
        case HeadsetProfile::Quest3:     width = 2064; height = 2208; break;
        case HeadsetProfile::GenericSymmetric:
        default:                         width = 1440; height = 1440; break;  // square FOV, square panel
    }
}

inline int GetIpdMillimeters() {
    return (int)(g_uiState.ipdMeters * 1000.0f + 0.5f);
}

inline void SetIpdMillimeters(int ipdMm) {
    ipdMm = (std::max)(0, (std::min)(200, ipdMm));
    g_uiState.ipdMeters = (float)ipdMm * 0.001f;
}

inline void AdjustIpdMillimeters(int deltaMm) {
    SetIpdMillimeters(GetIpdMillimeters() + deltaMm);
}

inline void SetHeadsetProfile(HeadsetProfile profile) {
    g_uiState.headsetProfile = profile;

    switch (profile) {
        case HeadsetProfile::GenericSymmetric:
            g_uiState.useAsymmetricFov = false;
            SetIpdMillimeters(64);
            break;
        case HeadsetProfile::Quest2:
            g_uiState.useAsymmetricFov = true;
            SetIpdMillimeters(64);
            break;
        case HeadsetProfile::Quest3:
            g_uiState.useAsymmetricFov = true;
            SetIpdMillimeters(64);
            break;
        case HeadsetProfile::ValveIndex:
            g_uiState.useAsymmetricFov = true;
            SetIpdMillimeters(63);
            break;
    }
}

inline void SetSymmetricViews() {
    g_uiState.headsetProfile = HeadsetProfile::GenericSymmetric;
    g_uiState.useAsymmetricFov = false;
}

inline void SetAsymmetricViews() {
    if (g_uiState.headsetProfile == HeadsetProfile::GenericSymmetric) {
        g_uiState.headsetProfile = HeadsetProfile::Quest3;
    }
    g_uiState.useAsymmetricFov = true;
}

inline const wchar_t* GetHeadsetProfileShortName() {
    switch (g_uiState.headsetProfile) {
        case HeadsetProfile::GenericSymmetric: return L"Generic";
        case HeadsetProfile::Quest2: return L"Quest 2";
        case HeadsetProfile::Quest3: return L"Quest 3";
        case HeadsetProfile::ValveIndex: return L"Index";
    }
    return L"Generic";
}

inline bool IsFovSettingsCommand(int cmd) {
    return cmd == ID_FOV_70 || cmd == ID_FOV_90 || cmd == ID_FOV_110 ||
           cmd == ID_FOV_SYMMETRIC || cmd == ID_FOV_ASYMMETRIC ||
           cmd == ID_PROFILE_GENERIC || cmd == ID_PROFILE_QUEST2 ||
           cmd == ID_PROFILE_QUEST3 || cmd == ID_PROFILE_INDEX;
}

inline bool IsIpdSettingsCommand(int cmd) {
    return cmd == ID_IPD_0 || cmd == ID_IPD_58 || cmd == ID_IPD_64 ||
           cmd == ID_IPD_70 || cmd == ID_IPD_80 ||
           cmd == ID_IPD_DECREASE || cmd == ID_IPD_INCREASE;
}

// Dark mode colors
namespace Colors {
    const COLORREF Background = RGB(30, 30, 30);
    const COLORREF Surface = RGB(45, 45, 45);
    const COLORREF Primary = RGB(100, 149, 237);  // Cornflower blue
    const COLORREF Text = RGB(230, 230, 230);
    const COLORREF TextSecondary = RGB(160, 160, 160);
    const COLORREF Border = RGB(60, 60, 60);
    const COLORREF Accent = RGB(0, 150, 136);  // Teal
}

// Enable dark title bar (Windows 10 1809+)
inline void EnableDarkTitleBar(HWND hwnd) {
    BOOL darkMode = TRUE;
    DwmSetWindowAttribute(hwnd, 20, &darkMode, sizeof(darkMode));
    DwmSetWindowAttribute(hwnd, 19, &darkMode, sizeof(darkMode));
}

// Set window border color
inline void SetWindowBorderColor(HWND hwnd, COLORREF color) {
    DwmSetWindowAttribute(hwnd, 34, &color, sizeof(color));
}

// Set caption/title bar color
inline void SetCaptionColor(HWND hwnd, COLORREF color) {
    DwmSetWindowAttribute(hwnd, 35, &color, sizeof(color));
}

// Create the application menu
inline HMENU CreateAppMenu() {
    HMENU menuBar = CreateMenu();

    // View Menu
    HMENU viewMenu = CreatePopupMenu();
    AppendMenuW(viewMenu, MF_STRING, ID_VIEW_BOTH_EYES, L"&Both Eyes\tB");
    AppendMenuW(viewMenu, MF_STRING, ID_VIEW_LEFT_EYE, L"&Left Eye Only\tL");
    AppendMenuW(viewMenu, MF_STRING, ID_VIEW_RIGHT_EYE, L"&Right Eye Only\tR");
    AppendMenuW(viewMenu, MF_SEPARATOR, 0, nullptr);

    // Display layout submenu
    HMENU layoutMenu = CreatePopupMenu();
    AppendMenuW(layoutMenu, MF_STRING, ID_DISPLAY_SIDE_BY_SIDE, L"&Side by Side");
    AppendMenuW(layoutMenu, MF_STRING, ID_DISPLAY_OVER_UNDER, L"&Over/Under");
    AppendMenuW(layoutMenu, MF_STRING, ID_DISPLAY_ANAGLYPH, L"&Anaglyph 3D");
    AppendMenuW(viewMenu, MF_POPUP, (UINT_PTR)layoutMenu, L"Display &Layout");

    AppendMenuW(menuBar, MF_POPUP, (UINT_PTR)viewMenu, L"&View");

    // Zoom Menu
    HMENU zoomMenu = CreatePopupMenu();
    AppendMenuW(zoomMenu, MF_STRING, ID_ZOOM_FIT, L"&Fit to Window\tF");
    AppendMenuW(zoomMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(zoomMenu, MF_STRING, ID_ZOOM_25, L"25%\t1");
    AppendMenuW(zoomMenu, MF_STRING, ID_ZOOM_50, L"50%\t2");
    AppendMenuW(zoomMenu, MF_STRING, ID_ZOOM_75, L"75%\t3");
    AppendMenuW(zoomMenu, MF_STRING, ID_ZOOM_100, L"100%\t4");
    AppendMenuW(zoomMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(zoomMenu, MF_STRING, ID_ZOOM_IN, L"Zoom &In\t+");
    AppendMenuW(zoomMenu, MF_STRING, ID_ZOOM_OUT, L"Zoom &Out\t-");

    AppendMenuW(menuBar, MF_POPUP, (UINT_PTR)zoomMenu, L"&Zoom");

    // FOV Menu
    HMENU fovMenu = CreatePopupMenu();
    AppendMenuW(fovMenu, MF_STRING, ID_FOV_SYMMETRIC, L"&Symmetric Views\t8");
    AppendMenuW(fovMenu, MF_STRING, ID_FOV_ASYMMETRIC, L"&Asymmetric Views\t9");
    AppendMenuW(fovMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(fovMenu, MF_STRING, ID_FOV_70, L"70\x00B0 (Narrow)\t5");
    AppendMenuW(fovMenu, MF_STRING, ID_FOV_90, L"90\x00B0 (Normal)\t6");
    AppendMenuW(fovMenu, MF_STRING, ID_FOV_110, L"110\x00B0 (Wide)\t7");
    AppendMenuW(fovMenu, MF_SEPARATOR, 0, nullptr);

    HMENU profileMenu = CreatePopupMenu();
    AppendMenuW(profileMenu, MF_STRING, ID_PROFILE_GENERIC, L"&Generic Symmetric");
    AppendMenuW(profileMenu, MF_STRING, ID_PROFILE_QUEST2, L"Quest &2");
    AppendMenuW(profileMenu, MF_STRING, ID_PROFILE_QUEST3, L"Quest &3");
    AppendMenuW(profileMenu, MF_STRING, ID_PROFILE_INDEX, L"Valve &Index");
    AppendMenuW(fovMenu, MF_POPUP, (UINT_PTR)profileMenu, L"Headset &Profile");

    HMENU ipdMenu = CreatePopupMenu();
    AppendMenuW(ipdMenu, MF_STRING, ID_IPD_DECREASE, L"Decrease IPD\t[");
    AppendMenuW(ipdMenu, MF_STRING, ID_IPD_INCREASE, L"Increase IPD\t]");
    AppendMenuW(ipdMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(ipdMenu, MF_STRING, ID_IPD_0, L"0 mm (No Stereo)");
    AppendMenuW(ipdMenu, MF_STRING, ID_IPD_58, L"58 mm");
    AppendMenuW(ipdMenu, MF_STRING, ID_IPD_64, L"64 mm");
    AppendMenuW(ipdMenu, MF_STRING, ID_IPD_70, L"70 mm");
    AppendMenuW(ipdMenu, MF_STRING, ID_IPD_80, L"80 mm");
    AppendMenuW(fovMenu, MF_POPUP, (UINT_PTR)ipdMenu, L"&IPD");
    AppendMenuW(fovMenu, MF_SEPARATOR, 0, nullptr);

    AppendMenuW(fovMenu, MF_STRING, ID_VIEW_FULL_RENDER, L"Show &Full Render\tG");
    AppendMenuW(menuBar, MF_POPUP, (UINT_PTR)fovMenu, L"F&OV");

    // Tools Menu
    HMENU toolsMenu = CreatePopupMenu();
    AppendMenuW(toolsMenu, MF_STRING, ID_TOOLS_SCREENSHOT, L"Take &Screenshot\tF12");
    AppendMenuW(toolsMenu, MF_STRING, ID_TOOLS_RESET_VIEW, L"&Reset View\tHome");
    AppendMenuW(toolsMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(toolsMenu, MF_STRING, ID_TOOLS_TOGGLE_STATS, L"Show &Statistics\tF3");
    AppendMenuW(menuBar, MF_POPUP, (UINT_PTR)toolsMenu, L"&Tools");

    // Help Menu
    HMENU helpMenu = CreatePopupMenu();
    AppendMenuW(helpMenu, MF_STRING, ID_HELP_CONTROLS, L"&Controls...\tF1");
    AppendMenuW(helpMenu, MF_STRING, ID_HELP_ABOUT, L"&About OpenXR Simulator");
    AppendMenuW(menuBar, MF_POPUP, (UINT_PTR)helpMenu, L"&Help");

    return menuBar;
}

// Update menu check marks based on current state
inline void UpdateMenuState(HMENU menu) {
    // View mode checks
    CheckMenuItem(menu, ID_VIEW_BOTH_EYES,
        g_uiState.viewMode == ViewMode::BothEyes ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_VIEW_LEFT_EYE,
        g_uiState.viewMode == ViewMode::LeftEyeOnly ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_VIEW_RIGHT_EYE,
        g_uiState.viewMode == ViewMode::RightEyeOnly ? MF_CHECKED : MF_UNCHECKED);

    // Layout checks
    CheckMenuItem(menu, ID_DISPLAY_SIDE_BY_SIDE,
        g_uiState.displayLayout == DisplayLayout::SideBySide ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_DISPLAY_OVER_UNDER,
        g_uiState.displayLayout == DisplayLayout::OverUnder ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_DISPLAY_ANAGLYPH,
        g_uiState.displayLayout == DisplayLayout::Anaglyph ? MF_CHECKED : MF_UNCHECKED);

    // Zoom checks
    CheckMenuItem(menu, ID_ZOOM_FIT, g_uiState.fitToWindow ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_ZOOM_25, (!g_uiState.fitToWindow && g_uiState.zoomLevel == 0.25f) ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_ZOOM_50, (!g_uiState.fitToWindow && g_uiState.zoomLevel == 0.50f) ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_ZOOM_75, (!g_uiState.fitToWindow && g_uiState.zoomLevel == 0.75f) ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_ZOOM_100, (!g_uiState.fitToWindow && g_uiState.zoomLevel == 1.0f) ? MF_CHECKED : MF_UNCHECKED);

    // Stats toggle
    CheckMenuItem(menu, ID_TOOLS_TOGGLE_STATS,
        g_uiState.showStats ? MF_CHECKED : MF_UNCHECKED);

    // FOV checks
    CheckMenuItem(menu, ID_FOV_SYMMETRIC, !g_uiState.useAsymmetricFov ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_FOV_ASYMMETRIC, g_uiState.useAsymmetricFov ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_FOV_70, (!g_uiState.useAsymmetricFov && g_uiState.fovDegrees == 70) ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_FOV_90, (!g_uiState.useAsymmetricFov && g_uiState.fovDegrees == 90) ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_FOV_110, (!g_uiState.useAsymmetricFov && g_uiState.fovDegrees == 110) ? MF_CHECKED : MF_UNCHECKED);

    // Headset profile checks
    CheckMenuItem(menu, ID_PROFILE_GENERIC,
        g_uiState.headsetProfile == HeadsetProfile::GenericSymmetric ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_PROFILE_QUEST2,
        g_uiState.headsetProfile == HeadsetProfile::Quest2 ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_PROFILE_QUEST3,
        g_uiState.headsetProfile == HeadsetProfile::Quest3 ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_PROFILE_INDEX,
        g_uiState.headsetProfile == HeadsetProfile::ValveIndex ? MF_CHECKED : MF_UNCHECKED);

    // IPD checks
    int ipdMm = GetIpdMillimeters();
    CheckMenuItem(menu, ID_IPD_0, ipdMm == 0 ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_IPD_58, ipdMm == 58 ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_IPD_64, ipdMm == 64 ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_IPD_70, ipdMm == 70 ? MF_CHECKED : MF_UNCHECKED);
    CheckMenuItem(menu, ID_IPD_80, ipdMm == 80 ? MF_CHECKED : MF_UNCHECKED);

    // Full render toggle
    CheckMenuItem(menu, ID_VIEW_FULL_RENDER,
        g_uiState.showFullRender ? MF_CHECKED : MF_UNCHECKED);
}

// Show controls help dialog
inline void ShowControlsDialog(HWND parent) {
    const wchar_t* helpText =
        L"OpenXR Simulator Controls\n"
        L"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        L"Mouse Look:\n"
        L"  Click and drag to look around\n"
        L"  ESC to release mouse capture\n\n"
        L"Movement (WASD):\n"
        L"  W/S - Forward/Backward\n"
        L"  A/D - Strafe Left/Right\n"
        L"  Q/E - Up/Down\n\n"
        L"View Controls:\n"
        L"  B - Both eyes\n"
        L"  L - Left eye only\n"
        L"  R - Right eye only\n\n"
        L"Zoom:\n"
        L"  F - Fit to window\n"
        L"  1-4 - Zoom presets (25%-100%)\n"
        L"  +/- - Zoom in/out\n"
        L"  Mouse wheel - Zoom\n\n"
        L"FOV:\n"
        L"  5 - 70\x00B0 (Narrow)\n"
        L"  6 - 90\x00B0 (Normal)\n"
        L"  7 - 110\x00B0 (Wide)\n"
        L"  8 - Symmetric views\n"
        L"  9 - Asymmetric views\n"
        L"  [ / ] - Decrease/Increase IPD\n"
        L"  Headset Profile menu - Asymmetric FOV presets\n"
        L"  IPD menu - Eye separation presets\n"
        L"  G - Toggle full render\n\n"
        L"Other:\n"
        L"  F12 - Screenshot\n"
        L"  F3 - Toggle stats\n"
        L"  Home - Reset view";

    MessageBoxW(parent, helpText, L"Controls", MB_OK | MB_ICONINFORMATION);
}

// Show about dialog
inline void ShowAboutDialog(HWND parent) {
    const wchar_t* aboutText =
        L"OpenXR Simulator\n"
        L"Version 1.0\n\n"
        L"A desktop-based OpenXR runtime for testing\n"
        L"and development without VR hardware.\n\n"
        L"Features:\n"
        L"  D3D11 and D3D12 support\n"
        L"  Mouse + WASD controls\n"
        L"  Stereo preview with zoom\n"
        L"  MCP integration for diagnostics";

    MessageBoxW(parent, aboutText, L"About OpenXR Simulator", MB_OK | MB_ICONINFORMATION);
}

// Calculate the preview window size based on source size and zoom
// NOTE: srcWidth and srcHeight are the dimensions of a SINGLE EYE swapchain
inline void CalculateWindowSize(int srcWidth, int srcHeight, int& outWidth, int& outHeight) {
    float zoom = g_uiState.fitToWindow ? 0.5f : g_uiState.zoomLevel;

    switch (g_uiState.viewMode) {
        case ViewMode::BothEyes:
            if (g_uiState.displayLayout == DisplayLayout::SideBySide) {
                // Two eyes side by side: double the width
                outWidth = (int)(srcWidth * 2 * zoom);
                outHeight = (int)(srcHeight * zoom);
            } else if (g_uiState.displayLayout == DisplayLayout::OverUnder) {
                // Two eyes stacked: double the height
                outWidth = (int)(srcWidth * zoom);
                outHeight = (int)(srcHeight * 2 * zoom);
            } else { // Anaglyph - both eyes overlap in same frame
                outWidth = (int)(srcWidth * zoom);
                outHeight = (int)(srcHeight * zoom);
            }
            break;
        case ViewMode::LeftEyeOnly:
        case ViewMode::RightEyeOnly:
            // Single eye: just use the single eye dimensions
            outWidth = (int)(srcWidth * zoom);
            outHeight = (int)(srcHeight * zoom);
            break;
    }

    // Ensure minimum size
    outWidth = (std::max)(outWidth, 320);
    outHeight = (std::max)(outHeight, 240);
}

// Adjust zoom level
inline void AdjustZoom(float delta) {
    g_uiState.fitToWindow = false;
    g_uiState.zoomLevel = (std::max)(0.1f, (std::min)(2.0f, g_uiState.zoomLevel + delta));
}

// Handle menu commands - returns true if handled
inline bool HandleMenuCommand(HWND hwnd, WPARAM wParam,
    std::function<void()> resizeCallback = nullptr,
    std::function<void()> screenshotCallback = nullptr,
    std::function<void()> resetViewCallback = nullptr,
    std::function<void(int)> settingsChangedCallback = nullptr) {

    int cmd = LOWORD(wParam);
    bool needsResize = false;
    bool settingsChanged = false;

    switch (cmd) {
        // View modes
        case ID_VIEW_BOTH_EYES:
            g_uiState.viewMode = ViewMode::BothEyes;
            needsResize = true;
            break;

        case ID_VIEW_LEFT_EYE:
            g_uiState.viewMode = ViewMode::LeftEyeOnly;
            needsResize = true;
            break;

        case ID_VIEW_RIGHT_EYE:
            g_uiState.viewMode = ViewMode::RightEyeOnly;
            needsResize = true;
            break;

        // Display layouts
        case ID_DISPLAY_SIDE_BY_SIDE:
            g_uiState.displayLayout = DisplayLayout::SideBySide;
            needsResize = true;
            break;

        case ID_DISPLAY_OVER_UNDER:
            g_uiState.displayLayout = DisplayLayout::OverUnder;
            needsResize = true;
            break;

        case ID_DISPLAY_ANAGLYPH:
            g_uiState.displayLayout = DisplayLayout::Anaglyph;
            needsResize = true;
            break;

        // Zoom presets
        case ID_ZOOM_FIT:
            g_uiState.fitToWindow = true;
            needsResize = true;
            break;

        case ID_ZOOM_25:
            g_uiState.fitToWindow = false;
            g_uiState.zoomLevel = 0.25f;
            needsResize = true;
            break;

        case ID_ZOOM_50:
            g_uiState.fitToWindow = false;
            g_uiState.zoomLevel = 0.50f;
            needsResize = true;
            break;

        case ID_ZOOM_75:
            g_uiState.fitToWindow = false;
            g_uiState.zoomLevel = 0.75f;
            needsResize = true;
            break;

        case ID_ZOOM_100:
            g_uiState.fitToWindow = false;
            g_uiState.zoomLevel = 1.0f;
            needsResize = true;
            break;

        case ID_ZOOM_IN:
            AdjustZoom(0.1f);
            needsResize = true;
            break;

        case ID_ZOOM_OUT:
            AdjustZoom(-0.1f);
            needsResize = true;
            break;

        // FOV options
        case ID_FOV_70:
            g_uiState.headsetProfile = HeadsetProfile::GenericSymmetric;
            g_uiState.useAsymmetricFov = false;
            g_uiState.fovDegrees = 70;
            settingsChanged = true;
            break;

        case ID_FOV_90:
            g_uiState.headsetProfile = HeadsetProfile::GenericSymmetric;
            g_uiState.useAsymmetricFov = false;
            g_uiState.fovDegrees = 90;
            settingsChanged = true;
            break;

        case ID_FOV_110:
            g_uiState.headsetProfile = HeadsetProfile::GenericSymmetric;
            g_uiState.useAsymmetricFov = false;
            g_uiState.fovDegrees = 110;
            settingsChanged = true;
            break;

        case ID_FOV_SYMMETRIC:
            SetSymmetricViews();
            settingsChanged = true;
            break;

        case ID_FOV_ASYMMETRIC:
            SetAsymmetricViews();
            settingsChanged = true;
            break;

        case ID_PROFILE_GENERIC:
            SetHeadsetProfile(HeadsetProfile::GenericSymmetric);
            settingsChanged = true;
            break;

        case ID_PROFILE_QUEST2:
            SetHeadsetProfile(HeadsetProfile::Quest2);
            settingsChanged = true;
            break;

        case ID_PROFILE_QUEST3:
            SetHeadsetProfile(HeadsetProfile::Quest3);
            settingsChanged = true;
            break;

        case ID_PROFILE_INDEX:
            SetHeadsetProfile(HeadsetProfile::ValveIndex);
            settingsChanged = true;
            break;

        case ID_IPD_0:
            SetIpdMillimeters(0);
            settingsChanged = true;
            break;

        case ID_IPD_58:
            SetIpdMillimeters(58);
            settingsChanged = true;
            break;

        case ID_IPD_64:
            SetIpdMillimeters(64);
            settingsChanged = true;
            break;

        case ID_IPD_70:
            SetIpdMillimeters(70);
            settingsChanged = true;
            break;

        case ID_IPD_80:
            SetIpdMillimeters(80);
            settingsChanged = true;
            break;

        case ID_IPD_DECREASE:
            AdjustIpdMillimeters(-1);
            settingsChanged = true;
            break;

        case ID_IPD_INCREASE:
            AdjustIpdMillimeters(1);
            settingsChanged = true;
            break;

        // Render options
        case ID_VIEW_FULL_RENDER:
            g_uiState.showFullRender = !g_uiState.showFullRender;
            needsResize = true;
            break;

        // Tools
        case ID_TOOLS_SCREENSHOT:
            if (screenshotCallback) screenshotCallback();
            return true;

        case ID_TOOLS_RESET_VIEW:
            if (resetViewCallback) resetViewCallback();
            return true;

        case ID_TOOLS_TOGGLE_STATS: {
            g_uiState.showStats = !g_uiState.showStats;
            // Refresh the menu checkmark and force an immediate title-bar
            // update so the user sees the stats appear/disappear right away.
            HMENU menuT = GetMenu(hwnd);
            if (menuT) UpdateMenuState(menuT);
            return true;
        }

        // Help
        case ID_HELP_CONTROLS:
            ShowControlsDialog(hwnd);
            return true;

        case ID_HELP_ABOUT:
            ShowAboutDialog(hwnd);
            return true;

        default:
            return false;
    }

    if (needsResize && resizeCallback) {
        resizeCallback();
    }

    if (settingsChanged && settingsChangedCallback) {
        settingsChangedCallback(cmd);
    }

    // Update menu checkmarks
    HMENU menu = GetMenu(hwnd);
    if (menu) UpdateMenuState(menu);

    return true;
}

// Handle keyboard shortcuts - returns true if handled
inline bool HandleKeyboardShortcut(HWND hwnd, WPARAM vk,
    std::function<void()> resizeCallback = nullptr,
    std::function<void()> screenshotCallback = nullptr,
    std::function<void()> resetViewCallback = nullptr,
    std::function<void(int)> settingsChangedCallback = nullptr) {

    switch (vk) {
        case 'B':
            return HandleMenuCommand(hwnd, ID_VIEW_BOTH_EYES, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case 'L':
            return HandleMenuCommand(hwnd, ID_VIEW_LEFT_EYE, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case 'R':
            return HandleMenuCommand(hwnd, ID_VIEW_RIGHT_EYE, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case 'F':
            return HandleMenuCommand(hwnd, ID_ZOOM_FIT, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case '1':
            return HandleMenuCommand(hwnd, ID_ZOOM_25, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case '2':
            return HandleMenuCommand(hwnd, ID_ZOOM_50, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case '3':
            return HandleMenuCommand(hwnd, ID_ZOOM_75, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case '4':
            return HandleMenuCommand(hwnd, ID_ZOOM_100, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case '5':
            return HandleMenuCommand(hwnd, ID_FOV_70, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case '6':
            return HandleMenuCommand(hwnd, ID_FOV_90, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case '7':
            return HandleMenuCommand(hwnd, ID_FOV_110, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case '8':
            return HandleMenuCommand(hwnd, ID_FOV_SYMMETRIC, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case '9':
            return HandleMenuCommand(hwnd, ID_FOV_ASYMMETRIC, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case 'G':
            return HandleMenuCommand(hwnd, ID_VIEW_FULL_RENDER, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case VK_OEM_PLUS:
        case VK_ADD:
            return HandleMenuCommand(hwnd, ID_ZOOM_IN, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case VK_OEM_MINUS:
        case VK_SUBTRACT:
            return HandleMenuCommand(hwnd, ID_ZOOM_OUT, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case VK_OEM_4:
            return HandleMenuCommand(hwnd, ID_IPD_DECREASE, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case VK_OEM_6:
            return HandleMenuCommand(hwnd, ID_IPD_INCREASE, resizeCallback, screenshotCallback, resetViewCallback, settingsChangedCallback);
        case VK_F1:
            ShowControlsDialog(hwnd);
            return true;
        case VK_F3:
            g_uiState.showStats = !g_uiState.showStats;
            return true;
        case VK_F12:
            if (screenshotCallback) screenshotCallback();
            return true;
        case VK_HOME:
            if (resetViewCallback) resetViewCallback();
            return true;
    }
    return false;
}

// Handle mouse wheel for zoom
inline bool HandleMouseWheel(HWND hwnd, short delta,
    std::function<void()> resizeCallback = nullptr) {

    float zoomDelta = (delta > 0) ? 0.05f : -0.05f;
    AdjustZoom(zoomDelta);

    if (resizeCallback) resizeCallback();

    HMENU menu = GetMenu(hwnd);
    if (menu) UpdateMenuState(menu);

    return true;
}

// Apply dark theme to window
inline void ApplyDarkTheme(HWND hwnd) {
    EnableDarkTitleBar(hwnd);
    SetCaptionColor(hwnd, Colors::Surface);
    SetWindowBorderColor(hwnd, Colors::Border);

    HMENU menu = CreateAppMenu();
    SetMenu(hwnd, menu);
    UpdateMenuState(menu);

    RedrawWindow(hwnd, nullptr, nullptr, RDW_INVALIDATE | RDW_FRAME | RDW_UPDATENOW);
}

// Extra information for the title bar when "Show Statistics" is on.
struct StatsInfo {
    int  sourceW = 0, sourceH = 0;   // per-eye XR swapchain dims
    int  clientW = 0, clientH = 0;   // preview window client dims
    float headX = 0, headY = 0, headZ = 0;
    float yawDeg = 0, pitchDeg = 0, rollDeg = 0;
};

// Briefly-shown "Screenshot saved" notice. Set by the capture path; the
// title-bar updater displays it for a few seconds, then it expires.
inline std::wstring g_lastScreenshotPath;
inline DWORD        g_lastScreenshotTickMs = 0;
constexpr DWORD     kScreenshotNoticeMs = 4000;

inline void NotifyScreenshotSaved(const std::wstring& path) {
    g_lastScreenshotPath = path;
    g_lastScreenshotTickMs = GetTickCount();
}

// Update window title with current state info. When `stats` is non-null and
// "Show Statistics" is on, the title gains a stats suffix. A recent screenshot
// notice (within kScreenshotNoticeMs) is prepended.
inline void UpdateWindowTitle(HWND hwnd, int fps = 0, int frameCount = 0,
                              const StatsInfo* stats = nullptr) {
    wchar_t title[768];

    const wchar_t* viewModeStr = L"Both Eyes";
    if (g_uiState.viewMode == ViewMode::LeftEyeOnly) viewModeStr = L"Left Eye";
    else if (g_uiState.viewMode == ViewMode::RightEyeOnly) viewModeStr = L"Right Eye";

    wchar_t zoomStr[32];
    if (g_uiState.fitToWindow) {
        wcscpy_s(zoomStr, L"Fit");
    } else {
        swprintf_s(zoomStr, L"%d%%", (int)(g_uiState.zoomLevel * 100));
    }

    wchar_t base[512];
    if (fps > 0) {
        swprintf_s(base, L"OpenXR Simulator - %s - %s - %s - %dmm - %d FPS",
                   viewModeStr, zoomStr, GetHeadsetProfileShortName(), GetIpdMillimeters(), fps);
    } else {
        swprintf_s(base, L"OpenXR Simulator - %s - %s - %s - %dmm",
                   viewModeStr, zoomStr, GetHeadsetProfileShortName(), GetIpdMillimeters());
    }

    wchar_t statsSuffix[256] = L"";
    if (g_uiState.showStats && stats) {
        swprintf_s(statsSuffix,
            L"  |  Src %dx%d  Win %dx%d  Head (%.2f,%.2f,%.2f) Yaw %.0f° Pitch %.0f°",
            stats->sourceW, stats->sourceH, stats->clientW, stats->clientH,
            stats->headX, stats->headY, stats->headZ,
            stats->yawDeg, stats->pitchDeg);
    }

    // Optional one-shot "Screenshot saved" notice
    bool showNotice = (g_lastScreenshotTickMs != 0) &&
                      ((GetTickCount() - g_lastScreenshotTickMs) < kScreenshotNoticeMs);
    if (showNotice) {
        swprintf_s(title, L"[Screenshot saved → %s]  %s%s",
                   g_lastScreenshotPath.c_str(), base, statsSuffix);
    } else {
        swprintf_s(title, L"%s%s", base, statsSuffix);
        if (g_lastScreenshotTickMs != 0) {
            // Expired — clear so we don't keep re-evaluating.
            g_lastScreenshotPath.clear();
            g_lastScreenshotTickMs = 0;
        }
    }

    SetWindowTextW(hwnd, title);
}

} // namespace ui
