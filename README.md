# OpenXR Simulator

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform](https://img.shields.io/badge/Platform-Windows-blue)](https://github.com)
[![OpenXR](https://img.shields.io/badge/OpenXR-1.0-green)](https://www.khronos.org/openxr/)

A lightweight OpenXR runtime that enables VR applications to run in a desktop window for development and testing without requiring a physical VR headset.

![KAJUqBgmzewBPq9YkMDsm5AwsucqBaUY6gw2eMLX](https://github.com/user-attachments/assets/4dd804e1-13f4-46eb-a540-7c5cb77bf09c)

## 🎯 Features

- **Desktop VR Preview** - Run VR applications in a resizable desktop window with side-by-side stereo view
- **Mouse & Keyboard Controls** - Navigate the virtual space using standard input devices
- **Proper sRGB Handling** - Automatic gamma correction for accurate color reproduction
- **Unity Compatible** - Fully tested with Unity's OpenXR plugin
- **Minimal Dependencies** - Only requires Windows and DirectX 11
- **Easy Setup** - Simple PowerShell scripts for registration/unregistration

## 🚀 Quick Start

### Prerequisites

- Windows 10/11 (64-bit)
- DirectX 11 compatible GPU
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
   - **Shift**: Move faster
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
- **Swapchain Rendering** - Creates D3D11 swapchains that applications render into
- **View Composition** - Blits stereo views to a desktop window using optimized shaders
- **Input Simulation** - Converts mouse/keyboard input to head pose and controller data

### Supported Features

- ✅ Core OpenXR 1.0 specification
- ✅ D3D11 graphics binding (`XR_KHR_D3D11_enable`)
- ✅ Win32 time conversion (`XR_KHR_win32_convert_performance_counter_time`)
- ✅ Multiple swapchain formats (sRGB, UNORM, HDR)
- ✅ Stereo rendering with configurable FOV
- ✅ Reference space tracking (LOCAL, STAGE, VIEW)
- ✅ Basic action system for input

### Limitations

- ❌ No hand tracking
- ❌ No haptic feedback
- ❌ No foveated rendering
- ❌ Limited to seated/standing experiences
- ❌ No OpenGL/Vulkan support (D3D11 only)

## 🎮 Configuration

### Field of View

The default FOV is set to 70° for comfortable desktop viewing. To modify, edit the FOV value in `src/runtime.cpp`:

```cpp
// In xrLocateViews_runtime function
views[i].fov = { -0.7f, 0.7f, 0.7f, -0.7f }; // tan(35°) ≈ 0.7
```

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
- Verify D3D11 support on your system
- Try running the application as administrator

### Performance issues

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
- Unity OpenXR Plugin team for compatibility testing
- Community contributors and testers

## 📞 Support

- **Issues**: [GitHub Issues](https://github.com/yourusername/OpenXR-Simulator/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourusername/OpenXR-Simulator/discussions)
- **Documentation**: [Wiki](https://github.com/yourusername/OpenXR-Simulator/wiki)

## 🗺️ Roadmap

- [ ] Linux support via Vulkan
- [ ] Configurable controller emulation
- [ ] Multi-monitor support
- [ ] Recording and playback functionality
- [ ] OpenXR validation layer integration
- [ ] GUI configuration tool

---

**Note**: This is a development tool and not intended for end-user VR experiences. For production VR applications, use a proper VR headset and runtime.
