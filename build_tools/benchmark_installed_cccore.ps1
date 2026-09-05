[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BaselineRoot,
    [Parameter(Mandatory = $true)][string]$InputBlend,
    [Parameter(Mandatory = $true)][string]$ReportDirectory,
    [string]$PackagePath = "",
    [string]$BlenderPath = "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$reportRoot = [IO.Path]::GetFullPath($ReportDirectory)
if (Test-Path -LiteralPath $reportRoot) { throw "기존 측정 결과를 덮어쓰지 않습니다: $reportRoot" }
if (!$PackagePath) {
    $version = [regex]::Match((Get-Content -LiteralPath (Join-Path $projectRoot "blender_manifest.toml") -Raw), '(?m)^version\s*=\s*"([^"]+)"').Groups[1].Value
    $PackagePath = Join-Path $projectRoot "build\colmap_tools-$version.zip"
}
$PackagePath = [IO.Path]::GetFullPath($PackagePath)
$InputBlend = [IO.Path]::GetFullPath($InputBlend)
$BaselineRoot = [IO.Path]::GetFullPath($BaselineRoot)
foreach ($file in @($BlenderPath, $PackagePath, $InputBlend)) {
    if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "필요한 측정 입력이 없습니다: $file" }
}

# 사용자 Blender 설정과 원본 파일을 변경하지 않는 별도 설치 경로를 만듭니다.
$savedEnvironment = @{}
$environmentPaths = @{
    BLENDER_USER_CONFIG = "config"
    BLENDER_USER_SCRIPTS = "scripts"
    BLENDER_USER_EXTENSIONS = "extensions"
    BLENDER_USER_DATAFILES = "datafiles"
}
try {
    foreach ($name in $environmentPaths.Keys) {
        $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        $path = Join-Path $reportRoot ("blender_user\" + $environmentPaths[$name])
        New-Item -ItemType Directory -Path $path -Force | Out-Null
        [Environment]::SetEnvironmentVariable($name, $path, "Process")
    }
    $repository = Join-Path $reportRoot "blender_user\repository"
    New-Item -ItemType Directory -Path $repository -Force | Out-Null
    & $BlenderPath --background --command extension repo-add colmap_benchmark --name "COLMAP 검증" --directory $repository --clear-all *> (Join-Path $reportRoot "install.log")
    if ($LASTEXITCODE -ne 0) { throw "벤치마크 저장소 생성 실패" }
    & $BlenderPath --background --command extension install-file -r colmap_benchmark $PackagePath *>> (Join-Path $reportRoot "install.log")
    if ($LASTEXITCODE -ne 0) { throw "벤치마크 ZIP 설치 실패" }
    foreach ($scenario in @("aligned_auto31", "misaligned_auto31")) {
        foreach ($run in @("warmup", "run01", "run02", "run03")) {
            $output = Join-Path $reportRoot "$scenario\$run"
            $logPath = Join-Path $reportRoot "$scenario-$run.log"
            # 동일한 이전 보고서 조건을 모든 새 Blender 프로세스에 전달합니다.
            & $BlenderPath --background $InputBlend --python-exit-code 1 --python (Join-Path $projectRoot "tests\align\experiments\benchmark_installed_cccore.py") -- --baseline (Join-Path $BaselineRoot "$scenario\run01\result.json") --output $output --archive $PackagePath *> $logPath
            if ($LASTEXITCODE -ne 0) { Get-Content -LiteralPath $logPath -Tail 18; throw "실데이터 벤치마크 실패: $scenario / $run" }
            $result = Get-Content -LiteralPath (Join-Path $output "result.json") -Raw -Encoding UTF8 | ConvertFrom-Json
            if (!$result.passed) { throw "실데이터 결과 검증 실패: $scenario / $run" }
            Write-Output ("{0} / {1}: {2:N3}초" -f $scenario, $run, $result.total_alignment_seconds)
        }
    }
}
finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
    }
}
