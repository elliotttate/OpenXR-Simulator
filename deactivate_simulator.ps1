<#
.SYNOPSIS
    Restore the OpenXR runtime that was active before activate_simulator.ps1 ran.
#>
param(
    [int]$MessageTime = 5
)

$ErrorActionPreference = 'Stop'

function Get-RuntimeValue([string]$name) {
    try { return Get-ItemPropertyValue -Path HKLM:\SOFTWARE\Khronos\OpenXR\1 -Name $name -ErrorAction SilentlyContinue }
    catch { return $null }
}

try {
    $simulatorJsonPath = Join-Path $PSScriptRoot 'openxr_simulator.json'
    if (Test-Path $simulatorJsonPath) { $simulatorJsonPath = (Resolve-Path $simulatorJsonPath).Path }

    $activeRuntimeJsonPath = Get-RuntimeValue 'ActiveRuntime'
    if ($activeRuntimeJsonPath -ne $simulatorJsonPath) {
        Write-Output "The OpenXR Simulator is not the active OpenXR runtime"
        Start-Sleep -Seconds $MessageTime
        return
    }

    $previousActiveRuntimeJsonPath = Get-RuntimeValue 'PreviousActiveRuntime'
    if ($null -eq $previousActiveRuntimeJsonPath) {
        Write-Output "Can't find the previous active OpenXR runtime. Re-activate it from its own settings app (SteamVR, Quest Link, Virtual Desktop, ...)."
        Start-Sleep -Seconds $MessageTime
        return
    }

    if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]'Administrator')) {
        Write-Output "Relaunching the script with elevated privileges ..."
        Start-Process -FilePath PowerShell.exe -Verb Runas -Wait -ArgumentList ("-File `"" + $PSCommandPath + "`" " + $MyInvocation.UnboundArguments)
        Exit
    }

    Set-ItemProperty -Path HKLM:SOFTWARE\Khronos\OpenXR\1 -Name "ActiveRuntime" -Value $previousActiveRuntimeJsonPath
    Write-Output "OpenXR ActiveRuntime has been restored to $previousActiveRuntimeJsonPath"
    Remove-ItemProperty -Path HKLM:SOFTWARE\Khronos\OpenXR\1 -Name "PreviousActiveRuntime"

    Start-Sleep -Seconds $MessageTime
}
catch {
    Write-Error $_
    Start-Sleep -Seconds 5
}
