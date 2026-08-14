<#
.SYNOPSIS
    Build the OpenXR Simulator runtime and install it into a BetterVR checkout.

.DESCRIPTION
    Installs openxr_simulator.dll, a relocatable openxr_simulator.json and the
    activate/deactivate scripts into the target folder, laid out the same way
    MetaXRSimulator\ is. Only build output lands there, never source -- BetterVR
    gitignores the whole folder.

    The dll and scripts are symlinked, so a rebuild is live in BetterVR without
    reinstalling. Creating symlinks needs Developer Mode (or an elevated shell);
    without it the script copies instead and says so.

.PARAMETER InstallTo
    Where to install. Defaults to the sibling BetterVR checkout.

.PARAMETER Copy
    Install real copies instead of symlinks, for a folder that has to stand on
    its own -- moved to another machine, or kept working after the build tree goes.

.PARAMETER Clean
    Delete the CMake build tree first.
#>
[CmdletBinding()]
param(
    [string]$InstallTo = (Join-Path $PSScriptRoot '..\BotW-BetterVR\OpenXRSimulator'),
    [switch]$Copy,
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

$linked = 0
$copied = 0
function Install-Artifact {
    param([string]$Source, [string]$DestDir)

    $source = (Resolve-Path $Source).Path
    $dest = Join-Path $DestDir (Split-Path $Source -Leaf)

    if (Test-Path -LiteralPath $dest) {
        try { Remove-Item -LiteralPath $dest -Force -ErrorAction Stop }
        catch { throw "Cannot replace $dest -- a running process still has it open. Close it and rebuild." }
    }

    if (-not $script:Copy) {
        try {
            New-Item -ItemType SymbolicLink -Path $dest -Target $source -ErrorAction Stop | Out-Null
            $script:linked++
            return
        } catch {
            # No SeCreateSymbolicLinkPrivilege: fall back rather than fail the build.
        }
    }
    Copy-Item -LiteralPath $source -Destination $dest -Force
    $script:copied++
}

Install-Artifact (Join-Path $root 'bin\openxr_simulator.dll') $InstallTo
Install-Artifact (Join-Path $root 'activate_simulator.ps1') $InstallTo
Install-Artifact (Join-Path $root 'deactivate_simulator.ps1') $InstallTo

# CMakeLists writes a manifest with an absolute library_path into bin\; install a
# relocatable one that resolves next to whatever openxr_simulator.dll ends up being,
# link or copy. Written rather than linked: its contents never change with a build.
$manifest = @{
    file_format_version = '1.0.0'
    runtime             = @{ library_path = '.\openxr_simulator.dll'; name = 'OpenXR Simulator' }
} | ConvertTo-Json -Depth 3
Set-Content -Path (Join-Path $InstallTo 'openxr_simulator.json') -Value $manifest -Encoding ascii

Write-Host ''
if ($linked -gt 0 -and $copied -eq 0) {
    Write-Host "Symlinked into $InstallTo -- rebuilds are picked up with no reinstall" -ForegroundColor Green
} elseif ($linked -gt 0) {
    Write-Host "Installed to $InstallTo ($linked symlinked, $copied copied)" -ForegroundColor Green
} elseif ($Copy) {
    Write-Host "Copied to $InstallTo" -ForegroundColor Green
} else {
    Write-Host "Copied to $InstallTo -- symlinks need Developer Mode (Settings > System >" -ForegroundColor Yellow
    Write-Host "For developers) or an elevated shell; without it every build needs a reinstall." -ForegroundColor Yellow
}
Write-Host ''
Write-Host 'Use it per-process (leaves the machine-wide runtime alone):'
Write-Host "  `$env:XR_RUNTIME_JSON = '$(Join-Path $InstallTo 'openxr_simulator.json')'"
Write-Host 'or machine-wide with .\activate_simulator.ps1 in that folder.'
