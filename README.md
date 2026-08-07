# OpenXR Simulator

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform](https://img.shields.io/badge/Platform-Windows-blue)](https://github.com)
[![OpenXR](https://img.shields.io/badge/OpenXR-1.0-green)](https://www.khronos.org/openxr/)

A lightweight OpenXR runtime that enables VR applications to run in a desktop window for development and testing without requiring a physical VR headset. Supports D3D11, D3D12, Vulkan and OpenGL graphics backends.

![KAJUqBgmzewBPq9YkMDsm5AwsucqBaUY6gw2eMLX](https://github.com/user-attachments/assets/4dd804e1-13f4-46eb-a540-7c5cb77bf09c)

## 🎯 Features

- **Multi-API Support** - Supports D3D11, D3D12, Vulkan and OpenGL graphics backends
- **Desktop VR Preview** - Run VR applications in a resizable desktop window with side-by-side stereo view
- **Headset Emulation** - Reproduces the measured per-eye FOV, panel resolution and IPD of ten popular headsets (Quest 2/3/Pro, Index, Vive Pro 2, Reverb G2, PS VR2, PICO 4, Bigscreen Beyond)
- **Mouse & Keyboard Controls** - Navigate the virtual space using standard input devices
- **Proper sRGB Handling** - Automatic gamma correction for accurate color reproduction
- **Unity & Unreal Compatible** - Tested with Unity's OpenXR plugin and Unreal Engine (via UEVR)
- **Steam Overlay Compatible** - D3D12 uses GDI-based rendering to avoid hook conflicts with Steam overlay
- **Minimal Dependencies** - Only requires Windows and a compatible GPU
- **Easy Setup** - Simple PowerShell scripts for registration/unregistration

## 🚀 Quick Start

### Prerequisites

- Windows 10/11 (64-bit)
- DirectX 11/12 or OpenGL compatible GPU
- Visual Studio 2022 (for building from source)
- CMake 3.20 or later (for building from source)

### Installation

1. Download the latest release from the [Releases](https://github.com/yourusername/OpenXR-Simulator/releases) page
2. Extract the archive to your preferred location
3. Run PowerShell as Administrator
4. Navigate to the `scripts` folder
5. Run `.\register-runtime.ps1` to set as active OpenXR runtime

```powershell
cd C:\Path\To\OpenXR-Simulator\scripts
.\register-runtime.ps1
```

### Usage

Once registered, any OpenXR application will automatically use the simulator:

1. Launch your VR application (e.g., Unity project with OpenXR)
2. A desktop window will appear showing left/right eye views
3. Use the following controls:
   - **Mouse**: Look around (hold right-click)
   - **WASD**: Move forward/backward/strafe
   - **Q/E**: Move up/down
   - **Shift**: Hold to move faster
   - **, / .**: Slower/faster movement
   - **ESC**: Release mouse capture

### Uninstallation

To restore your previous OpenXR runtime:

```powershell
cd C:\Path\To\OpenXR-Simulator\scripts
.\unregister-runtime.ps1
```

## 🛠️ Building from Source

### Clone the Repository

```bash
git clone https://github.com/yourusername/OpenXR-Simulator.git
cd OpenXR-Simulator
```

### Build with CMake

```bash
mkdir build
cd build
cmake .. -G "Visual Studio 17 2022" -A x64
cmake --build . --config Release
```

The built runtime will be in `build/bin/Release/openxr_simulator.dll`

## 📖 Technical Details

### Architecture

The simulator implements the OpenXR runtime interface, intercepting all OpenXR calls from applications:

- **Instance & Session Management** - Handles OpenXR instance creation and session lifecycle
- **Swapchain Rendering** - Creates swapchains for D3D11, D3D12, and OpenGL that applications render into
- **View Composition** - Blits stereo views to a desktop window (D3D11: DXGI swapchain, D3D12: GPU downscale to window size then readback, OpenGL: pixel buffer readback)
- **Input Simulation** - Converts mouse/keyboard input to head pose and controller data

### Supported Features

- ✅ Core OpenXR 1.0 specification
- ✅ D3D11 graphics binding (`XR_KHR_D3D11_enable`)
- ✅ D3D12 graphics binding (`XR_KHR_D3D12_enable`)
- ✅ Vulkan graphics binding (`XR_KHR_vulkan_enable` and `XR_KHR_vulkan_enable2`)
- ✅ OpenGL graphics binding (`XR_KHR_opengl_enable`)
- ✅ Win32 time conversion (`XR_KHR_win32_convert_performance_counter_time`)
- ✅ Multiple swapchain formats (sRGB, UNORM, HDR, typeless, depth)
- ✅ Mutable format swapchains (typeless backing for sRGB/non-sRGB views)
- ✅ Stereo rendering with configurable FOV
- ✅ Reference space tracking (LOCAL, STAGE, VIEW)
- ✅ Basic action system for input
- ✅ Screenshot capture (D3D11, D3D12, and OpenGL)

### Vulkan sessions

The compositor is D3D12 whatever the app binds. A Vulkan session's swapchain images are
D3D12 committed resources created `SHARED`, imported into the app's own `VkDevice` through
`VK_EXTERNAL_MEMORY_HANDLE_TYPE_D3D12_RESOURCE_BIT`, and handed back as `VkImage`s — so the
app renders in Vulkan and the preview, quad layers, screenshots and burst capture read the
same pixels as `ID3D12Resource`s with no second code path. `xrGetVulkanGraphicsDevice2KHR`
returns the `VkPhysicalDevice` whose `deviceLUID` matches the DXGI adapter the compositor
runs on, which is what makes the shared-handle import legal.

`xrEnumerateSwapchainFormats` reports `VkFormat` values under a Vulkan session. Images are
handed over in `VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL` (depth:
`VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL`) and must be released in the same layout,
as the spec requires — an app renders straight into an acquired image with no barrier of its
own, so the runtime owes it that layout and never changes it afterwards.

The app's `VkQueue` and the compositor's D3D12 queue are ordered by one shared `ID3D12Fence`
imported as a timeline `VkSemaphore`, driven by a single strictly increasing counter that
each side signals in turn. `SIMXR_VK_NO_TIMELINE=1` falls back to CPU waits, which is correct
but serialises the frame — useful when a driver's timeline import misbehaves.

Colour resources carry `D3D12_RESOURCE_FLAG_ALLOW_SIMULTANEOUS_ACCESS` (no DCC, so the bytes
Vulkan wrote are readable through a D3D12 SRV), depth uses a typeless DXGI format, and both
set `D3D12_DEFAULT_RESOURCE_PLACEMENT_ALIGNMENT` explicitly. Those three come from BetterVR's
own Vulkan/D3D12 bridge and are there for AMD.

### Limitations

- ❌ No hand tracking
- ❌ No haptic feedback
- ❌ No foveated rendering
- ❌ Limited to seated/standing experiences

## 🎮 Configuration

Settings are changed from the menu bar and persist across restarts in
`%LOCALAPPDATA%\OpenXR-Simulator\settings.json`. Delete the file to go back to
defaults.

### Headset Profiles

Pick a headset from **FOV → Headset Profile** to preset the per-eye frustum, the
panel resolution reported to `xrEnumerateViewConfigurationViews`, and a nominal
IPD. The values are measured ones from the
[HMD Geometry Database](https://risa2000.github.io/hmdgdb/). Add a profile by
appending a row to `ui::kHeadsetSpecs` in
[ui_enhancements.h](src/ui_enhancements.h); the enum, menu and settings keys
follow from the table.

### Movement Speed

WASD/QE move the head at 3 m/s by default, and holding **Shift** multiplies that
by 4. Both numbers are set from **Tools → Movement Speed**: presets from 0.5 to
10 m/s, `,` and `.` to step off them, and a submenu for the Shift multiplier.

### Mirror Rate

The preview window is a mirror of what the headset would show, and drawing it costs
the app a little time on every frame it updates. **Tools → Mirror Rate** caps how
often that happens: **60 Hz** by default, or 30/15 Hz, **Every Frame**, or **Off** to
freeze the mirror entirely while the application keeps running normally. Lower is
cheaper — turning it off leaves the runtime costing essentially nothing per frame,
which is worth doing while profiling the application itself. It applies to every
backend, and a screenshot request always forces a fresh frame through regardless of
the setting.

### Window Size

Default preview window is 1920x540 (960x540 per eye). Modify in `runtime.cpp`:

```cpp
static UINT g_persistentWidth = 1920;  // Total width
static UINT g_persistentHeight = 540;  // Height
```

### Background Color

The preview window background can be customized:

```cpp
const float clearColor[4] = {0.1f, 0.1f, 0.2f, 1.0f}; // Dark blue
```

## 🐛 Troubleshooting

### Application doesn't use the simulator

1. Verify registration:
```powershell
Get-Content "$env:LOCALAPPDATA\openxr\1\active_runtime.json"
```

2. Check for conflicting API layers:
```powershell
reg query "HKLM\SOFTWARE\Khronos\OpenXR\1\ApiLayers\Implicit"
```

3. Ensure no other VR runtime is running (SteamVR, Oculus, etc.)

### Colors appear washed out

- Disable any OpenXR API layers that modify rendering
- Ensure your application uses sRGB swapchain formats
- Check that no post-processing is double-applying gamma

### Preview window doesn't appear

- Check the log file: `%LOCALAPPDATA%\OpenXR-Simulator\openxr_simulator.log`
- Verify D3D11/D3D12/OpenGL support on your system
- Try running the application as administrator

### D3D12 applications crash or show stack overflow

- This is typically caused by DXGI Present hook conflicts with Steam overlay or UEVR. The simulator uses GDI-based rendering for D3D12 to avoid this, so make sure you're on the latest version.

### Performance issues

- Turn **Tools → Mirror Rate** down, or **Off**. Mirroring the eyes to the window is
  the only per-frame work the runtime does that scales with resolution.
- Leave `SIMXR_VERBOSE` unset. Setting it makes the runtime log every frame, and each
  line is flushed to disk — useful when diagnosing a frame, expensive as a default.
- Reduce swapchain resolution in your application
- Disable MSAA if enabled
- Close other GPU-intensive applications

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request. For major changes, please open an issue first to discuss what you would like to change.

### Development Setup

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- [Khronos Group](https://www.khronos.org/) for the OpenXR specification
- [OpenXR SDK](https://github.com/KhronosGroup/OpenXR-SDK) for headers and loader interfaces
- [HMD Geometry Database](https://risa2000.github.io/hmdgdb/) by risa2000 for the
  headset profile FOV data
- Unity OpenXR Plugin team for compatibility testing
- Community contributors and testers

## 📞 Support

- **Issues**: [GitHub Issues](https://github.com/yourusername/OpenXR-Simulator/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourusername/OpenXR-Simulator/discussions)
- **Documentation**: [Wiki](https://github.com/yourusername/OpenXR-Simulator/wiki)

## 🗺️ Roadmap

- [ ] Linux support
- [ ] Configurable controller emulation
- [ ] Multi-monitor support
- [ ] Recording and playback functionality
- [ ] OpenXR validation layer integration
- [ ] GUI configuration tool

---

**Note**: This is a development tool and not intended for end-user VR experiences. For production VR applications, use a proper VR headset and runtime.
