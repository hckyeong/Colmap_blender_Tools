Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# 전체 빌드를 실행하지 않고 실제 정리 함수와 경로 검증 함수만 읽어 검사합니다.
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$tokens = $null
$parseErrors = $null
$syntax = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $projectRoot "build_tools\build_extension.ps1"), [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count) { throw "빌드 스크립트 문법 검사에 실패했습니다: $parseErrors" }
foreach ($functionName in @("Assert-ChildPath", "Remove-PreviousInstallPackages")) {
    $definition = $syntax.Find({
        param($node)
        # 이름이 일치하는 함수 정의만 선택합니다.
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName
    }, $false)
    if ($null -eq $definition) { throw "검사할 함수를 찾지 못했습니다: $functionName" }
    . ([scriptblock]::Create($definition.Extent.Text))
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("colmap-install-check-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
try {
    $oldPackage = Join-Path $testRoot "colmap_tools-0.3.70.zip"
    Set-Content -LiteralPath $oldPackage -Value "이전 설치본"
    $blocked = $false
    try { Remove-PreviousInstallPackages -OutputPath $testRoot } catch { $blocked = $true }
    if (-not $blocked -or -not (Test-Path -LiteralPath $oldPackage)) { throw "새 ZIP 없이 이전 설치본을 삭제했습니다." }

    foreach ($name in @("colmap_tools.zip", "colmap_tools-smallgicp.zip", "cccore_rebuild_sources.zip", "python312.zip", "colmap_tools-user-backup.zip")) {
        Set-Content -LiteralPath (Join-Path $testRoot $name) -Value "보존 대상"
    }
    $nested = Join-Path $testRoot "nested"
    New-Item -ItemType Directory -Path $nested | Out-Null
    Set-Content -LiteralPath (Join-Path $nested "colmap_tools-0.3.60.zip") -Value "재귀 정리 금지"
    Remove-PreviousInstallPackages -OutputPath $testRoot
    if (Test-Path -LiteralPath $oldPackage) { throw "이전 버전 설치 ZIP이 남았습니다." }
    if (@(Get-ChildItem -LiteralPath $testRoot -File).Count -ne 5) { throw "설치본 외 ZIP을 삭제했습니다." }
    if (-not (Test-Path -LiteralPath (Join-Path $nested "colmap_tools-0.3.60.zip"))) { throw "출력 하위 폴더까지 삭제했습니다." }
    Remove-PreviousInstallPackages -OutputPath $testRoot
    Write-Output "설치 ZIP 정리 경계 검사 통과"
}
finally {
    $resolvedRoot = [IO.Path]::GetFullPath($testRoot)
    $temporaryPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolvedRoot.StartsWith($temporaryPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($resolvedRoot) -notmatch '^colmap-install-check-[a-f0-9]{32}$') {
        throw "검사 임시 폴더가 허용 범위 밖에 있습니다: $resolvedRoot"
    }
    Remove-Item -LiteralPath $resolvedRoot -Recurse -Force
}
