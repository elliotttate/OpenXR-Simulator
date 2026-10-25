param(
    [string]$NativeHud = (Join-Path $PSScriptRoot '..\..\build-ui-automation\flatout4-evidence\rc17f-popup-spacing-renderdoc\rc00c-statistics-reader-green\hud-ResourceId-1147.png'),
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
$taskNative = (Resolve-Path -LiteralPath $NativeHud).Path
Push-Location $PSScriptRoot
try {
    if (-not $SkipBuild) {
        & .\build.cmd
        if ($LASTEXITCODE -ne 0) { throw 'Isolated build failed.' }
    }
    New-Item -ItemType Directory -Path evidence -Force | Out-Null
    & .\build\mesh_test.exe 2>&1 | Tee-Object -FilePath evidence\mesh-volume.log
    if ($LASTEXITCODE -ne 0) { throw 'Geometry test failed.' }
    & .\build\mesh_test.exe --flat-control 2>&1 | Tee-Object -FilePath evidence\mesh-flat-negative.log
    if ($LASTEXITCODE -ne 1 -or -not ((Get-Content evidence\mesh-flat-negative.log -Raw) -match 'sides>0')) { throw 'Flat geometry negative control did not fail at the required side-geometry gate.' }
    & .\build\ui_panel_volume_probe.exe $taskNative evidence\flat-negative --flat-control 2>&1 | Tee-Object -FilePath evidence\warp-flat-negative.log
    if ($LASTEXITCODE -ne 1 -or -not ((Get-Content evidence\warp-flat-negative.log -Raw) -match 'No visible side depth')) { throw 'Flat WARP negative control did not fail at the required side-depth gate.' }
    & .\build\ui_panel_volume_probe.exe $taskNative evidence\stereo-negative --stereo-control 2>&1 | Tee-Object -FilePath evidence\warp-stereo-negative.log
    if ($LASTEXITCODE -ne 1 -or -not ((Get-Content evidence\warp-stereo-negative.log -Raw) -match 'Stereo world-space correspondence failed')) { throw 'Duplicated-eye negative control did not fail at the required stereo gate.' }
    & .\build\ui_panel_volume_probe.exe $taskNative evidence\volume 2>&1 | Tee-Object -FilePath evidence\warp-volume.log
    if ($LASTEXITCODE -ne 0) { throw 'WARP volume verification failed.' }
    $taskProvenance = [ordered]@{
        source = [IO.Path]::GetRelativePath((Resolve-Path '..\..').Path, $taskNative)
        sourceSha256 = (Get-FileHash -LiteralPath $taskNative -Algorithm SHA256).Hash
        sourceSize = @(2100, 1181)
        mainPanelCrop = @(105, 61, 1901, 971)
        buttonCrops = @(@(1772, 74, 87, 79), @(1903, 74, 88, 79))
        bodyThicknessMm = @(4, 8)
        buttonThicknessMm = @(4, 5)
        panelRearChamferMm = 0.6
        buttonRearChamferMm = 0.4
        buttonRestOffsetMm = 3
        hoverAdditionalMm = 30
        driver = 'D3D_DRIVER_TYPE_WARP'
        generatedAtUtc = [DateTime]::UtcNow.ToString('o')
    }
    $taskProvenance | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath evidence\volume\provenance.json -Encoding utf8
    Write-Output 'Verified: both negative controls rejected; 4 mm and 8 mm WARP prototypes passed.'
} finally { Pop-Location }
