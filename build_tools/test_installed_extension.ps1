[CmdletBinding()]
param(
    [string]$BlenderPath = "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
    [string]$PackagePath = "",
    [switch]$WorkspaceGui
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

# 격리 설치에 사용할 경로와 Build ZIP을 확정합니다.
$projectRoot = Split-Path -Parent $PSScriptRoot
$artifactRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot ".artifacts"))
$temporaryRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $artifactRoot ("installed_extension_" + [guid]::NewGuid().ToString("N")))
)
$artifactPrefix = $artifactRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $temporaryRoot.StartsWith($artifactPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "격리 설치 경로가 Artifact Root 밖에 있습니다: $temporaryRoot"
}
$manifestPath = Join-Path $projectRoot "blender_manifest.toml"
$manifestVersionMatch = [regex]::Match(
    (Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8),
    '(?m)^version\s*=\s*"(?<version>[^"]+)"\s*$'
)
if (-not $manifestVersionMatch.Success) {
    throw "Extension Manifest에서 Version을 읽지 못했습니다: $manifestPath"
}
$manifestVersion = $manifestVersionMatch.Groups["version"].Value
$resolvedPackage = if ([string]::IsNullOrWhiteSpace($PackagePath)) {
    Join-Path $projectRoot "build\colmap_tools-$manifestVersion.zip"
}
else {
    [System.IO.Path]::GetFullPath($PackagePath)
}
if (-not (Test-Path -LiteralPath $BlenderPath -PathType Leaf)) {
    throw "Blender 5.2 실행 파일을 찾을 수 없습니다: $BlenderPath"
}
if (-not (Test-Path -LiteralPath $resolvedPackage -PathType Leaf)) {
    throw "검사할 Extension ZIP을 찾을 수 없습니다: $resolvedPackage"
}

$configPath = Join-Path $temporaryRoot "config"
$scriptsPath = Join-Path $temporaryRoot "scripts"
$extensionsPath = Join-Path $temporaryRoot "extensions"
$dataFilesPath = Join-Path $temporaryRoot "datafiles"
$repositoryPath = Join-Path $temporaryRoot "repository"
New-Item -ItemType Directory -Path $configPath, $scriptsPath, $extensionsPath, $dataFilesPath, $repositoryPath -Force | Out-Null

# 기존 Process 환경을 보존한 채 Blender 사용자 영역만 임시 위치로 전환합니다.
$previousConfig = $env:BLENDER_USER_CONFIG
$previousScripts = $env:BLENDER_USER_SCRIPTS
$previousExtensions = $env:BLENDER_USER_EXTENSIONS
$previousDataFiles = $env:BLENDER_USER_DATAFILES
$previousRepository = $env:COLMAP_TOOLS_TEST_REPOSITORY
$previousInstallRoot = $env:COLMAP_TOOLS_EXPECTED_INSTALL_ROOT
$previousUseInstalled = $env:COLMAP_TOOLS_USE_INSTALLED
$previousInstalledModule = $env:COLMAP_TOOLS_INSTALLED_MODULE
$env:BLENDER_USER_CONFIG = $configPath
$env:BLENDER_USER_SCRIPTS = $scriptsPath
$env:BLENDER_USER_EXTENSIONS = $extensionsPath
$env:BLENDER_USER_DATAFILES = $dataFilesPath
$env:COLMAP_TOOLS_TEST_REPOSITORY = "colmap_test"
$env:COLMAP_TOOLS_EXPECTED_INSTALL_ROOT = $repositoryPath

try {
    & $BlenderPath --background --command extension repo-add colmap_test --name "COLMAP Tools Test" --directory $repositoryPath --clear-all
    if ($LASTEXITCODE -ne 0) {
        throw "격리 Extension Repository 생성에 실패했습니다."
    }
    # 설치 Process에서 즉시 활성화하지 않고 다음 검증 Process에서 활성화해 Windows 파일 잠금을 피합니다.
    & $BlenderPath --background --command extension install-file -r colmap_test $resolvedPackage
    if ($LASTEXITCODE -ne 0) {
        throw "COLMAP Tools ZIP 격리 설치에 실패했습니다."
    }
    & $BlenderPath --background --python (Join-Path $projectRoot "tests\blender_background\test_installed_extension.py")
    if ($LASTEXITCODE -ne 0) {
        throw "COLMAP Tools 격리 설치본 검증에 실패했습니다."
    }
    if ($WorkspaceGui) {
        # 같은 격리 Repository의 설치본으로 전체 Workspace 실행 순서 GUI 회귀를 실행합니다.
        $env:COLMAP_TOOLS_USE_INSTALLED = "1"
        $env:COLMAP_TOOLS_INSTALLED_MODULE = "bl_ext.colmap_test.colmap_tools"
        & $BlenderPath --python-use-system-env --python (Join-Path $projectRoot "tests\blender_gui\test_workspace_isolation.py")
        if ($LASTEXITCODE -ne 0) {
            throw "COLMAP Tools 격리 설치본 Workspace GUI 검증에 실패했습니다."
        }
        & $BlenderPath --python-use-system-env --python (Join-Path $projectRoot "tests\blender_gui\test_camera_marker_handoff.py")
        if ($LASTEXITCODE -ne 0) {
            throw "COLMAP Tools 격리 설치본 Camera Marker 교대 GUI 검증에 실패했습니다."
        }
    }
}
finally {
    $env:BLENDER_USER_CONFIG = $previousConfig
    $env:BLENDER_USER_SCRIPTS = $previousScripts
    $env:BLENDER_USER_EXTENSIONS = $previousExtensions
    $env:BLENDER_USER_DATAFILES = $previousDataFiles
    $env:COLMAP_TOOLS_TEST_REPOSITORY = $previousRepository
    $env:COLMAP_TOOLS_EXPECTED_INSTALL_ROOT = $previousInstallRoot
    $env:COLMAP_TOOLS_USE_INSTALLED = $previousUseInstalled
    $env:COLMAP_TOOLS_INSTALLED_MODULE = $previousInstalledModule
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
    if ((Test-Path -LiteralPath $artifactRoot -PathType Container) -and -not (Get-ChildItem -LiteralPath $artifactRoot -Force)) {
        Remove-Item -LiteralPath $artifactRoot -Force
    }
}

Write-Host "COLMAP Tools 격리 설치본 검증 완료"
