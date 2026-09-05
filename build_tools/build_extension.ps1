[CmdletBinding()]
param(
    [string]$BlenderPath = "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
    [string]$PythonPath = "python",
    [string]$OutputDirectory = "",
    [ValidateSet("CCCoreLib", "SmallGicp", "Full")]
    [string]$AlignProfile = "CCCoreLib",
    [string]$SmallGicpRuntimePath = "",
    [string]$SmallGicpPackagePython = "",
    [string]$CcCoreRuntimePath = "",
    [string]$CcCorePythonPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$EmbeddedPythonVersion = "3.12.10"
$NumPyVersion = "1.26.4"
$SmallGicpVersion = "1.0.1"

function Assert-ChildPath {
    <# 대상 경로가 지정한 부모 폴더 안에 있는지 검증합니다. #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$ParentPath,
        [Parameter(Mandatory = $true)]
        [string]$ChildPath
    )

    $parentFullPath = [System.IO.Path]::GetFullPath($ParentPath).TrimEnd("\")
    $childFullPath = [System.IO.Path]::GetFullPath($ChildPath)
    if (-not $childFullPath.StartsWith($parentFullPath + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "허용된 부모 폴더 밖의 경로입니다: $childFullPath"
    }
}

function Remove-VerifiedDirectory {
    <# 검증된 임시 빌드 디렉터리만 재귀 삭제합니다. #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$ParentPath,
        [Parameter(Mandatory = $true)]
        [string]$TargetPath
    )

    Assert-ChildPath -ParentPath $ParentPath -ChildPath $TargetPath
    if (Test-Path -LiteralPath $TargetPath) {
        $item = Get-Item -LiteralPath $TargetPath -Force
        if (-not $item.PSIsContainer -or $item.Attributes.ToString().Contains("ReparsePoint")) {
            throw "삭제 대상이 일반 임시 디렉터리가 아닙니다: $TargetPath"
        }
        Remove-Item -LiteralPath $TargetPath -Recurse -Force
    }
}

function Copy-ExtensionSource {
    <# 통합 Runtime Source만 격리된 빌드 Staging으로 복사합니다. #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,
        [Parameter(Mandatory = $true)]
        [string]$StagingRoot
    )

    New-Item -ItemType Directory -Path $StagingRoot -Force | Out-Null
    foreach ($fileName in @("__init__.py", "blender_manifest.toml", "LICENSE", "README.md")) {
        $source = Join-Path $ProjectRoot $fileName
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Extension Staging에 필요한 파일이 없습니다: $fileName"
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $StagingRoot $fileName)
    }
    foreach ($directoryName in @("shell", "shared", "tools")) {
        $source = Join-Path $ProjectRoot $directoryName
        if (-not (Test-Path -LiteralPath $source -PathType Container)) {
            throw "Extension Staging에 필요한 폴더가 없습니다: $directoryName"
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $StagingRoot $directoryName) -Recurse
    }

    $staleRuntime = Join-Path $StagingRoot "tools\align\runtime"
    if (Test-Path -LiteralPath $staleRuntime) {
        Remove-VerifiedDirectory -ParentPath $StagingRoot -TargetPath $staleRuntime
    }
}

function Set-StagedBuildProfile {
    <# Staging의 Align 배포 프로필을 고정값으로 기록합니다. #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$PackagePath,
        [Parameter(Mandatory = $true)]
        [string]$ProfileName
    )

    $profileValue = switch ($ProfileName) {
        "CCCoreLib" { "CCCORELIB" }
        "SmallGicp" { "SMALL_GICP" }
        default { "FULL" }
    }
    $profileSource = "`"`"`"빌드가 포함한 선택형 backend 범위를 고정합니다.`"`"`"`n`nDISTRIBUTION_PROFILE = `"$profileValue`"`n"
    [System.IO.File]::WriteAllText(
        (Join-Path $PackagePath "_build_profile.py"),
        $profileSource,
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Remove-SmallGicpExcludedBackends {
    <# SmallGicp 배포판에서 선택형 Open3D·Hybrid 구현 파일을 제거합니다. #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$PackagePath
    )

    $optionalPath = Join-Path $PackagePath "optional_backends"
    if (Test-Path -LiteralPath $optionalPath) {
        Remove-VerifiedDirectory -ParentPath $PackagePath -TargetPath $optionalPath
    }
    foreach ($relativePath in @(
        "alignment\icp_solver.py",
        "alignment\icp_worker.py",
        "alignment\icp_open3d_solver.py",
        "alignment\icp_open3d_worker.py"
    )) {
        $targetPath = Join-Path $PackagePath $relativePath
        if (Test-Path -LiteralPath $targetPath -PathType Leaf) {
            Remove-Item -LiteralPath $targetPath -Force
        }
    }
}

function Remove-StagedPythonCaches {
    <# Staging에서 Python 캐시와 바이트코드를 제외합니다. #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$StagingRoot
    )

    Get-ChildItem -LiteralPath $StagingRoot -Recurse -Directory -Filter "__pycache__" |
        Sort-Object FullName -Descending |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $StagingRoot -Recurse -File |
        Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
        Remove-Item -Force
}

function Resolve-SmallGicpRuntimeSource {
    <# 명시 경로, 통합 캐시, 기존 Align 캐시 순서로 검증 대상 런타임을 찾습니다. #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,
        [string]$RequestedPath,
        [string]$PackagePython
    )

    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        $requested = [System.IO.Path]::GetFullPath($RequestedPath)
        if (-not (Test-Path -LiteralPath (Join-Path $requested "python.exe") -PathType Leaf)) {
            throw "지정한 Small GICP 런타임에 python.exe가 없습니다: $requested"
        }
        return (Resolve-Path -LiteralPath $requested).Path
    }

    $integratedRuntime = Join-Path $ProjectRoot ".small-gicp-runtime\python"
    if (Test-Path -LiteralPath (Join-Path $integratedRuntime "python.exe") -PathType Leaf) {
        return (Resolve-Path -LiteralPath $integratedRuntime).Path
    }

    $legacyRuntime = Join-Path $ProjectRoot "COLMAP_Align\.small-gicp-runtime\python"
    if (Test-Path -LiteralPath (Join-Path $legacyRuntime "python.exe") -PathType Leaf) {
        return (Resolve-Path -LiteralPath $legacyRuntime).Path
    }

    $prepareScript = Join-Path $PSScriptRoot "prepare_small_gicp_runtime.ps1"
    if ([string]::IsNullOrWhiteSpace($PackagePython)) {
        & $prepareScript -RuntimePath $integratedRuntime
    }
    else {
        & $prepareScript -RuntimePath $integratedRuntime -PackagePython $PackagePython
    }
    if (-not (Test-Path -LiteralPath (Join-Path $integratedRuntime "python.exe") -PathType Leaf)) {
        throw "Small GICP 내장 런타임 준비 결과를 찾을 수 없습니다: $integratedRuntime"
    }
    return (Resolve-Path -LiteralPath $integratedRuntime).Path
}

function Test-SmallGicpRuntime {
    <# 이동식 Python의 필수 파일·버전과 실제 합성 GICP 결과를 검증합니다. #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$RuntimePath,
        [Parameter(Mandatory = $true)]
        [string]$WorkerPath
    )

    foreach ($requiredPath in @(
        "python.exe",
        "python312.dll",
        "python312._pth",
        "runtime_manifest.json",
        "LICENSE.txt",
        "Lib\site-packages\numpy\__init__.py",
        "Lib\site-packages\numpy-1.26.4.dist-info\LICENSE.txt",
        "Lib\site-packages\small_gicp.cp312-win_amd64.pyd",
        "Lib\site-packages\small_gicp-1.0.1.dist-info\licenses\LICENSE"
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $RuntimePath $requiredPath) -PathType Leaf)) {
            throw "Small GICP 내장 런타임 필수 파일이 없습니다: $requiredPath"
        }
    }
    if (Test-Path -LiteralPath (Join-Path $RuntimePath "Lib\site-packages\open3d")) {
        throw "Small GICP 내장 런타임에 Open3D가 포함되어 있습니다."
    }
    if (Test-Path -LiteralPath (Join-Path $RuntimePath "pyvenv.cfg")) {
        throw "Small GICP 내장 런타임에 이동할 수 없는 가상환경 설정이 포함되어 있습니다."
    }

    $runtimeManifest = Get-Content -Raw -LiteralPath (Join-Path $RuntimePath "runtime_manifest.json") | ConvertFrom-Json
    if (
        $runtimeManifest.python_version -ne $EmbeddedPythonVersion -or
        $runtimeManifest.numpy_version -ne $NumPyVersion -or
        $runtimeManifest.small_gicp_version -ne $SmallGicpVersion -or
        $runtimeManifest.platform -ne "win-amd64"
    ) {
        throw "Small GICP Runtime Manifest의 버전 또는 플랫폼이 배포 계약과 다릅니다."
    }

    $runtimePython = Join-Path $RuntimePath "python.exe"
    $probe = & $runtimePython -I $WorkerPath --check-environment --backend SMALL_GICP
    if ($LASTEXITCODE -ne 0) {
        throw "Small GICP 내장 런타임의 합성 GICP 검사가 실패했습니다."
    }
    $environment = $probe | ConvertFrom-Json
    if (
        -not $environment.ok -or
        $environment.worker_python_version -ne $EmbeddedPythonVersion -or
        $environment.worker_numpy_version -ne $NumPyVersion -or
        $environment.small_gicp_version -ne $SmallGicpVersion -or
        -not $environment.synthetic_gicp_smoke.passed
    ) {
        throw "Small GICP 내장 런타임의 버전 또는 합성 GICP 결과가 올바르지 않습니다."
    }
    return $environment
}

function Add-SmallGicpEmbeddedRuntime {
    <# 검증 대상 런타임을 Staging에 복사한 뒤 복사본으로 합성 GICP를 재검증합니다. #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$RuntimeSource,
        [Parameter(Mandatory = $true)]
        [string]$AlignPackagePath
    )

    $runtimeDestination = Join-Path $AlignPackagePath "runtime\python"
    New-Item -ItemType Directory -Path (Split-Path -Parent $runtimeDestination) -Force | Out-Null
    Copy-Item -LiteralPath $RuntimeSource -Destination $runtimeDestination -Recurse
    return Test-SmallGicpRuntime `
        -RuntimePath $runtimeDestination `
        -WorkerPath (Join-Path $AlignPackagePath "alignment\icp_small_gicp_worker.py")
}

function Add-CcCoreEmbeddedRuntime {
    <# 고정 CC 런타임을 준비하고 복사한 스테이징에서 실제 최소 정렬을 검사합니다. #>
    param([string]$ProjectRoot, [string]$AlignPackagePath)

    $helper = Join-Path $PSScriptRoot "cccore_distribution.py"
    $cacheRoot = Join-Path $ProjectRoot ".cccore-runtime"
    $pythonSource = if ($CcCorePythonPath) { [IO.Path]::GetFullPath($CcCorePythonPath) } else { Join-Path $cacheRoot "python" }
    $nativeSource = if ($CcCoreRuntimePath) { [IO.Path]::GetFullPath($CcCoreRuntimePath) } else { Join-Path $cacheRoot "cccore" }
    if (!$CcCorePythonPath -and !(Test-Path -LiteralPath (Join-Path $pythonSource "python.exe"))) {
        & $PythonPath -X utf8 $helper prepare-python
        if ($LASTEXITCODE -ne 0) { throw "정식 CC Python 준비에 실패했습니다." }
    }
    if (!$CcCoreRuntimePath -and !(Test-Path -LiteralPath (Join-Path $nativeSource "colmap_cccore_worker.exe"))) {
        & (Join-Path $PSScriptRoot "prepare_cccore_runtime.ps1") -EnvironmentRoot $cacheRoot -RuntimePath $nativeSource
        if ($LASTEXITCODE -ne 0) { throw "정식 CC 네이티브 런타임 준비에 실패했습니다." }
    }
    $runtime = Join-Path $AlignPackagePath "runtime"
    & $PythonPath -X utf8 $helper stage --python-source $pythonSource --native-source $nativeSource --destination $runtime
    if ($LASTEXITCODE -ne 0) { throw "정식 CC 실행 환경의 스테이징 검증에 실패했습니다." }
    $nativeWorker = Join-Path $runtime "cccore\colmap_cccore_worker.exe"
    $result = & (Join-Path $runtime "python\python.exe") -I (Join-Path $AlignPackagePath "alignment\icp_cccore_worker.py") --check-environment --native-worker $nativeWorker
    if ($LASTEXITCODE -ne 0) { throw "스테이징 CC 정렬 환경 검사가 실패했습니다." }
    $environment = $result | ConvertFrom-Json
    if (!$environment.ok -or $environment.backend_id -ne "CCCORELIB" -or
        $environment.algorithm_version -ne "cccore-icp-v3" -or $environment.job_protocol_version -ne "cccore-job-v3" -or
        $environment.native_environment.self_test -ne "passed") {
        throw "스테이징 CC 엔진 또는 최소 정렬 결과가 배포 계약과 다릅니다."
    }
    Write-Host "CCCoreLib 스테이징 실제 정렬 검사 통과"
}

function Test-BuiltArchive {
    <# ZIP의 통합 Runtime Source와 Align 프로필별 포함·제외 계약을 검증합니다. #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$ArchivePath,
        [Parameter(Mandatory = $true)]
        [string]$ProfileName
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        $entryNames = @($archive.Entries | ForEach-Object { $_.FullName.Replace("\", "/") })
        foreach ($required in @(
            "__init__.py",
            "blender_manifest.toml",
            "shell/lifecycle.py",
            "shared/blender_runtime/workspace_factory.py",
            "shared/blender_runtime/scene_marker_session.py",
            "shared/colmap_dataset/blender/registry.py",
            "shared/colmap_dataset/blender/view_layers.py",
            "shared/colmap_dataset/contracts.py",
            "shared/colmap_transfer/contracts.py",
            "shared/colmap_transfer/registry.py",
            "tools/align/_build_profile.py",
            "tools/align/registration.py",
            "tools/align/alignment/icp_external_contract.py",
            "tools/align/resources/workspaces/colmap_align_seed_5_2.blend",
            "tools/modifier/blender/lifecycle.py",
            "tools/modifier/blender/dataset_adapter.py",
            "tools/modifier/blender/projection_snapshot_provider.py",
            "tools/modifier/blender/camera_review_keymap.py",
            "tools/modifier/blender/modifier_data_visibility.py",
            "tools/modifier/resources/workspaces/colmap_modifier_seed_5_2.blend",
            "tools/projection_paint/blender/lifecycle.py",
            "tools/projection_paint/blender/dataset_adapter.py",
            "tools/projection_paint/blender/dataset_view_layer.py",
            "tools/projection_paint/blender/snapshot_scene_transaction.py",
            "tools/projection_paint/blender/snapshot_update.py",
            "tools/projection_paint/resources/workspaces/projection_paint_seed_5_2.blend"
        )) {
            if ($entryNames -notcontains $required) {
                throw "Extension ZIP에 필수 Runtime Source가 없습니다: $required"
            }
        }

        $profileEntry = $archive.GetEntry("tools/align/_build_profile.py")
        $profileReader = [System.IO.StreamReader]::new($profileEntry.Open())
        try {
            $profileSource = $profileReader.ReadToEnd()
        }
        finally {
            $profileReader.Dispose()
        }

        if ($ProfileName -in @("CCCoreLib", "Full")) {
            $expectedProfile = if ($ProfileName -eq "Full") { "FULL" } else { "CCCORELIB" }
            if ($profileSource -notmatch ('DISTRIBUTION_PROFILE\s*=\s*"' + $expectedProfile + '"')) {
                throw "CC ZIP의 배포 프로필이 요청과 다릅니다: $expectedProfile"
            }
            foreach ($required in @(
                "tools/align/alignment/icp_cccore_solver.py",
                "tools/align/alignment/icp_cccore_worker.py",
                "tools/align/runtime/python/python.exe",
                "tools/align/runtime/python/runtime_manifest.json",
                "tools/align/runtime/cccore/colmap_cccore_worker.exe",
                "tools/align/runtime/cccore/CCCoreLib.dll",
                "tools/align/runtime/cccore/tbb12.dll",
                "tools/align/runtime/cccore/runtime_manifest.json",
                "tools/align/runtime/cccore/cccore_rebuild_sources.zip",
                "tools/align/runtime/cccore/licenses/CCCoreLib-LGPL-2.0.txt",
                "tools/align/runtime/cccore/licenses/oneTBB-Apache-2.0.txt"
            )) {
                if ($entryNames -notcontains $required) { throw "정식 CC ZIP에 필수 파일이 없습니다: $required" }
            }
            foreach ($entry in $entryNames) {
                if (($ProfileName -eq "CCCoreLib" -and $entry -match 'tools/align/(optional_backends/|alignment/icp_(small_gicp|open3d|solver\.py|worker\.py))') -or
                    $entry -match 'tools/align/runtime/python/.*small_gicp') {
                    throw "정식 CC ZIP에 제외한 구엔진이 포함되었습니다: $entry"
                }
            }
        }
        elseif ($ProfileName -eq "SmallGicp") {
            if ($profileSource -notmatch 'DISTRIBUTION_PROFILE\s*=\s*"SMALL_GICP"') {
                throw "SmallGicp ZIP의 Align 배포 프로필이 SMALL_GICP가 아닙니다."
            }
            foreach ($required in @(
                "tools/align/alignment/icp_small_gicp_solver.py",
                "tools/align/alignment/icp_small_gicp_worker.py",
                "tools/align/runtime/python/python.exe",
                "tools/align/runtime/python/python312.dll",
                "tools/align/runtime/python/python312._pth",
                "tools/align/runtime/python/runtime_manifest.json",
                "tools/align/runtime/python/LICENSE.txt",
                "tools/align/runtime/python/Lib/site-packages/numpy/__init__.py",
                "tools/align/runtime/python/Lib/site-packages/numpy-1.26.4.dist-info/LICENSE.txt",
                "tools/align/runtime/python/Lib/site-packages/small_gicp.cp312-win_amd64.pyd",
                "tools/align/runtime/python/Lib/site-packages/small_gicp-1.0.1.dist-info/licenses/LICENSE"
            )) {
                if ($entryNames -notcontains $required) {
                    throw "SmallGicp ZIP에 내장 런타임 필수 파일이 없습니다: $required"
                }
            }
            $forbiddenPrefixes = @(
                "tools/align/optional_backends/",
                "tools/align/alignment/icp_open3d_",
                "tools/align/alignment/icp_solver.py",
                "tools/align/alignment/icp_worker.py",
                "tools/align/runtime/python/Lib/site-packages/open3d/"
            )
            foreach ($entry in $entryNames) {
                if ($forbiddenPrefixes | Where-Object { $entry.StartsWith($_, [System.StringComparison]::OrdinalIgnoreCase) }) {
                    throw "SmallGicp ZIP에 선택형 Backend 파일이 포함되었습니다: $entry"
                }
            }
            if ($entryNames -contains "tools/align/runtime/python/pyvenv.cfg") {
                throw "SmallGicp ZIP에 이동할 수 없는 가상환경 설정이 포함되었습니다."
            }

            $manifestEntry = $archive.GetEntry("tools/align/runtime/python/runtime_manifest.json")
            $manifestReader = [System.IO.StreamReader]::new($manifestEntry.Open())
            try {
                $runtimeManifest = $manifestReader.ReadToEnd() | ConvertFrom-Json
            }
            finally {
                $manifestReader.Dispose()
            }
            if (
                $runtimeManifest.python_version -ne $EmbeddedPythonVersion -or
                $runtimeManifest.numpy_version -ne $NumPyVersion -or
                $runtimeManifest.small_gicp_version -ne $SmallGicpVersion -or
                $runtimeManifest.platform -ne "win-amd64"
            ) {
                throw "SmallGicp ZIP의 Runtime Manifest가 배포 계약과 다릅니다."
            }
        }

        $forbiddenPattern = "^(test_env|tests|docs|build_tools|COLMAP_Align|COLMAP_Modifier|COLMAP_Projection_Paint)(/|$)"
        $unexpectedEntries = @($entryNames | Where-Object { $_ -match $forbiddenPattern })
        if ($unexpectedEntries.Count -gt 0) {
            throw "Extension ZIP에 비배포 항목이 포함되었습니다: $($unexpectedEntries -join ', ')"
        }
    }
    finally {
        $archive.Dispose()
    }
}

# 통합 Extension Root와 출력 위치를 확정합니다.
$projectRoot = Split-Path -Parent $PSScriptRoot
$projectRoot = (Resolve-Path -LiteralPath $projectRoot).Path
$buildOutput = if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    Join-Path $projectRoot "build"
}
elseif ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    [System.IO.Path]::GetFullPath($OutputDirectory)
}
else {
    [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
}

if (-not (Test-Path -LiteralPath $BlenderPath -PathType Leaf)) {
    throw "Blender 5.2 실행 파일을 찾을 수 없습니다: $BlenderPath"
}

# 정적 구조, 공존 식별자와 순수 Core 회귀 테스트를 먼저 실행합니다.
$testSuites = @(
    [pscustomobject]@{ Name = "Architecture"; Path = "tests\architecture" },
    [pscustomobject]@{ Name = "Coexistence"; Path = "tests\coexistence" },
    [pscustomobject]@{ Name = "Modifier Core"; Path = "tests\modifier\core" },
    [pscustomobject]@{ Name = "Projection Core"; Path = "tests\projection_paint\core" }
)
foreach ($suite in $testSuites) {
    # 내장 Python도 검증할 소스 루트를 명시적으로 탐색하며 실행 환경의 격리 설정은 바꾸지 않습니다.
    & $PythonPath -X utf8 -c "import sys,unittest; sys.path.insert(0,sys.argv[1]); suite=unittest.TestLoader().discover(sys.argv[2], pattern='test_*.py'); result=unittest.TextTestRunner(verbosity=1).run(suite); sys.exit(not result.wasSuccessful())" $projectRoot (Join-Path $projectRoot $suite.Path)
    if ($LASTEXITCODE -ne 0) {
        throw "$($suite.Name) 테스트에 실패했습니다."
    }
}

# Blender에서 통합 등록, 멱등성과 실패 Rollback을 검증합니다.
& $BlenderPath --background --factory-startup --python (Join-Path $projectRoot "tests\blender_background\test_extension_registration.py")
if ($LASTEXITCODE -ne 0) {
    throw "COLMAP Tools Blender Background 등록 검증에 실패했습니다."
}

$artifactRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot ".artifacts"))
$buildId = [Guid]::NewGuid().ToString("N")
$operationRoot = Join-Path $artifactRoot "extension_build_$buildId"
$stagingRoot = Join-Path $operationRoot "source"
$temporaryOutput = Join-Path $operationRoot "output"
$temporaryDestination = ""
$finalPackage = ""
Assert-ChildPath -ParentPath $artifactRoot -ChildPath $operationRoot

try {
    New-Item -ItemType Directory -Path $temporaryOutput -Force | Out-Null
    Copy-ExtensionSource -ProjectRoot $projectRoot -StagingRoot $stagingRoot
    $alignPackagePath = Join-Path $stagingRoot "tools\align"
    Set-StagedBuildProfile -PackagePath $alignPackagePath -ProfileName $AlignProfile

    if ($AlignProfile -in @("CCCoreLib", "Full")) {
        if ($AlignProfile -eq "CCCoreLib") {
            Remove-SmallGicpExcludedBackends -PackagePath $alignPackagePath
            foreach ($name in @("icp_small_gicp_solver.py", "icp_small_gicp_worker.py")) {
                Remove-Item -LiteralPath (Join-Path $alignPackagePath "alignment\$name") -Force
            }
        }
        Add-CcCoreEmbeddedRuntime -ProjectRoot $projectRoot -AlignPackagePath $alignPackagePath
    }
    elseif ($AlignProfile -eq "SmallGicp") {
        Remove-SmallGicpExcludedBackends -PackagePath $alignPackagePath
        $runtimeSource = Resolve-SmallGicpRuntimeSource `
            -ProjectRoot $projectRoot `
            -RequestedPath $SmallGicpRuntimePath `
            -PackagePython $SmallGicpPackagePython
        $smallGicpEnvironment = Add-SmallGicpEmbeddedRuntime `
            -RuntimeSource $runtimeSource `
            -AlignPackagePath $alignPackagePath
        Write-Host "Small GICP Staging 검증 완료 · Python $($smallGicpEnvironment.worker_python_version) · NumPy $($smallGicpEnvironment.worker_numpy_version) · small_gicp $($smallGicpEnvironment.small_gicp_version)"
    }
    Remove-StagedPythonCaches -StagingRoot $stagingRoot

    # 검증된 Staging만 Blender Extension Builder에 전달합니다.
    & $BlenderPath --background --factory-startup --command extension validate $stagingRoot
    if ($LASTEXITCODE -ne 0) {
        throw "COLMAP Tools Extension Staging 검증에 실패했습니다."
    }
    & $BlenderPath --background --factory-startup --command extension build --source-dir $stagingRoot --output-dir $temporaryOutput
    if ($LASTEXITCODE -ne 0) {
        throw "COLMAP Tools Extension 빌드에 실패했습니다."
    }

    $package = Get-ChildItem -LiteralPath $temporaryOutput -File -Filter "colmap_tools-*.zip" |
        Sort-Object -Property LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if ($null -eq $package) {
        throw "생성된 COLMAP Tools Extension ZIP를 찾을 수 없습니다."
    }
    Test-BuiltArchive -ArchivePath $package.FullName -ProfileName $AlignProfile
    & $PythonPath -X utf8 (Join-Path $PSScriptRoot "cccore_distribution.py") validate-archive $package.FullName
    if ($LASTEXITCODE -ne 0) { throw "배포 ZIP의 경로 또는 CC 파일 무결성 검사에 실패했습니다." }

    New-Item -ItemType Directory -Path $buildOutput -Force | Out-Null
    $finalPackage = Join-Path $buildOutput $package.Name
    $temporaryDestination = Join-Path $buildOutput (".$buildId-" + $package.Name)
    Copy-Item -LiteralPath $package.FullName -Destination $temporaryDestination
    if ((Get-FileHash -LiteralPath $temporaryDestination -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $package.FullName -Algorithm SHA256).Hash) {
        throw "최종 출력으로 복사한 Extension ZIP의 SHA-256이 달라졌습니다."
    }
    Move-Item -LiteralPath $temporaryDestination -Destination $finalPackage -Force
    $temporaryDestination = ""
}
finally {
    if (-not [string]::IsNullOrWhiteSpace($temporaryDestination) -and (Test-Path -LiteralPath $temporaryDestination)) {
        Remove-Item -LiteralPath $temporaryDestination -Force
    }
    Remove-VerifiedDirectory -ParentPath $artifactRoot -TargetPath $operationRoot
    if ((Test-Path -LiteralPath $artifactRoot -PathType Container) -and -not (Get-ChildItem -LiteralPath $artifactRoot -Force)) {
        Remove-Item -LiteralPath $artifactRoot -Force
    }
}

if ([string]::IsNullOrWhiteSpace($finalPackage) -or -not (Test-Path -LiteralPath $finalPackage -PathType Leaf)) {
    throw "검증된 COLMAP Tools Extension ZIP가 최종 출력에 없습니다."
}
$hash = (Get-FileHash -LiteralPath $finalPackage -Algorithm SHA256).Hash
Write-Host "COLMAP Tools Extension 빌드 완료: $finalPackage"
Write-Host "Align 배포 프로필: $AlignProfile"
Write-Host "SHA-256: $hash"
