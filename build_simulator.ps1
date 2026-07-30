<#
.SYNOPSIS
    Build the OpenXR Simulator runtime and install it into a BetterVR checkout.

.DESCRIPTION
    Installs openxr_simulator.dll, a relocatable openxr_simulator.json and the
    activate/deactivate scripts into the target folder, laid out the same way
    MetaXRSimulator\ is. Only build output lands there, never source -- BetterVR
    gitignores the whole folder.

.PARAMETER InstallTo
    Where to install. Defaults to the sibling BetterVR checkout.

.PARAMETER Clean
    Delete the CMake build tree first.
#>
[CmdletBinding()]
param(
    [string]$InstallTo = (Join-Path $PSScriptRoot '..\BotW-BetterVR\OpenXRSimulator'),
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$build = Join-Path $root 'build'

$vsRoot = & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsRoot) { throw 'No Visual Studio installation with the C++ toolchain was found.' }

$cmakeDir = Join-Path $vsRoot 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin'
$ninjaDir = Join-Path $vsRoot 'Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja'
if (Test-Path $cmakeDir) { $env:PATH = "$cmakeDir;$ninjaDir;$env:PATH" }
if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) { throw 'cmake was not found on PATH or in the Visual Studio installation.' }

Import-Module (Join-Path $vsRoot 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
Enter-VsDevShell -VsInstallPath $vsRoot -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64' | Out-Null

if ($Clean -and (Test-Path $build)) { Remove-Item -Recurse -Force $build }

Write-Host '==> Configuring' -ForegroundColor Cyan
cmake -S $root -B $build -G Ninja -DCMAKE_BUILD_TYPE=Release
if ($LASTEXITCODE -ne 0) { throw "cmake configure failed ($LASTEXITCODE)" }

Write-Host '==> Building' -ForegroundColor Cyan
cmake --build $build
if ($LASTEXITCODE -ne 0) { throw "cmake build failed ($LASTEXITCODE)" }

if (-not (Test-Path $InstallTo)) { New-Item -ItemType Directory -Force $InstallTo | Out-Null }
$InstallTo = (Resolve-Path $InstallTo).Path

Copy-Item (Join-Path $root 'bin\openxr_simulator.dll') $InstallTo -Force
Copy-Item (Join-Path $root 'activate_simulator.ps1') $InstallTo -Force
Copy-Item (Join-Path $root 'deactivate_simulator.ps1') $InstallTo -Force

# CMakeLists writes a manifest with an absolute library_path into bin\; install a
# relocatable one so the folder can be moved or copied without a rebuild.
$manifest = @{
    file_format_version = '1.0.0'
    runtime             = @{ library_path = '.\openxr_simulator.dll'; name = 'OpenXR Simulator' }
} | ConvertTo-Json -Depth 3
Set-Content -Path (Join-Path $InstallTo 'openxr_simulator.json') -Value $manifest -Encoding ascii

Write-Host ''
Write-Host "Installed to $InstallTo" -ForegroundColor Green
Write-Host ''
Write-Host 'Use it per-process (leaves the machine-wide runtime alone):'
Write-Host "  `$env:XR_RUNTIME_JSON = '$(Join-Path $InstallTo 'openxr_simulator.json')'"
Write-Host 'or machine-wide with .\activate_simulator.ps1 in that folder.'
