#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Unregisters the OpenXR Simulator runtime and restores backup
.DESCRIPTION
    This script removes the OpenXR Simulator as the active runtime and 
    optionally restores a previously backed up runtime configuration
#>

Write-Host "OpenXR Simulator - Runtime Unregistration" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

$openxrPath = "$env:LOCALAPPDATA\openxr\1"
$runtimeFile = "$openxrPath\active_runtime.json"
$backupFile = "$openxrPath\active_runtime.backup.json"

if (!(Test-Path $runtimeFile)) {
    Write-Host "No active runtime found. Nothing to unregister." -ForegroundColor Yellow
    exit 0
}

# Check if current runtime is the simulator
$current = Get-Content $runtimeFile | ConvertFrom-Json
if ($current.runtime.library_path -match "openxr_simulator\.dll") {
    Write-Host "Current runtime is OpenXR Simulator. Removing..." -ForegroundColor Yellow
    
    # Check for backup
    if (Test-Path $backupFile) {
        $restore = Read-Host "Backup runtime found. Restore it? (Y/N)"
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
} else {
    Write-Host "Current runtime is not OpenXR Simulator:" -ForegroundColor Yellow
    Write-Host "  $($current.runtime.library_path)" -ForegroundColor White
    Write-Host "No changes made." -ForegroundColor Gray
}

Write-Host "`nTo register again, run: .\register-runtime.ps1" -ForegroundColor Gray