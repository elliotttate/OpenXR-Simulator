<#
.SYNOPSIS
    Compile and run xr_probe -- a standalone replay of BetterVR's OpenXR call
    sequence against whatever runtime XR_RUNTIME_JSON points at.

.DESCRIPTION
    Answers "would BetterVR survive on this runtime?" in a few seconds without
    launching Cemu. It mirrors src\rendering\openxr.cpp, renderer.cpp and
    swapchain.cpp: the same three mandatory extensions, the same D3D12 binding,
    the same two swapchain formats, the same action shapes, and a 30-frame loop
    submitting a projection layer with chained depth plus a quad layer.

    Exit code 0 means every check passed.

.PARAMETER RuntimeJson
    Runtime manifest to test. Defaults to the sibling OpenXR Simulator build.

.PARAMETER BetterVRRoot
    BetterVR checkout to borrow the vcpkg-built openxr_loader.lib from.

.PARAMETER Vulkan
    Run the Vulkan probe (xr_probe_vk.cpp) instead: the same sequence over
    XR_KHR_vulkan_enable2, with the Khronos validation layer on and a real render
    pass into every acquired image. Needs the Vulkan SDK (VULKAN_SDK).

.PARAMETER Enable1
    With -Vulkan, take the XR_KHR_vulkan_enable (v1) path instead of _enable2.

.EXAMPLE
    .\run_xr_probe.ps1
    .\run_xr_probe.ps1 -Vulkan
    .\run_xr_probe.ps1 -Vulkan -Enable1
    .\run_xr_probe.ps1 -RuntimeJson ..\..\BotW-BetterVR\MetaXRSimulator\meta_openxr_simulator.json
#>
[CmdletBinding()]
param(
    [string]$RuntimeJson = (Join-Path $PSScriptRoot '..\..\BotW-BetterVR\OpenXRSimulator\openxr_simulator.json'),
    [string]$BetterVRRoot = (Join-Path $PSScriptRoot '..\..\BotW-BetterVR'),
    [switch]$Vulkan,
    [switch]$Enable1
)

$ErrorActionPreference = 'Stop'
$probeName = if ($Vulkan) { 'xr_probe_vk' } else { 'xr_probe' }
$exe = Join-Path $PSScriptRoot "$probeName.exe"

if (-not (Test-Path $RuntimeJson)) { throw "Runtime manifest not found: $RuntimeJson. Run ..\build_simulator.ps1 first." }
if (-not (Test-Path $BetterVRRoot)) { throw "BetterVR checkout not found: $BetterVRRoot. Pass -BetterVRRoot." }

# The probe links the same static OpenXR loader the layer does, so reuse whichever
# vcpkg tree the user has actually configured.
$vcpkg = Get-ChildItem -Path $BetterVRRoot -Directory -Filter 'cmake-build-*' |
    ForEach-Object { Join-Path $_.FullName 'vcpkg_installed\x64-windows-static' } |
    Where-Object { Test-Path (Join-Path $_ 'lib\openxr_loader.lib') } |
    Select-Object -First 1
if (-not $vcpkg) {
    $vcpkg = Join-Path $BetterVRRoot 'build\vcpkg_installed\x64-windows-static'
    if (-not (Test-Path (Join-Path $vcpkg 'lib\openxr_loader.lib'))) {
        throw "openxr_loader.lib not found. Configure a BetterVR build tree first so vcpkg fetches it."
    }
}

$vulkanSdk = $env:VULKAN_SDK
if ($Vulkan -and -not ($vulkanSdk -and (Test-Path (Join-Path $vulkanSdk 'Include\vulkan\vulkan.h')))) {
    throw 'The Vulkan probe needs the Vulkan SDK. Install it (VULKAN_SDK must point at it).'
}

$source = Join-Path $PSScriptRoot "$probeName.cpp"
if (-not (Test-Path $exe) -or (Get-Item $source).LastWriteTime -gt (Get-Item $exe).LastWriteTime) {
    $vsRoot = & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $vsRoot) { throw 'No Visual Studio installation with the C++ toolchain was found.' }
    Import-Module (Join-Path $vsRoot 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
    Enter-VsDevShell -VsInstallPath $vsRoot -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64' | Out-Null

    Write-Host "==> Compiling $probeName" -ForegroundColor Cyan
    Push-Location $PSScriptRoot
    try {
        if ($Vulkan) {
            & cl /nologo /std:c++17 /EHsc /MT "/I$vcpkg\include" "/I$vulkanSdk\Include" "$probeName.cpp" /Fe:"$probeName.exe" `
                /link "/LIBPATH:$vcpkg\lib" "/LIBPATH:$vulkanSdk\Lib" openxr_loader.lib jsoncpp.lib vulkan-1.lib advapi32.lib ole32.lib pathcch.lib
        } else {
            & cl /nologo /std:c++17 /EHsc /MT "/I$vcpkg\include" "$probeName.cpp" /Fe:"$probeName.exe" `
                /link "/LIBPATH:$vcpkg\lib" openxr_loader.lib jsoncpp.lib d3d12.lib dxgi.lib advapi32.lib ole32.lib pathcch.lib
        }
        if ($LASTEXITCODE -ne 0) { throw "cl failed ($LASTEXITCODE)" }
    } finally { Pop-Location }
}

$env:XR_RUNTIME_JSON = (Resolve-Path $RuntimeJson).Path
$env:XR_PROBE_VK_ENABLE1 = if ($Vulkan -and $Enable1) { '1' } else { '' }
Write-Host "==> Probing $env:XR_RUNTIME_JSON with $probeName" -ForegroundColor Cyan
& $exe
exit $LASTEXITCODE
