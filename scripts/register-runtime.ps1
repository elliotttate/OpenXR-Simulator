<#
.SYNOPSIS
    Registers the OpenXR Simulator as the active OpenXR runtime
.DESCRIPTION
    This script sets the OpenXR Simulator as the system's active OpenXR runtime,
    allowing VR applications to use it for desktop preview. Auto-elevates to admin if needed.
#>

param(
    [Parameter(Mandatory=$false)]
    [string]$RuntimePath = "",
    [int]$MessageTime = 5
)

# Auto-elevate to Administrator if not already elevated
if (-Not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] 'Administrator'))
{
    if ([int](Get-CimInstance -Class Win32_OperatingSystem | Select-Object -ExpandProperty BuildNumber) -ge 6000)
    {
        Write-Host "Relaunching with elevated privileges..." -ForegroundColor Yellow
        
        # Get the full path to this script
        $scriptPath = $MyInvocation.MyCommand.Path
        if (-not $scriptPath) {
            $scriptPath = Join-Path $PSScriptRoot "register-runtime.ps1"
        }
        
        # If RuntimePath wasn't specified, calculate the default based on script location
        if ([string]::IsNullOrEmpty($RuntimePath)) {
            $scriptDir = Split-Path -Parent $scriptPath
            $RuntimePath = Join-Path (Split-Path -Parent $scriptDir) "bin\openxr_simulator.dll"
        }
        
        $CommandLine = "-ExecutionPolicy Bypass -File `"$scriptPath`" -RuntimePath `"$RuntimePath`" -MessageTime $MessageTime"
        Start-Process -FilePath PowerShell.exe -Verb Runas -ArgumentList $CommandLine -Wait
        Exit
    }
}

Write-Host "OpenXR Simulator - Runtime Registration" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# If RuntimePath is still empty, set default based on script location
if ([string]::IsNullOrEmpty($RuntimePath)) {
    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    $RuntimePath = Join-Path (Split-Path -Parent $scriptDir) "bin\openxr_simulator.dll"
}

# Resolve full path
$RuntimePath = [System.IO.Path]::GetFullPath($RuntimePath)

# Check if DLL exists
if (!(Test-Path $RuntimePath)) {
    Write-Host "ERROR: Runtime DLL not found at: $RuntimePath" -ForegroundColor Red
    Write-Host "Please build the project first or specify the correct path" -ForegroundColor Yellow
    Start-Sleep -s $MessageTime
    exit 1
}

try {
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
    Write-Host "Updated local runtime configuration" -ForegroundColor Green

    # Also update the registry for system-wide setting
    $registryPath = "HKLM:\SOFTWARE\Khronos\OpenXR\1"
    
    # Create registry key if it doesn't exist
    if (!(Test-Path $registryPath)) {
        New-Item -Path $registryPath -ItemType Directory -Force | Out-Null
        Write-Host "Created OpenXR registry key" -ForegroundColor Green
    }

    # Get current active runtime for backup
    try {
        $currentRuntime = Get-ItemPropertyValue -Path $registryPath -Name ActiveRuntime -ErrorAction SilentlyContinue
        if ($currentRuntime -and $currentRuntime -ne $runtimeFile) {
            Set-ItemProperty -Path $registryPath -Name "PreviousActiveRuntime" -Value $currentRuntime
            Write-Host "Backed up previous runtime: $currentRuntime" -ForegroundColor Yellow
        }
    } catch {
        # No previous runtime set
    }

    # Set the active runtime in registry
    Set-ItemProperty -Path $registryPath -Name "ActiveRuntime" -Value $runtimeFile
    Write-Host "Updated registry with active runtime" -ForegroundColor Green

    # Verify registration
    $verification = Get-Content $runtimeFile | ConvertFrom-Json
    $regVerification = Get-ItemPropertyValue -Path $registryPath -Name ActiveRuntime -ErrorAction SilentlyContinue
    
    if ($verification.runtime.library_path -eq $RuntimePath.Replace('\', '\\') -and $regVerification -eq $runtimeFile) {
        Write-Host "`n✅ SUCCESS: OpenXR Simulator is now the active runtime!" -ForegroundColor Green
        Write-Host "Runtime path: $RuntimePath" -ForegroundColor White
        Write-Host "VR applications will now use the simulator for desktop preview" -ForegroundColor White
    } else {
        Write-Host "`n⚠ WARNING: Registration may be incomplete. Please check manually." -ForegroundColor Yellow
    }

    Write-Host "`nTo unregister, run: .\unregister-runtime.ps1" -ForegroundColor Gray
    Start-Sleep -s $MessageTime

} catch {
    Write-Host "`n❌ ERROR: Failed to register runtime" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Start-Sleep -s $MessageTime
    exit 1
}