<#
.SYNOPSIS
    Make the OpenXR Simulator the machine-wide OpenXR runtime.

.DESCRIPTION
    Same contract as MetaXRSimulator\activate_simulator.ps1: the previous
    ActiveRuntime is stashed in PreviousActiveRuntime so deactivate_simulator.ps1
    can put it back. Needs administrator rights and self-elevates.

    Prefer the per-process route where you can -- set XR_RUNTIME_JSON to this
    folder's openxr_simulator.json and the machine-wide registration (Virtual
    Desktop, SteamVR, Quest Link) is left completely alone.
#>
param(
    [int]$MessageTime = 5
)

$ErrorActionPreference = 'Stop'

function Get-SimulatorJson {
    $p = Join-Path $PSScriptRoot 'openxr_simulator.json'
    if (-not (Test-Path $p)) {
        Write-Error "openxr_simulator.json not found. Run .\build_simulator.ps1 first." -ErrorAction Stop
    }
    return (Resolve-Path $p).Path
}

function Get-ActiveRuntime {
    try { return Get-ItemPropertyValue -Path HKLM:\SOFTWARE\Khronos\OpenXR\1 -Name ActiveRuntime -ErrorAction SilentlyContinue }
    catch { return $null }
}

try {
    $simulatorJsonPath = Get-SimulatorJson
    $activeRuntimeJsonPath = Get-ActiveRuntime

    if ($activeRuntimeJsonPath -eq $simulatorJsonPath) {
        Write-Output "OpenXR ActiveRuntime has already been set to the OpenXR Simulator"
        Start-Sleep -Seconds $MessageTime
        return
    }

    if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]'Administrator')) {
        Write-Output "Relaunching the script with elevated privileges ..."
        Start-Process -FilePath PowerShell.exe -Verb Runas -Wait -ArgumentList ("-File `"" + $PSCommandPath + "`" " + $MyInvocation.UnboundArguments)
        Exit
    }

    if (-not (Test-Path -Path HKLM:SOFTWARE\Khronos\OpenXR\1)) {
        New-Item -Path HKLM:SOFTWARE\Khronos\OpenXR\1 -ItemType Directory -Force | Out-Null
    }

    if ($null -ne $activeRuntimeJsonPath) {
        Set-ItemProperty -Path HKLM:SOFTWARE\Khronos\OpenXR\1 -Name "PreviousActiveRuntime" -Value $activeRuntimeJsonPath
    }

    Set-ItemProperty -Path HKLM:SOFTWARE\Khronos\OpenXR\1 -Name "ActiveRuntime" -Value $simulatorJsonPath

    Write-Output "OpenXR ActiveRuntime set to the OpenXR Simulator: $simulatorJsonPath"
    Start-Sleep -Seconds $MessageTime
}
catch {
    Write-Error $_
    Start-Sleep -Seconds 5
}
