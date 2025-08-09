#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registers the OpenXR Simulator as the active OpenXR runtime
.DESCRIPTION
    This script sets the OpenXR Simulator as the system's active OpenXR runtime,
    allowing VR applications to use it for desktop preview
#>

param(
    [Parameter(Mandatory=$false)]
    [string]$RuntimePath = "$PSScriptRoot\..\bin\openxr_simulator.dll"
)

Write-Host "OpenXR Simulator - Runtime Registration" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Resolve full path
$RuntimePath = [System.IO.Path]::GetFullPath($RuntimePath)

# Check if DLL exists
if (!(Test-Path $RuntimePath)) {
    Write-Host "ERROR: Runtime DLL not found at: $RuntimePath" -ForegroundColor Red
    Write-Host "Please build the project first or specify the correct path" -ForegroundColor Yellow
    exit 1
}

# Create active_runtime.json content
$jsonContent = @{
    "file_format_version" = "1.0.0"
    "runtime" = @{
        "library_path" = $RuntimePath.Replace('\', '\\')
    }
} | ConvertTo-Json -Depth 3

# Determine OpenXR runtime path
$openxrPath = "$env:LOCALAPPDATA\openxr\1"
if (!(Test-Path $openxrPath)) {
    New-Item -Path $openxrPath -ItemType Directory -Force | Out-Null
    Write-Host "Created OpenXR directory: $openxrPath" -ForegroundColor Green
}

$runtimeFile = "$openxrPath\active_runtime.json"

# Backup existing runtime if it exists
if (Test-Path $runtimeFile) {
    $backupFile = "$openxrPath\active_runtime.backup.json"
    Copy-Item $runtimeFile $backupFile -Force
    Write-Host "Backed up existing runtime to: $backupFile" -ForegroundColor Yellow
}

# Write new runtime configuration
Set-Content -Path $runtimeFile -Value $jsonContent -Force
Write-Host "Registered OpenXR Simulator as active runtime" -ForegroundColor Green
Write-Host "Runtime path: $RuntimePath" -ForegroundColor White

# Verify registration
$verification = Get-Content $runtimeFile | ConvertFrom-Json
if ($verification.runtime.library_path -eq $RuntimePath.Replace('\', '\\')) {
    Write-Host "`n✅ SUCCESS: OpenXR Simulator is now the active runtime!" -ForegroundColor Green
    Write-Host "VR applications will now use the simulator for desktop preview" -ForegroundColor White
} else {
    Write-Host "`n❌ ERROR: Registration may have failed. Please check manually." -ForegroundColor Red
}

Write-Host "`nTo unregister, run: .\unregister-runtime.ps1" -ForegroundColor Gray