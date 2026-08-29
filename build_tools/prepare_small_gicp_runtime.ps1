[CmdletBinding()]
param(
    [string]$PackagePython = "",
    [string]$RuntimePath = "",
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$EmbeddedPythonVersion = "3.12.10"
$EmbeddedPythonArchiveName = "python-$EmbeddedPythonVersion-embed-amd64.zip"
$EmbeddedPythonUrl = "https://www.python.org/ftp/python/$EmbeddedPythonVersion/$EmbeddedPythonArchiveName"
$EmbeddedPythonSha256 = "4ACBED6DD1C744B0376E3B1CF57CE906F9DC9E95E68824584C8099A63025A3C3"
$NumPyVersion = "1.26.4"
$SmallGicpVersion = "1.0.1"

function Assert-ChildPath {
    <# 대상 경로가 지정한 프로젝트 내부 부모 경로에 포함되는지 검증합니다. #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$ParentPath,
        [Parameter(Mandatory = $true)]
        [string]$ChildPath
    )

    $parentFullPath = [System.IO.Path]::GetFullPath($ParentPath).TrimEnd("\")
    $childFullPath = [System.IO.Path]::GetFullPath($ChildPath)
    if (-not $childFullPath.StartsWith($parentFullPath + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "프로젝트 폴더 밖의 Small GICP 런타임 경로입니다: $childFullPath"
    }
}

function Remove-VerifiedDirectory {
    <# 검증된 프로젝트 내부 런타임 또는 Staging 디렉터리만 재귀 삭제합니다. #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,
        [Parameter(Mandatory = $true)]
        [string]$TargetPath
    )

    Assert-ChildPath -ParentPath $ProjectRoot -ChildPath $TargetPath
    if (Test-Path -LiteralPath $TargetPath) {
        $item = Get-Item -LiteralPath $TargetPath -Force
        if (-not $item.PSIsContainer -or $item.Attributes.ToString().Contains("ReparsePoint")) {
            throw "삭제 대상이 일반 프로젝트 내부 디렉터리가 아닙니다: $TargetPath"
        }
        Remove-Item -LiteralPath $TargetPath -Recurse -Force
    }
}

function Assert-PackagePython {
    <# Wheel 설치에 사용할 Python이 CPython 3.12 Windows x64인지 확인합니다. #>
    param([string]$PythonExecutable)

    $probe = & $PythonExecutable -I -c "import json, platform, struct, sys, sysconfig; print(json.dumps({'implementation': platform.python_implementation(), 'major': sys.version_info.major, 'minor': sys.version_info.minor, 'bits': struct.calcsize('P') * 8, 'platform': sysconfig.get_platform()}))"
    if ($LASTEXITCODE -ne 0) {
        throw "Small GICP Wheel 설치용 Python 정보를 확인하지 못했습니다."
    }
    $environment = $probe | ConvertFrom-Json
    if (
        $environment.implementation -ne "CPython" -or
        $environment.major -ne 3 -or
        $environment.minor -ne 12 -or
        $environment.bits -ne 64 -or
        $environment.platform -ne "win-amd64"
    ) {
        throw "Small GICP 배포 런타임 조립에는 CPython 3.12 Windows x64가 필요합니다."
    }
}

function Test-EmbeddedRuntime {
    <# 조립한 이동식 Python으로 실제 small_gicp 합성 GICP를 실행해 결과를 반환합니다. #>
    param(
        [string]$PythonExecutable,
        [string]$WorkerPath
    )

    $probe = & $PythonExecutable -I $WorkerPath --check-environment --backend SMALL_GICP
    if ($LASTEXITCODE -ne 0) {
        throw "내장 Small GICP Worker 환경 검사가 실패했습니다."
    }
    $environment = $probe | ConvertFrom-Json
    if (
        -not $environment.ok -or
        $environment.worker_python_version -ne $EmbeddedPythonVersion -or
        $environment.worker_numpy_version -ne $NumPyVersion -or
        $environment.small_gicp_version -ne $SmallGicpVersion -or
        -not $environment.synthetic_gicp_smoke.passed
    ) {
        throw "내장 Small GICP 런타임 버전 또는 합성 GICP 결과가 올바르지 않습니다."
    }
    return $environment
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$resolvedProjectRoot = (Resolve-Path -LiteralPath $projectRoot).Path
$requirementsPath = Join-Path $PSScriptRoot "small_gicp_backend_requirements.txt"
$workerPath = Join-Path $resolvedProjectRoot "tools\align\alignment\icp_small_gicp_worker.py"
$packagePythonPath = if ([string]::IsNullOrWhiteSpace($PackagePython)) {
    Join-Path $resolvedProjectRoot ".venv-small-gicp\Scripts\python.exe"
}
else {
    [System.IO.Path]::GetFullPath($PackagePython)
}
$resolvedRuntimePath = if ([string]::IsNullOrWhiteSpace($RuntimePath)) {
    Join-Path $resolvedProjectRoot ".small-gicp-runtime\python"
}
else {
    [System.IO.Path]::GetFullPath($RuntimePath)
}
Assert-ChildPath -ParentPath $resolvedProjectRoot -ChildPath $resolvedRuntimePath

if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
    throw "Small GICP Requirements 파일을 찾을 수 없습니다: $requirementsPath"
}
if (-not (Test-Path -LiteralPath $workerPath -PathType Leaf)) {
    throw "Small GICP Worker를 찾을 수 없습니다: $workerPath"
}

$runtimePython = Join-Path $resolvedRuntimePath "python.exe"
if (-not $Force -and (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
    $verified = Test-EmbeddedRuntime -PythonExecutable $runtimePython -WorkerPath $workerPath
    Write-Host "기존 Small GICP 내장 런타임 검증 완료"
    Write-Host "Python: $($verified.worker_python_version) · NumPy: $($verified.worker_numpy_version) · small_gicp: $($verified.small_gicp_version)"
    return
}

if (-not (Test-Path -LiteralPath $packagePythonPath -PathType Leaf)) {
    throw "Small GICP Wheel 설치용 Python을 찾을 수 없습니다: $packagePythonPath"
}
Assert-PackagePython -PythonExecutable $packagePythonPath

$cacheRoot = Join-Path $resolvedProjectRoot ".small-gicp-runtime-cache"
$archivePath = Join-Path $cacheRoot $EmbeddedPythonArchiveName
if (-not (Test-Path -LiteralPath $cacheRoot)) {
    New-Item -ItemType Directory -Path $cacheRoot | Out-Null
}
if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
    Invoke-WebRequest -Uri $EmbeddedPythonUrl -OutFile $archivePath
}
$archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
if ($archiveHash -ne $EmbeddedPythonSha256) {
    throw "공식 CPython Embeddable ZIP의 SHA-256이 고정값과 다릅니다."
}

$buildRoot = Join-Path $resolvedProjectRoot ".small-gicp-runtime-build"
if (-not (Test-Path -LiteralPath $buildRoot)) {
    New-Item -ItemType Directory -Path $buildRoot | Out-Null
}
$buildId = [Guid]::NewGuid().ToString("N")
$stagingRoot = Join-Path $buildRoot $buildId
$stagingRuntime = Join-Path $stagingRoot "python"
Assert-ChildPath -ParentPath $buildRoot -ChildPath $stagingRoot

try {
    New-Item -ItemType Directory -Path $stagingRuntime -Force | Out-Null
    Expand-Archive -LiteralPath $archivePath -DestinationPath $stagingRuntime
    $sitePackages = Join-Path $stagingRuntime "Lib\site-packages"
    New-Item -ItemType Directory -Path $sitePackages -Force | Out-Null
    & $packagePythonPath -I -m pip install `
        --disable-pip-version-check `
        --require-virtualenv `
        --only-binary=:all: `
        --no-compile `
        --no-deps `
        --target $sitePackages `
        --requirement $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Small GICP 배포 패키지 설치에 실패했습니다."
    }

    $pthPath = Join-Path $stagingRuntime "python312._pth"
    [System.IO.File]::WriteAllText(
        $pthPath,
        "python312.zip`r`n.`r`nLib/site-packages`r`n",
        [System.Text.UTF8Encoding]::new($false)
    )
    Get-ChildItem -LiteralPath $stagingRuntime -Recurse -Directory -Filter "__pycache__" |
        Sort-Object FullName -Descending |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $stagingRuntime -Recurse -File |
        Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
        Remove-Item -Force

    $manifest = [ordered]@{
        schema_version = 1
        python_version = $EmbeddedPythonVersion
        python_archive = $EmbeddedPythonArchiveName
        python_archive_sha256 = $EmbeddedPythonSha256
        numpy_version = $NumPyVersion
        small_gicp_version = $SmallGicpVersion
        platform = "win-amd64"
    }
    $manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stagingRuntime "runtime_manifest.json") -Encoding UTF8
    $verified = Test-EmbeddedRuntime `
        -PythonExecutable (Join-Path $stagingRuntime "python.exe") `
        -WorkerPath $workerPath

    if (Test-Path -LiteralPath $resolvedRuntimePath) {
        Remove-VerifiedDirectory -ProjectRoot $resolvedProjectRoot -TargetPath $resolvedRuntimePath
    }
    $runtimeParent = Split-Path -Parent $resolvedRuntimePath
    if (-not (Test-Path -LiteralPath $runtimeParent)) {
        New-Item -ItemType Directory -Path $runtimeParent | Out-Null
    }
    Move-Item -LiteralPath $stagingRuntime -Destination $resolvedRuntimePath
    Write-Host "Small GICP 내장 런타임 준비 완료: $resolvedRuntimePath"
    Write-Host "Python: $($verified.worker_python_version) · NumPy: $($verified.worker_numpy_version) · small_gicp: $($verified.small_gicp_version)"
}
finally {
    Remove-VerifiedDirectory -ProjectRoot $resolvedProjectRoot -TargetPath $stagingRoot
}
