<#
.SYNOPSIS
    Assemble and verify a redistributable OpenXR Simulator package.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9A-Za-z][0-9A-Za-z._-]*$')]
    [string]$Version,

    [Parameter(Mandatory)]
    [string]$X64Binary,

    [Parameter(Mandatory)]
    [string]$X86Binary,

    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\dist')
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$x64 = (Resolve-Path -LiteralPath $X64Binary).Path
$x86 = (Resolve-Path -LiteralPath $X86Binary).Path

function Get-PeMachine {
    param([string]$Path)

    $stream = [IO.File]::OpenRead($Path)
    $reader = [IO.BinaryReader]::new($stream)
    try {
        if ($reader.ReadUInt16() -ne 0x5a4d) {
            throw "$Path is not a PE binary"
        }
        $stream.Position = 0x3c
        $peOffset = $reader.ReadUInt32()
        if ($peOffset -gt ($stream.Length - 6)) {
            throw "$Path has an invalid PE header offset"
        }
        $stream.Position = $peOffset
        if ($reader.ReadUInt32() -ne 0x00004550) {
            throw "$Path has an invalid PE signature"
        }
        return $reader.ReadUInt16()
    }
    finally {
        $reader.Dispose()
        $stream.Dispose()
    }
}

$x64Machine = Get-PeMachine $x64
$x86Machine = Get-PeMachine $x86
if ($x64Machine -ne 0x8664) {
    throw "$x64 is not an x64 binary (PE machine 0x$($x64Machine.ToString('x4')))"
}
if ($x86Machine -ne 0x014c) {
    throw "$x86 is not an x86 binary (PE machine 0x$($x86Machine.ToString('x4')))"
}

New-Item -ItemType Directory -Force $OutputDirectory | Out-Null
$output = (Resolve-Path -LiteralPath $OutputDirectory).Path
$packageName = "OpenXR-Simulator-$Version-windows"
$packageDir = Join-Path $output $packageName
$zip = Join-Path $output "$packageName.zip"
$checksum = "$zip.sha256"
$verificationDir = Join-Path $output "$packageName-verification"

foreach ($path in @($packageDir, $zip, $checksum, $verificationDir)) {
    if (Test-Path -LiteralPath $path) {
        throw "Refusing to overwrite existing release output: $path"
    }
}

New-Item -ItemType Directory $packageDir | Out-Null
Copy-Item -LiteralPath $x64 -Destination (Join-Path $packageDir 'openxr_simulator.dll')
Copy-Item -LiteralPath $x86 -Destination (Join-Path $packageDir 'openxr_simulator-32.dll')

$commonFiles = @(
    'activate_simulator.ps1',
    'deactivate_simulator.ps1',
    'Activate Simulator.cmd',
    'Deactivate Simulator.cmd',
    'README.md',
    'LICENSE'
)
foreach ($file in $commonFiles) {
    Copy-Item -LiteralPath (Join-Path $root $file) -Destination $packageDir
}

$mcpDir = Join-Path $packageDir 'mcp-server'
New-Item -ItemType Directory $mcpDir | Out-Null
foreach ($file in @('openxr_simulator_mcp.py', 'pyproject.toml', 'README.md')) {
    Copy-Item -LiteralPath (Join-Path $root "mcp-server\$file") -Destination $mcpDir
}

function Write-RuntimeManifest {
    param(
        [string]$Path,
        [string]$Name,
        [string]$Library
    )

    @{
        file_format_version = '1.0.0'
        runtime = @{
            name = $Name
            library_path = $Library
        }
    } | ConvertTo-Json -Depth 3 |
        Set-Content -LiteralPath $Path -Encoding ascii
}

$x64ManifestArgs = @{
    Path = Join-Path $packageDir 'openxr_simulator.json'
    Name = 'OpenXR Simulator'
    Library = '.\openxr_simulator.dll'
}
Write-RuntimeManifest @x64ManifestArgs

$x86ManifestArgs = @{
    Path = Join-Path $packageDir 'openxr_simulator-32.json'
    Name = 'OpenXR Simulator x86'
    Library = '.\openxr_simulator-32.dll'
}
Write-RuntimeManifest @x86ManifestArgs

Compress-Archive -Path "$packageDir\*" -DestinationPath $zip
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
"$hash  $packageName.zip" | Set-Content -LiteralPath $checksum -Encoding ascii

try {
    Expand-Archive -LiteralPath $zip -DestinationPath $verificationDir

    $required = @(
        'openxr_simulator.dll',
        'openxr_simulator.json',
        'openxr_simulator-32.dll',
        'openxr_simulator-32.json',
        'Activate Simulator.cmd',
        'Deactivate Simulator.cmd',
        'mcp-server\openxr_simulator_mcp.py'
    )
    foreach ($file in $required) {
        if (-not (Test-Path -LiteralPath (Join-Path $verificationDir $file))) {
            throw "Release archive is missing $file"
        }
    }

    $x64Manifest = Get-Content -Raw (Join-Path $verificationDir 'openxr_simulator.json') | ConvertFrom-Json
    $x86Manifest = Get-Content -Raw (Join-Path $verificationDir 'openxr_simulator-32.json') | ConvertFrom-Json
    if ($x64Manifest.runtime.library_path -ne '.\openxr_simulator.dll') {
        throw 'The x64 runtime manifest is not relocatable'
    }
    if ($x86Manifest.runtime.library_path -ne '.\openxr_simulator-32.dll') {
        throw 'The x86 runtime manifest is not relocatable'
    }

    $verifiedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
    if ($verifiedHash -ne $hash) {
        throw 'Release archive checksum verification failed'
    }
}
finally {
    if (Test-Path -LiteralPath $verificationDir) {
        Remove-Item -LiteralPath $verificationDir -Recurse -Force
    }
}

Write-Host "Created $zip"
Write-Host "SHA-256 $hash"
