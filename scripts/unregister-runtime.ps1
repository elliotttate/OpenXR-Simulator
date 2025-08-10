<#
.SYNOPSIS
    Unregisters the OpenXR Simulator runtime and restores backup
.DESCRIPTION
    This script removes the OpenXR Simulator as the active runtime and 
    optionally restores a previously backed up runtime configuration.
    Auto-elevates to admin if needed.
#>

param(
    [int]$MessageTime = 5,
    [switch]$Force
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
            $scriptPath = Join-Path $PSScriptRoot "unregister-runtime.ps1"
        }
        
        $CommandLine = "-ExecutionPolicy Bypass -File `"$scriptPath`" -MessageTime $MessageTime"
        if ($Force) {
            $CommandLine += " -Force"
        }
        Start-Process -FilePath PowerShell.exe -Verb Runas -ArgumentList $CommandLine -Wait
        Exit
    }
}

Write-Host "OpenXR Simulator - Runtime Unregistration" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

$openxrPath = "$env:LOCALAPPDATA\openxr\1"
$runtimeFile = "$openxrPath\active_runtime.json"
$backupFile = "$openxrPath\active_runtime.backup.json"
$registryPath = "HKLM:\SOFTWARE\Khronos\OpenXR\1"

$changesMade = $false

try {
    # Check local runtime file
    if (Test-Path $runtimeFile) {
        # Check if current runtime is the simulator
        $current = Get-Content $runtimeFile | ConvertFrom-Json
        if ($current.runtime.library_path -match "openxr_simulator\.dll") {
            Write-Host "Current local runtime is OpenXR Simulator. Removing..." -ForegroundColor Yellow
            
            # Check for backup
            if (Test-Path $backupFile) {
                if ($Force) {
                    $restore = 'Y'
                } else {
                    $restore = Read-Host "Backup runtime found. Restore it? (Y/N)"
                }
                
                if ($restore -eq 'Y' -or $restore -eq 'y') {
                    Copy-Item $backupFile $runtimeFile -Force
                    Write-Host "✅ Restored previous runtime from backup" -ForegroundColor Green
                    
                    $restored = Get-Content $runtimeFile | ConvertFrom-Json
                    Write-Host "Restored runtime: $($restored.runtime.library_path)" -ForegroundColor White
                } else {
                    Remove-Item $runtimeFile -Force
                    Write-Host "✅ Removed OpenXR Simulator runtime (no runtime active)" -ForegroundColor Green
                }
            } else {
                Remove-Item $runtimeFile -Force
                Write-Host "✅ Removed OpenXR Simulator runtime (no backup to restore)" -ForegroundColor Green
            }
            $changesMade = $true
        } else {
            Write-Host "Current local runtime is not OpenXR Simulator:" -ForegroundColor Yellow
            Write-Host "  $($current.runtime.library_path)" -ForegroundColor White
        }
    } else {
        Write-Host "No local active runtime found." -ForegroundColor Yellow
    }

    # Check and update registry
    if (Test-Path $registryPath) {
        try {
            $regRuntime = Get-ItemPropertyValue -Path $registryPath -Name ActiveRuntime -ErrorAction SilentlyContinue
            
            if ($regRuntime -eq $runtimeFile -or $regRuntime -match "openxr_simulator") {
                Write-Host "Registry points to OpenXR Simulator. Updating..." -ForegroundColor Yellow
                
                # Try to restore previous runtime from registry
                try {
                    $previousRuntime = Get-ItemPropertyValue -Path $registryPath -Name PreviousActiveRuntime -ErrorAction SilentlyContinue
                    if ($previousRuntime) {
                        Set-ItemProperty -Path $registryPath -Name "ActiveRuntime" -Value $previousRuntime
                        Write-Host "✅ Restored previous runtime in registry: $previousRuntime" -ForegroundColor Green
                    } else {
                        # Remove the ActiveRuntime value if no previous runtime
                        Remove-ItemProperty -Path $registryPath -Name ActiveRuntime -ErrorAction SilentlyContinue
                        Write-Host "✅ Removed ActiveRuntime from registry (no previous runtime)" -ForegroundColor Green
                    }
                } catch {
                    # No previous runtime, just remove current
                    Remove-ItemProperty -Path $registryPath -Name ActiveRuntime -ErrorAction SilentlyContinue
                    Write-Host "✅ Removed ActiveRuntime from registry" -ForegroundColor Green
                }
                $changesMade = $true
            } else {
                Write-Host "Registry runtime is not OpenXR Simulator: $regRuntime" -ForegroundColor Yellow
            }
        } catch {
            Write-Host "No registry ActiveRuntime set." -ForegroundColor Gray
        }
    }

    if ($changesMade) {
        Write-Host "`n✅ Successfully unregistered OpenXR Simulator" -ForegroundColor Green
    } else {
        Write-Host "`nNo changes made - OpenXR Simulator was not the active runtime" -ForegroundColor Yellow
    }

} catch {
    Write-Host "`n❌ ERROR: Failed to unregister runtime" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Start-Sleep -s $MessageTime
    exit 1
}

Write-Host "`nTo register again, run: .\register-runtime.ps1" -ForegroundColor Gray
Start-Sleep -s $MessageTime