[CmdletBinding()]
param(
    [string]$EnvironmentRoot = "",
    [string]$RuntimePath = "",
    [string]$PythonPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$NativeSource = Join-Path $ProjectRoot "tools\align\native\cccore_worker"
$Lock = Get-Content -LiteralPath (Join-Path $NativeSource "dependencies.lock.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if (!$EnvironmentRoot) { $EnvironmentRoot = Split-Path -Parent $ProjectRoot }
$EnvironmentRoot = [IO.Path]::GetFullPath($EnvironmentRoot).TrimEnd('\')
if (!$RuntimePath) { $RuntimePath = Join-Path $EnvironmentRoot "runtime\cccore" }
$RuntimePath = [IO.Path]::GetFullPath($RuntimePath)
$Cache = Join-Path $EnvironmentRoot "cache"
$CoreSource = Join-Path $Cache "cccore-src"
$Dependencies = Join-Path $Cache "cccore-deps"
$StagingId = [Guid]::NewGuid().ToString('N')
$StagingRoot = Join-Path $Cache "cccore-stage-$StagingId"
$BuildPath = Join-Path $StagingRoot "build"
$SourcePackage = Join-Path $StagingRoot "source-package"
$RuntimeParent = Split-Path -Parent $RuntimePath
$RuntimeName = Split-Path -Leaf $RuntimePath
$StagedRuntime = Join-Path $RuntimeParent ".$RuntimeName.stage-$StagingId"
$RuntimeBackup = Join-Path $RuntimeParent ".$RuntimeName.backup-$StagingId"
$TbbArchive = Join-Path $Dependencies $Lock.tbb.archive_name
$TbbRoot = Join-Path $Dependencies $Lock.tbb.archive_root
$TbbCMake = Join-Path $TbbRoot $Lock.tbb.cmake_dir
$TbbRuntime = Join-Path $TbbRoot $Lock.tbb.runtime_file
$TbbLicense = Join-Path $TbbRoot $Lock.tbb.license_source
$TbbThirdParty = Join-Path $TbbRoot $Lock.tbb.third_party_source
$RuntimeBinaries = @('colmap_cccore_worker.exe','colmap_mesh_cleanup.dll','CCCoreLib.dll','tbb12.dll','msvcp140.dll','vcruntime140.dll','vcruntime140_1.dll','concrt140.dll')
$RuntimeLicenses = @('CCCoreLib-LGPL-2.0.txt','CCCoreLib-build-MIT.txt','nanoflann-BSD.txt','json-MIT.txt','oneTBB-Apache-2.0.txt','oneTBB-third-party-programs.txt','COLMAP-Tools-GPL.txt')
$ExpectedRuntimeFiles = @($RuntimeBinaries) + @($RuntimeLicenses | ForEach-Object { "licenses\$_" }) + @(
    'cccore_rebuild_sources.zip','native_test_report.json','REBUILD.md','THIRD_PARTY_NOTICES.md','runtime_manifest.json'
)

function Assert-ChildPath {
    <# 생성하거나 갱신할 경로가 명시한 격리 환경 내부인지 검사합니다. #>
    param([string]$Path)
    $resolved = [IO.Path]::GetFullPath($Path)
    if (!$resolved.StartsWith($EnvironmentRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "격리 환경 밖의 출력 경로입니다: $resolved"
    }
    $current = $resolved
    while ($current -and $current.Length -ge $EnvironmentRoot.Length) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "출력 경로에 재분석 지점이 있습니다: $current" }
        }
        $current = Split-Path -Parent $current
    }
}

function Get-NormalizedSha256 {
    <# 줄바꿈 차이를 제거한 UTF-8 소스의 고정 해시를 계산합니다. #>
    param([string]$Path)
    $content = [IO.File]::ReadAllText($Path).Replace("`r`n", "`n")
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($content)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return [Convert]::ToHexString($algorithm.ComputeHash($bytes)).ToLowerInvariant() }
    finally { $algorithm.Dispose() }
}

function Assert-Hash {
    <# 다운로드와 패치 파일이 잠금 파일의 SHA-256과 일치하는지 확인합니다. #>
    param([string]$Path, [string]$Expected)
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Expected) { throw "고정 SHA-256이 다른 파일입니다: $Path" }
}

function Get-PinnedSourceSha256 {
    <# 이전 패치가 바꾸지 않은 파일을 고정 커밋의 원문과 직접 대조합니다. #>
    param([string]$RelativePath)
    $start = [Diagnostics.ProcessStartInfo]::new('git')
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
    foreach ($argument in @('-C', $CoreSource, 'show', "$($Lock.cccore.commit):$RelativePath")) {
        $start.ArgumentList.Add($argument)
    }
    $process = [Diagnostics.Process]::Start($start)
    try {
        $content = $process.StandardOutput.ReadToEnd().Replace("`r`n", "`n")
        $errorText = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw "고정 커밋 원문을 읽지 못했습니다: $RelativePath ($errorText)" }
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try { return [Convert]::ToHexString($algorithm.ComputeHash([Text.UTF8Encoding]::new($false).GetBytes($content))).ToLowerInvariant() }
        finally { $algorithm.Dispose() }
    }
    finally { $process.Dispose() }
}

function Get-PinnedFile {
    <# 이미 검증 가능한 캐시를 재사용하거나 고정 출처 파일을 내려받습니다. #>
    param([string]$Url, [string]$Path, [string]$Sha256)
    if (!(Test-Path -LiteralPath $Path)) { Invoke-WebRequest -Uri $Url -OutFile $Path }
    Assert-Hash -Path $Path -Expected $Sha256
}

function Invoke-Checked {
    <# 외부 빌드 명령의 실패를 즉시 감지합니다. #>
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "외부 빌드 명령이 실패했습니다: $Program ($LASTEXITCODE)" }
}

function Copy-SourceTree {
    <# Git에 추적된 원문만 복사해 저장소 메타데이터 없는 재빌드 소스를 만듭니다. #>
    param([string]$Repository, [string]$Destination)
    Assert-ChildPath $Destination
    $files = & git -C $Repository ls-files
    if ($LASTEXITCODE -ne 0) { throw "재빌드 원문 목록을 읽지 못했습니다." }
    foreach ($relative in $files) {
        $sourceFile = Join-Path $Repository $relative
        if (!(Test-Path -LiteralPath $sourceFile -PathType Leaf)) { continue }
        $targetFile = Join-Path $Destination $relative
        Assert-ChildPath $targetFile
        New-Item -ItemType Directory -Path (Split-Path -Parent $targetFile) -Force | Out-Null
        Copy-Item -LiteralPath $sourceFile -Destination $targetFile -Force
    }
}

function Remove-SafeTree {
    <# 격리 환경 내부의 확인된 작업 디렉터리만 재귀적으로 정리합니다. #>
    param([string]$Path)
    Assert-ChildPath $Path
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
}

function Assert-ExactFileSet {
    <# 런타임에 허용한 상대 파일만 정확히 존재하는지 검사합니다. #>
    param([string]$Root, [string[]]$Expected)
    $actual = @(Get-ChildItem -LiteralPath $Root -Recurse -File | ForEach-Object {
        [IO.Path]::GetRelativePath($Root, $_.FullName).Replace('/', '\')
    } | Sort-Object)
    $expectedSorted = @($Expected | Sort-Object)
    $difference = @(Compare-Object -ReferenceObject $expectedSorted -DifferenceObject $actual)
    if ($difference.Count -ne 0) {
        throw "런타임 파일 허용 목록과 실제 파일이 다릅니다: $($difference | Out-String)"
    }
}

function Invoke-NativeEnvironmentCheck {
    <# 지정 런타임의 실제 DLL 경로와 oneTBB 병렬 계약을 실행해 확인합니다. #>
    param([string]$Root)
    $output = & (Join-Path $Root 'colmap_cccore_worker.exe') --check-environment
    if ($LASTEXITCODE -ne 0) { throw "실제 합성 등록 검사가 실패했습니다: $output" }
    $result = $output | ConvertFrom-Json
    if ($result.protocol_version -ne $Lock.protocol_version -or
        $result.thread_backend -ne 'oneTBB' -or
        $result.tbb_version -ne $Lock.tbb.version -or
        !$result.distance_multithread_support -or
        !$result.octree_multithread_support) {
        throw "oneTBB 병렬 환경 계약이 올바르지 않습니다: $output"
    }
    $loadedLibraries = [ordered]@{ library_path='CCCoreLib.dll'; tbb_library_path='tbb12.dll' }
    foreach ($property in $loadedLibraries.Keys) {
        $expectedPath = [IO.Path]::GetFullPath((Join-Path $Root $loadedLibraries[$property]))
        if (![string]::Equals([IO.Path]::GetFullPath($result.$property), $expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
            throw "런타임 밖의 공유 DLL이 로드되었습니다: $($result.$property)"
        }
    }
    return $result
}

function ConvertTo-PortableSelfTestResult {
    <# 검증 결과의 실제 DLL 경로를 배포 가능한 고정 상대 식별자로 바꿉니다. #>
    param([object]$Result)
    $portable = $Result | ConvertTo-Json -Depth 10 | ConvertFrom-Json
    $portable.library_path = 'CCCoreLib.dll'
    $portable.tbb_library_path = 'tbb12.dll'
    return $portable
}

function Assert-RuntimeManifest {
    <# Manifest가 메시 검사와 점평면까지의 원문·배포 해시를 연결하는지 검사합니다. #>
    param([string]$Root)
    $manifestPath = Join-Path $Root 'runtime_manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.protocol_version -ne $Lock.protocol_version -or $manifest.tbb_version -ne $Lock.tbb.version) {
        throw "Runtime Manifest의 프로토콜 또는 oneTBB 버전이 잠금 정보와 다릅니다."
    }
    $expectedCorePatch = "$($Lock.cccore.patch_id)+$($Lock.cccore.concurrency_patch.patch_id)+$($Lock.cccore.registration_safety_patch.id)+$($Lock.cccore.mesh_grid_memory_patch.id)+$($Lock.cccore.mesh_grid_reuse_patch.id)+$($Lock.cccore.mesh_grid_fixed_domain_patch.id)+$($Lock.cccore.icp_mesh_grid_reuse_patch.id)+$($Lock.cccore.mesh_bvh_search_patch.id)+$($Lock.cccore.point_to_plane_patch.id)"
    if ($manifest.core_patch -ne $expectedCorePatch -or $manifest.self_test.core_patch -ne $expectedCorePatch -or
        $manifest.registration_safety_patch -ne $Lock.cccore.registration_safety_patch.id -or
        $manifest.registration_safety_patch_sha256 -ne $Lock.cccore.registration_safety_patch.sha256 -or
        $manifest.patched_registration_sha256 -ne $Lock.cccore.registration_safety_patch.patched_sha256) {
        throw "Runtime Manifest의 등록 안전 패치 또는 최종 원문 해시가 잠금 정보와 다릅니다."
    }
    $gridPatch = $Lock.cccore.mesh_grid_memory_patch
    if ($manifest.mesh_grid_memory_patch -ne $gridPatch.id -or
        $manifest.mesh_grid_memory_patch_sha256 -ne $gridPatch.sha256) {
        throw "Runtime Manifest의 격자 메모리 패치가 잠금 정보와 다릅니다."
    }
    $gridSourceNames = @($manifest.mesh_grid_source_sha256.PSObject.Properties.Name | Sort-Object)
    $expectedGridNames = @($gridPatch.files | ForEach-Object { $_.path } | Sort-Object)
    if (@(Compare-Object -ReferenceObject $expectedGridNames -DifferenceObject $gridSourceNames).Count -ne 0) {
        throw "Runtime Manifest의 격자 메모리 원문 목록이 다릅니다."
    }
    foreach ($source in $gridPatch.files) {
        if ($manifest.mesh_grid_source_sha256.($source.path) -ne $source.patched_sha256) {
            throw "Runtime Manifest의 격자 메모리 최종 원문 해시가 다릅니다: $($source.path)"
        }
    }
    $reusePatch = $Lock.cccore.mesh_grid_reuse_patch
    if ($manifest.mesh_grid_reuse_patch -ne $reusePatch.id -or $manifest.mesh_grid_reuse_patch_sha256 -ne $reusePatch.sha256) {
        throw "Runtime Manifest의 격자 재사용 패치가 잠금 정보와 다릅니다."
    }
    $reuseNames = @($manifest.mesh_grid_reuse_source_sha256.PSObject.Properties.Name | Sort-Object)
    $expectedReuseNames = @($reusePatch.files | ForEach-Object { $_.path } | Sort-Object)
    if (@(Compare-Object -ReferenceObject $expectedReuseNames -DifferenceObject $reuseNames).Count -ne 0) {
        throw "Runtime Manifest의 격자 재사용 원문 목록이 다릅니다."
    }
    foreach ($source in $reusePatch.files) {
        if ($manifest.mesh_grid_reuse_source_sha256.($source.path) -ne $source.patched_sha256) {
            throw "Runtime Manifest의 격자 재사용 최종 원문 해시가 다릅니다."
        }
    }
    $domainPatch = $Lock.cccore.mesh_grid_fixed_domain_patch
    if ($manifest.mesh_grid_fixed_domain_patch -ne $domainPatch.id -or
        $manifest.mesh_grid_fixed_domain_patch_sha256 -ne $domainPatch.sha256) {
        throw "Runtime Manifest의 격자 고정 범위 패치가 잠금 정보와 다릅니다."
    }
    $domainNames = @($manifest.mesh_grid_fixed_domain_source_sha256.PSObject.Properties.Name | Sort-Object)
    $expectedDomainNames = @($domainPatch.files | ForEach-Object { $_.path } | Sort-Object)
    if (@(Compare-Object -ReferenceObject $expectedDomainNames -DifferenceObject $domainNames).Count -ne 0) {
        throw "Runtime Manifest의 격자 고정 범위 원문 목록이 다릅니다."
    }
    foreach ($source in $domainPatch.files) {
        if ($manifest.mesh_grid_fixed_domain_source_sha256.($source.path) -ne $source.patched_sha256) {
            throw "Runtime Manifest의 격자 고정 범위 최종 원문 해시가 다릅니다."
        }
    }
    $icpPatch = $Lock.cccore.icp_mesh_grid_reuse_patch
    if ($manifest.icp_mesh_grid_reuse_patch -ne $icpPatch.id -or
        $manifest.icp_mesh_grid_reuse_patch_sha256 -ne $icpPatch.sha256) {
        throw "Runtime Manifest의 ICP 내부 격자 패치가 잠금 정보와 다릅니다."
    }
    $icpNames = @($manifest.icp_mesh_grid_reuse_source_sha256.PSObject.Properties.Name | Sort-Object)
    $expectedIcpNames = @($icpPatch.files | ForEach-Object { $_.path } | Sort-Object)
    if (@(Compare-Object -ReferenceObject $expectedIcpNames -DifferenceObject $icpNames).Count -ne 0) {
        throw "Runtime Manifest의 ICP 내부 격자 원문 목록이 다릅니다."
    }
    foreach ($source in $icpPatch.files) {
        if ($manifest.icp_mesh_grid_reuse_source_sha256.($source.path) -ne $source.patched_sha256) {
            throw "Runtime Manifest의 ICP 내부 격자 최종 원문 해시가 다릅니다."
        }
    }
    $bvhPatch = $Lock.cccore.mesh_bvh_search_patch
    if ($manifest.mesh_bvh_search_patch -ne $bvhPatch.id -or
        $manifest.mesh_bvh_search_patch_sha256 -ne $bvhPatch.sha256) {
        throw "Runtime Manifest의 BVH 검색 패치가 잠금 정보와 다릅니다."
    }
    $bvhNames = @($manifest.mesh_bvh_search_source_sha256.PSObject.Properties.Name | Sort-Object)
    $expectedBvhNames = @($bvhPatch.files | ForEach-Object { $_.path } | Sort-Object)
    if (@(Compare-Object -ReferenceObject $expectedBvhNames -DifferenceObject $bvhNames).Count -ne 0) {
        throw "Runtime Manifest의 BVH 패치 원문 목록이 다릅니다."
    }
    foreach ($source in $bvhPatch.files) {
        if ($manifest.mesh_bvh_search_source_sha256.($source.path) -ne $source.patched_sha256) {
            throw "Runtime Manifest의 BVH 패치 최종 원문 해시가 다릅니다."
        }
    }
    $planePatch = $Lock.cccore.point_to_plane_patch
    if ($manifest.point_to_plane_patch -ne $planePatch.id -or
        $manifest.point_to_plane_patch_sha256 -ne $planePatch.sha256) {
        throw "Runtime Manifest의 점평면 패치가 잠금 정보와 다릅니다."
    }
    $planeNames = @($manifest.point_to_plane_source_sha256.PSObject.Properties.Name | Sort-Object)
    $expectedPlaneNames = @($planePatch.files | ForEach-Object { $_.path } | Sort-Object)
    if (@(Compare-Object -ReferenceObject $expectedPlaneNames -DifferenceObject $planeNames).Count -ne 0) {
        throw "Runtime Manifest의 점평면 원문 목록이 다릅니다."
    }
    foreach ($source in $planePatch.files) {
        if ($manifest.point_to_plane_source_sha256.($source.path) -ne $source.patched_sha256) {
            throw "Runtime Manifest의 점평면 최종 원문 해시가 다릅니다: $($source.path)"
        }
    }
    # 메시 검사 ABI와 실제 컴파일 원문을 같은 배포 단위로 확인합니다.
    if ($manifest.mesh_cleanup_abi -ne 1) { throw "Runtime Manifest의 메시 검사 ABI가 다릅니다." }
    $cleanupNames = @($manifest.mesh_cleanup_source_sha256.PSObject.Properties.Name | Sort-Object)
    if (@(Compare-Object -ReferenceObject @('mesh_cleanup.cpp', 'mesh_cleanup.h') -DifferenceObject $cleanupNames).Count -ne 0) {
        throw "Runtime Manifest의 메시 검사 원문 목록이 다릅니다."
    }
    foreach ($name in $cleanupNames) {
        Assert-Hash (Join-Path $NativeSource $name) $manifest.mesh_cleanup_source_sha256.$name
    }
    $bvhImplementationNames = @($manifest.bvh_source_sha256.PSObject.Properties.Name | Sort-Object)
    if (@(Compare-Object -ReferenceObject @('mesh_bvh.cpp', 'mesh_bvh.h') -DifferenceObject $bvhImplementationNames).Count -ne 0) {
        throw "Runtime Manifest의 BVH 구현 원문 목록이 다릅니다."
    }
    foreach ($name in $bvhImplementationNames) {
        Assert-Hash (Join-Path $NativeSource $name) $manifest.bvh_source_sha256.$name
    }
    $manifestFiles = @($manifest.runtime_files | ForEach-Object { [string]$_ } | Sort-Object)
    if (@(Compare-Object -ReferenceObject @($ExpectedRuntimeFiles | Sort-Object) -DifferenceObject $manifestFiles).Count -ne 0) {
        throw "Runtime Manifest의 파일 허용 목록이 빌드 계약과 다릅니다."
    }
    $binaryNames = @($manifest.binary_sha256.PSObject.Properties.Name | Sort-Object)
    if (@(Compare-Object -ReferenceObject @($RuntimeBinaries | Sort-Object) -DifferenceObject $binaryNames).Count -ne 0) {
        throw "Runtime Manifest의 바이너리 목록이 빌드 계약과 다릅니다."
    }
    foreach ($binary in $RuntimeBinaries) {
        Assert-Hash (Join-Path $Root $binary) $manifest.binary_sha256.$binary
    }
    $licenseNames = @($manifest.license_sha256.PSObject.Properties.Name | Sort-Object)
    if (@(Compare-Object -ReferenceObject @($RuntimeLicenses | Sort-Object) -DifferenceObject $licenseNames).Count -ne 0) {
        throw "Runtime Manifest의 라이선스 목록이 빌드 계약과 다릅니다."
    }
    foreach ($license in $RuntimeLicenses) {
        Assert-Hash (Join-Path $Root "licenses\$license") $manifest.license_sha256.$license
    }
    if ($manifest.source_archive -ne 'cccore_rebuild_sources.zip') { throw "Runtime Manifest의 원문 묶음 경로가 올바르지 않습니다." }
    Assert-Hash (Join-Path $Root $manifest.source_archive) $manifest.source_archive_sha256
    if ($manifest.native_test_report -ne 'native_test_report.json') { throw "Runtime Manifest의 네이티브 검사 경로가 올바르지 않습니다." }
    Assert-Hash (Join-Path $Root $manifest.native_test_report) $manifest.native_test_report_sha256
    $nativeReport = Get-Content -LiteralPath (Join-Path $Root $manifest.native_test_report) -Raw -Encoding UTF8 | ConvertFrom-Json
    if (!$nativeReport.succeeded -or $nativeReport.tests_run -lt 1) { throw "Runtime Manifest가 실패한 네이티브 검사 보고서를 가리킵니다." }
    $loadedLibraries = [ordered]@{ library_path='CCCoreLib.dll'; tbb_library_path='tbb12.dll' }
    foreach ($property in $loadedLibraries.Keys) {
        if (![string]::Equals([string]$manifest.self_test.$property, [string]$loadedLibraries[$property], [StringComparison]::Ordinal)) {
            throw "Runtime Manifest 자체 검사의 DLL 상대 식별자가 고정 계약과 다릅니다."
        }
    }
}

foreach ($directory in @($RuntimePath, $Cache, $CoreSource, $Dependencies, $StagingRoot, $BuildPath, $SourcePackage, $StagedRuntime, $RuntimeBackup)) { Assert-ChildPath $directory }
New-Item -ItemType Directory -Path $RuntimeParent, $Dependencies -Force | Out-Null
try {
New-Item -ItemType Directory -Path $StagingRoot, $StagedRuntime -Force | Out-Null
if (!(Test-Path -LiteralPath (Join-Path $CoreSource ".git"))) {
    if (Test-Path -LiteralPath $CoreSource) { throw "Git 출처를 확인할 수 없는 기존 코어 폴더입니다." }
    Invoke-Checked git @('-c', 'core.autocrlf=false', 'clone', '--no-checkout', $Lock.cccore.repository, $CoreSource)
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'checkout', '--detach', $Lock.cccore.commit)
}
$actualCommit = (& git -C $CoreSource rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $actualCommit -ne $Lock.cccore.commit) { throw "CCCoreLib 캐시의 커밋이 잠금 파일과 다릅니다." }
Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'submodule', 'update', '--init', '--recursive')
$NanoSource = Join-Path $CoreSource "extern\nanoflann"
$actualNanoCommit = (& git -C $NanoSource rev-parse HEAD).Trim()
if ($actualNanoCommit -ne $Lock.nanoflann.commit) { throw "nanoflann 커밋이 잠금 파일과 다릅니다." }
if ((& git -C $NanoSource status --porcelain)) { throw "nanoflann 원문에 예상하지 않은 변경이 있습니다." }
$allowedChangedFiles = @('CMakeLists.txt','src/RegistrationTools.cpp','include/RegistrationTools.h','include/DgmOctree.h','src/DgmOctree.cpp','src/DistanceComputationTools.cpp','include/DistanceComputationTools.h','include/GridAndMeshIntersection.h','src/GridAndMeshIntersection.cpp','include/Grid3D.h')
$changedFiles = @(& git -C $CoreSource diff --name-only HEAD)
foreach ($changedFile in $changedFiles) {
    if ($changedFile -notin $allowedChangedFiles) { throw "CCCoreLib 원문에 예상하지 않은 변경이 있습니다: $changedFile" }
}
if ((& git -C $CoreSource ls-files --others --exclude-standard)) { throw "CCCoreLib 캐시에 추적되지 않은 파일이 있습니다." }
# 완전히 검증된 마지막 패치만 되돌려 기존 패치 단계의 입력 해시를 순서대로 재검증합니다.
# 기존 캐시의 BVH 최종 패치를 먼저 해제하여 기존 일곱 패치의 검증을 그대로 유지합니다.
$PointToPlanePatch = $Lock.cccore.point_to_plane_patch
$PointToPlanePatchFile = Join-Path $NativeSource $PointToPlanePatch.file
Assert-Hash $PointToPlanePatchFile $PointToPlanePatch.sha256
$planePatchedCount = 0
foreach ($source in $PointToPlanePatch.files) {
    if ((Get-NormalizedSha256 (Join-Path $CoreSource $source.path)) -eq $source.patched_sha256) { $planePatchedCount++ }
}
if ($planePatchedCount -and $planePatchedCount -ne $PointToPlanePatch.files.Count) {
    throw "점평면 패치가 일부 파일에만 적용되어 있습니다."
}
if ($planePatchedCount -eq $PointToPlanePatch.files.Count) {
    foreach ($source in $PointToPlanePatch.files) {
        $sourcePath = Join-Path $CoreSource $source.path
        [IO.File]::WriteAllText($sourcePath, [IO.File]::ReadAllText($sourcePath).Replace("`r`n", "`n"), [Text.UTF8Encoding]::new($false))
    }
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--reverse', '--check', $PointToPlanePatchFile)
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--reverse', $PointToPlanePatchFile)
    foreach ($source in $PointToPlanePatch.files) { Assert-Hash (Join-Path $CoreSource $source.path) $source.original_sha256 }
}
$MeshBvhPatch = $Lock.cccore.mesh_bvh_search_patch
$MeshBvhPatchFile = Join-Path $NativeSource $MeshBvhPatch.file
Assert-Hash $MeshBvhPatchFile $MeshBvhPatch.sha256
$bvhPatchedCount = 0
foreach ($source in $MeshBvhPatch.files) {
    if ((Get-NormalizedSha256 (Join-Path $CoreSource $source.path)) -eq $source.patched_sha256) { $bvhPatchedCount++ }
}
if ($bvhPatchedCount -and $bvhPatchedCount -ne $MeshBvhPatch.files.Count) {
    throw "BVH 표면 검색 패치가 일부 파일에만 적용되어 있습니다."
}
if ($bvhPatchedCount -eq $MeshBvhPatch.files.Count) {
    foreach ($source in $MeshBvhPatch.files) {
        $sourcePath = Join-Path $CoreSource $source.path
        [IO.File]::WriteAllText($sourcePath, [IO.File]::ReadAllText($sourcePath).Replace("`r`n", "`n"), [Text.UTF8Encoding]::new($false))
    }
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--reverse', '--check', $MeshBvhPatchFile)
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--reverse', $MeshBvhPatchFile)
    foreach ($source in $MeshBvhPatch.files) { Assert-Hash (Join-Path $CoreSource $source.path) $source.original_sha256 }
}
$IcpMeshGridPatch = $Lock.cccore.icp_mesh_grid_reuse_patch
$IcpMeshGridPatchFile = Join-Path $NativeSource $IcpMeshGridPatch.file
Assert-Hash $IcpMeshGridPatchFile $IcpMeshGridPatch.sha256
$icpPatchedCount = 0
foreach ($source in $IcpMeshGridPatch.files) {
    if ((Get-NormalizedSha256 (Join-Path $CoreSource $source.path)) -eq $source.patched_sha256) { $icpPatchedCount++ }
}
if ($icpPatchedCount -and $icpPatchedCount -ne $IcpMeshGridPatch.files.Count) {
    throw "ICP 내부 격자 패치가 일부 파일에만 적용되어 있습니다."
}
if ($icpPatchedCount -eq $IcpMeshGridPatch.files.Count) {
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--reverse', '--check', $IcpMeshGridPatchFile)
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--reverse', $IcpMeshGridPatchFile)
    foreach ($source in $IcpMeshGridPatch.files) { Assert-Hash (Join-Path $CoreSource $source.path) $source.original_sha256 }
}
$PatchFile = Join-Path $NativeSource $Lock.cccore.patch_file
Assert-Hash $PatchFile $Lock.cccore.patch_sha256
$RegistrationSafetyPatch = $Lock.cccore.registration_safety_patch
$RegistrationSafetyPatchFile = Join-Path $NativeSource $RegistrationSafetyPatch.file
Assert-Hash $RegistrationSafetyPatchFile $RegistrationSafetyPatch.sha256
if ($RegistrationSafetyPatch.original_sha256 -ne $Lock.cccore.patched_registration_sha256) {
    throw "등록 안전 패치의 입력 해시가 기존 배율 패치의 결과와 다릅니다."
}
$RegistrationSource = Join-Path $CoreSource "src\RegistrationTools.cpp"
$registrationHash = Get-NormalizedSha256 $RegistrationSource
if ($registrationHash -notin @($Lock.cccore.original_registration_normalized_sha256,
    $RegistrationSafetyPatch.original_sha256, $RegistrationSafetyPatch.patched_sha256)) {
    throw "등록 원문이 원본·배율 패치 후·안전 패치 후의 검증 가능한 상태가 아닙니다."
}
$normalized = [IO.File]::ReadAllText($RegistrationSource).Replace("`r`n", "`n")
[IO.File]::WriteAllText($RegistrationSource, $normalized, [Text.UTF8Encoding]::new($false))
if ($registrationHash -eq $Lock.cccore.original_registration_normalized_sha256) {
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--check', $PatchFile)
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', $PatchFile)
    Assert-Hash $RegistrationSource $RegistrationSafetyPatch.original_sha256
}
if ((Get-NormalizedSha256 $RegistrationSource) -eq $RegistrationSafetyPatch.original_sha256) {
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--check', $RegistrationSafetyPatchFile)
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', $RegistrationSafetyPatchFile)
}
Assert-Hash $RegistrationSource $RegistrationSafetyPatch.patched_sha256
$ConcurrencyPatch = $Lock.cccore.concurrency_patch
$ConcurrencyPatchFile = Join-Path $NativeSource $ConcurrencyPatch.patch_file
Assert-Hash $ConcurrencyPatchFile $ConcurrencyPatch.patch_sha256
$MeshGridPatch = $Lock.cccore.mesh_grid_memory_patch
$MeshGridPatchFile = Join-Path $NativeSource $MeshGridPatch.file
Assert-Hash $MeshGridPatchFile $MeshGridPatch.sha256
$MeshGridSources = @{}
foreach ($source in $MeshGridPatch.files) {
    $MeshGridSources[$source.path] = $source
    $predecessor = @($ConcurrencyPatch.files | Where-Object { $_.path -eq $source.path })
    if ($predecessor.Count -ne 1 -or $predecessor[0].patched_sha256 -ne $source.original_sha256) {
        throw "격자 메모리 패치의 입력 원문이 동시성 패치 결과와 연결되지 않습니다: $($source.path)"
    }
}
$MeshGridReusePatch = $Lock.cccore.mesh_grid_reuse_patch
$MeshGridReusePatchFile = Join-Path $NativeSource $MeshGridReusePatch.file
Assert-Hash $MeshGridReusePatchFile $MeshGridReusePatch.sha256
$MeshGridReuseSources = @{}
foreach ($source in $MeshGridReusePatch.files) {
    $MeshGridReuseSources[$source.path] = $source
    if ($MeshGridSources.ContainsKey($source.path) -and
        $MeshGridSources[$source.path].patched_sha256 -ne $source.original_sha256) {
        throw "격자 재사용 패치의 입력 원문이 메모리 패치 결과와 연결되지 않습니다."
    }
}
$MeshGridDomainPatch = $Lock.cccore.mesh_grid_fixed_domain_patch
$MeshGridDomainPatchFile = Join-Path $NativeSource $MeshGridDomainPatch.file
Assert-Hash $MeshGridDomainPatchFile $MeshGridDomainPatch.sha256
$MeshGridDomainSources = @{}
foreach ($source in $MeshGridDomainPatch.files) {
    $MeshGridDomainSources[$source.path] = $source
    if ($MeshGridReuseSources.ContainsKey($source.path)) {
        if ($MeshGridReuseSources[$source.path].patched_sha256 -ne $source.original_sha256) {
            throw "격자 고정 범위 패치의 입력 원문이 재사용 패치 결과와 연결되지 않습니다."
        }
    }
    else {
        if ($source.original_commit -ne $Lock.cccore.commit -or
            (Get-PinnedSourceSha256 $source.path) -ne $source.original_sha256) {
            throw "격자 고정 범위 패치의 입력 원문이 고정 커밋과 다릅니다: $($source.path)"
        }
    }
}
$originalConcurrencyFiles = 0
$patchedConcurrencyFiles = 0
foreach ($source in $ConcurrencyPatch.files) {
    $sourcePath = Join-Path $CoreSource $source.path
    $sourceHash = Get-NormalizedSha256 $sourcePath
    if ($sourceHash -eq $source.original_normalized_sha256) { $originalConcurrencyFiles++ }
    elseif ($sourceHash -eq $source.patched_sha256) { $patchedConcurrencyFiles++ }
    elseif ($MeshGridSources.ContainsKey($source.path) -and $sourceHash -eq $MeshGridSources[$source.path].patched_sha256) {
        $patchedConcurrencyFiles++
    }
    elseif ($MeshGridReuseSources.ContainsKey($source.path) -and $sourceHash -eq $MeshGridReuseSources[$source.path].patched_sha256) {
        $patchedConcurrencyFiles++
    }
    elseif ($MeshGridDomainSources.ContainsKey($source.path) -and $sourceHash -eq $MeshGridDomainSources[$source.path].patched_sha256) {
        $patchedConcurrencyFiles++
    }
    else { throw "CCCoreLib 동시성 패치 원문 해시가 다릅니다: $($source.path)" }
}
if ($originalConcurrencyFiles -and $patchedConcurrencyFiles) { throw "CCCoreLib 동시성 패치가 일부 파일에만 적용되어 있습니다." }
foreach ($source in $ConcurrencyPatch.files) {
    $sourcePath = Join-Path $CoreSource $source.path
    $normalized = [IO.File]::ReadAllText($sourcePath).Replace("`r`n", "`n")
    [IO.File]::WriteAllText($sourcePath, $normalized, [Text.UTF8Encoding]::new($false))
}
if ($originalConcurrencyFiles -eq $ConcurrencyPatch.files.Count) {
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--check', $ConcurrencyPatchFile)
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', $ConcurrencyPatchFile)
}
# 후속 격자 패치가 적용된 캐시도 허용하되 각 단계의 원문 해시를 고정합니다.
foreach ($source in $ConcurrencyPatch.files) {
    $sourcePath = Join-Path $CoreSource $source.path
    if ($MeshGridSources.ContainsKey($source.path) -and
        (Get-NormalizedSha256 $sourcePath) -eq $MeshGridSources[$source.path].patched_sha256) { continue }
    if ($MeshGridReuseSources.ContainsKey($source.path) -and
        (Get-NormalizedSha256 $sourcePath) -eq $MeshGridReuseSources[$source.path].patched_sha256) { continue }
    if ($MeshGridDomainSources.ContainsKey($source.path) -and
        (Get-NormalizedSha256 $sourcePath) -eq $MeshGridDomainSources[$source.path].patched_sha256) { continue }
    Assert-Hash $sourcePath $source.patched_sha256
}
$originalGridFiles = 0
$patchedGridFiles = 0
foreach ($source in $MeshGridPatch.files) {
    $sourceHash = Get-NormalizedSha256 (Join-Path $CoreSource $source.path)
    if ($sourceHash -eq $source.original_sha256) { $originalGridFiles++ }
    elseif ($sourceHash -eq $source.patched_sha256) { $patchedGridFiles++ }
    elseif ($MeshGridReuseSources.ContainsKey($source.path) -and $sourceHash -eq $MeshGridReuseSources[$source.path].patched_sha256) { $patchedGridFiles++ }
    elseif ($MeshGridDomainSources.ContainsKey($source.path) -and $sourceHash -eq $MeshGridDomainSources[$source.path].patched_sha256) { $patchedGridFiles++ }
    else { throw "격자 메모리 패치 원문 해시가 다릅니다: $($source.path)" }
}
if ($originalGridFiles -and $patchedGridFiles) { throw "격자 메모리 패치가 일부 파일에만 적용되어 있습니다." }
if ($originalGridFiles -eq $MeshGridPatch.files.Count) {
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--check', $MeshGridPatchFile)
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', $MeshGridPatchFile)
}
foreach ($source in $MeshGridPatch.files) {
    $sourcePath = Join-Path $CoreSource $source.path
    if ($MeshGridReuseSources.ContainsKey($source.path) -and
        (Get-NormalizedSha256 $sourcePath) -eq $MeshGridReuseSources[$source.path].patched_sha256) { continue }
    if ($MeshGridDomainSources.ContainsKey($source.path) -and
        (Get-NormalizedSha256 $sourcePath) -eq $MeshGridDomainSources[$source.path].patched_sha256) { continue }
    Assert-Hash $sourcePath $source.patched_sha256
}
$originalReuseFiles = 0
$patchedReuseFiles = 0
foreach ($source in $MeshGridReusePatch.files) {
    $sourcePath = Join-Path $CoreSource $source.path
    $sourceHash = Get-NormalizedSha256 $sourcePath
    if ($sourceHash -eq $source.original_sha256) { $originalReuseFiles++ }
    elseif ($sourceHash -eq $source.patched_sha256) { $patchedReuseFiles++ }
    elseif ($MeshGridDomainSources.ContainsKey($source.path) -and $sourceHash -eq $MeshGridDomainSources[$source.path].patched_sha256) { $patchedReuseFiles++ }
    else { throw "격자 재사용 패치 원문 해시가 다릅니다: $($source.path)" }
    [IO.File]::WriteAllText($sourcePath, [IO.File]::ReadAllText($sourcePath).Replace("`r`n", "`n"), [Text.UTF8Encoding]::new($false))
}
if ($originalReuseFiles -and $patchedReuseFiles) { throw "격자 재사용 패치가 일부 파일에만 적용되어 있습니다." }
if ($originalReuseFiles -eq $MeshGridReusePatch.files.Count) {
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--check', $MeshGridReusePatchFile)
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', $MeshGridReusePatchFile)
}
foreach ($source in $MeshGridReusePatch.files) {
    $sourcePath = Join-Path $CoreSource $source.path
    if ($MeshGridDomainSources.ContainsKey($source.path) -and
        (Get-NormalizedSha256 $sourcePath) -eq $MeshGridDomainSources[$source.path].patched_sha256) { continue }
    Assert-Hash $sourcePath $source.patched_sha256
}
$originalDomainFiles = 0
$patchedDomainFiles = 0
foreach ($source in $MeshGridDomainPatch.files) {
    $sourcePath = Join-Path $CoreSource $source.path
    $sourceHash = Get-NormalizedSha256 $sourcePath
    if ($sourceHash -eq $source.original_sha256) { $originalDomainFiles++ }
    elseif ($sourceHash -eq $source.patched_sha256) { $patchedDomainFiles++ }
    else { throw "격자 고정 범위 패치 원문 해시가 다릅니다: $($source.path)" }
    [IO.File]::WriteAllText($sourcePath, [IO.File]::ReadAllText($sourcePath).Replace("`r`n", "`n"), [Text.UTF8Encoding]::new($false))
}
if ($originalDomainFiles -and $patchedDomainFiles) { throw "격자 고정 범위 패치가 일부 파일에만 적용되어 있습니다." }
if ($originalDomainFiles -eq $MeshGridDomainPatch.files.Count) {
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--check', $MeshGridDomainPatchFile)
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', $MeshGridDomainPatchFile)
}
foreach ($source in $MeshGridDomainPatch.files) { Assert-Hash (Join-Path $CoreSource $source.path) $source.patched_sha256 }
# 새 패치의 모든 입력을 직전 단계 또는 고정 커밋에 연결한 뒤 한 번에 적용합니다.
foreach ($source in $IcpMeshGridPatch.files) {
    $expectedInput = if ($source.path -eq 'src/RegistrationTools.cpp') { $RegistrationSafetyPatch.patched_sha256 }
        elseif ($MeshGridDomainSources.ContainsKey($source.path)) { $MeshGridDomainSources[$source.path].patched_sha256 }
        elseif ($MeshGridReuseSources.ContainsKey($source.path)) { $MeshGridReuseSources[$source.path].patched_sha256 }
        elseif ($MeshGridSources.ContainsKey($source.path)) { $MeshGridSources[$source.path].patched_sha256 }
        else { Get-PinnedSourceSha256 $source.path }
    if ($source.original_sha256 -ne $expectedInput) {
        throw "ICP 내부 격자 패치의 입력 원문이 이전 패치와 연결되지 않습니다: $($source.path)"
    }
    Assert-Hash (Join-Path $CoreSource $source.path) $source.original_sha256
}
Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--check', $IcpMeshGridPatchFile)
Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', $IcpMeshGridPatchFile)
foreach ($source in $IcpMeshGridPatch.files) { Assert-Hash (Join-Path $CoreSource $source.path) $source.patched_sha256 }
# BVH 콜백 패치는 기존 ICP 패치가 만든 두 등록 원문에만 연결합니다.
foreach ($source in $MeshBvhPatch.files) {
    $previous = @($IcpMeshGridPatch.files | Where-Object { $_.path -eq $source.path })
    if ($previous.Count -ne 1 -or $previous[0].patched_sha256 -ne $source.original_sha256) {
        throw "BVH 패치의 입력 원문이 ICP 패치와 연결되지 않습니다: $($source.path)"
    }
    Assert-Hash (Join-Path $CoreSource $source.path) $source.original_sha256
}
Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--check', $MeshBvhPatchFile)
Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', $MeshBvhPatchFile)
foreach ($source in $MeshBvhPatch.files) { Assert-Hash (Join-Path $CoreSource $source.path) $source.patched_sha256 }

# 점평면 후행 패치는 검증된 BVH 등록 원문에만 연결합니다.
foreach ($source in $PointToPlanePatch.files) {
    $previous = @($MeshBvhPatch.files | Where-Object { $_.path -eq $source.path })
    if ($previous.Count -ne 1 -or $previous[0].patched_sha256 -ne $source.original_sha256) {
        throw "점평면 패치의 입력 원문이 BVH 패치와 연결되지 않습니다: $($source.path)"
    }
    Assert-Hash (Join-Path $CoreSource $source.path) $source.original_sha256
}
Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--check', $PointToPlanePatchFile)
Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', $PointToPlanePatchFile)
foreach ($source in $PointToPlanePatch.files) { Assert-Hash (Join-Path $CoreSource $source.path) $source.patched_sha256 }

Get-PinnedFile $Lock.json.header_url (Join-Path $Dependencies 'json.hpp') $Lock.json.header_sha256
Get-PinnedFile $Lock.json.license_url (Join-Path $Dependencies 'json-LICENSE.MIT') $Lock.json.license_sha256
Get-PinnedFile $Lock.tbb.archive_url $TbbArchive $Lock.tbb.archive_sha256
if (Test-Path -LiteralPath $TbbRoot) {
    Assert-ChildPath $TbbRoot
    Remove-Item -LiteralPath $TbbRoot -Recurse -Force
}
Expand-Archive -LiteralPath $TbbArchive -DestinationPath $Dependencies -Force
if (!(Test-Path -LiteralPath (Join-Path $TbbCMake 'TBBConfig.cmake'))) { throw "oneTBB CMake Package를 찾지 못했습니다." }
Assert-Hash $TbbRuntime $Lock.tbb.runtime_sha256
Assert-Hash $TbbLicense $Lock.tbb.license_sha256
Assert-Hash $TbbThirdParty $Lock.tbb.third_party_sha256

$VsWhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (!(Test-Path -LiteralPath $VsWhere)) { throw "Visual Studio 검색 도구가 없습니다." }
$VisualStudio = (& $VsWhere -latest -products '*' -version '[17.0,18.0)' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath).Trim()
if (!$VisualStudio) { throw "Visual Studio 2022의 C++ x64 빌드 도구가 필요합니다." }
$CMake = Join-Path $VisualStudio "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
if (!(Test-Path -LiteralPath $CMake)) { throw "Visual Studio에 포함된 CMake를 찾지 못했습니다." }
Invoke-Checked $CMake @('-S',$NativeSource,'-B',$BuildPath,'-G','Visual Studio 17 2022','-A','x64',
    "-DCCCORE_SOURCE=$CoreSource","-DJSON_INCLUDE_DIR=$Dependencies","-DTBB_DIR=$TbbCMake","-DCORE_COMMIT=$($Lock.cccore.commit)",
    "-DCORE_PATCH=$($Lock.cccore.patch_id)+$($ConcurrencyPatch.patch_id)+$($RegistrationSafetyPatch.id)+$($MeshGridPatch.id)+$($MeshGridReusePatch.id)+$($MeshGridDomainPatch.id)+$($IcpMeshGridPatch.id)+$($MeshBvhPatch.id)+$($PointToPlanePatch.id)")
Invoke-Checked $CMake @('--build',$BuildPath,'--config','Release','--parallel','8')
foreach ($binary in @('colmap_cccore_worker.exe','colmap_mesh_cleanup.dll','CCCoreLib.dll')) {
    Copy-Item -LiteralPath (Join-Path $BuildPath "bin\$binary") -Destination (Join-Path $StagedRuntime $binary) -Force
}
Copy-Item -LiteralPath $TbbRuntime -Destination (Join-Path $StagedRuntime 'tbb12.dll') -Force
$RedistRoot = Join-Path $VisualStudio "VC\Redist\MSVC"
$RedistVersion = Get-ChildItem -LiteralPath $RedistRoot -Directory | Where-Object Name -Match '^14\.' | Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1
$CrtPath = Join-Path $RedistVersion.FullName "x64\Microsoft.VC143.CRT"
foreach ($library in @('msvcp140.dll','vcruntime140.dll','vcruntime140_1.dll','concrt140.dll')) {
    $librarySource = Join-Path $CrtPath $library
    if (!(Test-Path -LiteralPath $librarySource)) { throw "공유 C++ 런타임 파일이 없습니다: $librarySource" }
    Copy-Item -LiteralPath $librarySource -Destination (Join-Path $StagedRuntime $library) -Force
}

$Licenses = Join-Path $StagedRuntime "licenses"
New-Item -ItemType Directory -Path $Licenses -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $CoreSource 'LICENSE.txt') -Destination (Join-Path $Licenses 'CCCoreLib-LGPL-2.0.txt') -Force
Copy-Item -LiteralPath (Join-Path $CoreSource 'LICENSE-MIT.txt') -Destination (Join-Path $Licenses 'CCCoreLib-build-MIT.txt') -Force
Copy-Item -LiteralPath (Join-Path $NanoSource 'COPYING') -Destination (Join-Path $Licenses 'nanoflann-BSD.txt') -Force
Copy-Item -LiteralPath (Join-Path $Dependencies 'json-LICENSE.MIT') -Destination (Join-Path $Licenses 'json-MIT.txt') -Force
Copy-Item -LiteralPath $TbbLicense -Destination (Join-Path $StagedRuntime $Lock.tbb.license_file) -Force
Copy-Item -LiteralPath $TbbThirdParty -Destination (Join-Path $StagedRuntime $Lock.tbb.third_party_file) -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'LICENSE') -Destination (Join-Path $Licenses 'COLMAP-Tools-GPL.txt') -Force
Copy-Item -LiteralPath (Join-Path $NativeSource 'THIRD_PARTY_NOTICES.md') -Destination $StagedRuntime -Force
Copy-Item -LiteralPath (Join-Path $NativeSource 'REBUILD.md') -Destination $StagedRuntime -Force
Copy-SourceTree $CoreSource (Join-Path $SourcePackage 'cccore-src')
Copy-SourceTree $NanoSource (Join-Path $SourcePackage 'cccore-src\extern\nanoflann')
$PackagedNative = Join-Path $SourcePackage 'native'
New-Item -ItemType Directory -Path $PackagedNative, (Join-Path $SourcePackage 'json') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $NativeSource 'main.cpp'), (Join-Path $NativeSource 'mesh_bvh.h'), (Join-Path $NativeSource 'mesh_bvh.cpp'), (Join-Path $NativeSource 'CMakeLists.txt'), (Join-Path $NativeSource 'dependencies.lock.json'), (Join-Path $NativeSource 'REBUILD.md') -Destination $PackagedNative -Force
Copy-Item -LiteralPath (Join-Path $NativeSource 'mesh_cleanup.h'), (Join-Path $NativeSource 'mesh_cleanup.cpp') -Destination $PackagedNative -Force
Copy-Item -LiteralPath (Join-Path $NativeSource 'patches') -Destination $PackagedNative -Recurse -Force
Copy-Item -LiteralPath (Join-Path $Dependencies 'json.hpp'), (Join-Path $Dependencies 'json-LICENSE.MIT') -Destination (Join-Path $SourcePackage 'json') -Force
$PackagedTbb = Join-Path $SourcePackage 'tbb'
if (Test-Path -LiteralPath $PackagedTbb) {
    Assert-ChildPath $PackagedTbb
    Remove-Item -LiteralPath $PackagedTbb -Recurse -Force
}
New-Item -ItemType Directory -Path $PackagedTbb -Force | Out-Null
Copy-Item -LiteralPath $TbbArchive -Destination (Join-Path $PackagedTbb $Lock.tbb.archive_name) -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'tests\align\native\test_cccore_worker.py') -Destination $SourcePackage -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'tests\align\native\test_point_to_plane.py') -Destination $SourcePackage -Force
# Python 회귀와 선택적 C++ 시험이 같은 고정 입력을 원문 묶음에서 읽도록 보존합니다.
$NativeTests = Join-Path $ProjectRoot 'tests\align\native'
$PackagedTests = Join-Path $SourcePackage 'tests\align\native'
$PackagedFixtures = Join-Path $SourcePackage 'fixtures'
New-Item -ItemType Directory -Path $PackagedTests, $PackagedFixtures -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $NativeTests 'test_mesh_bvh.cpp'), (Join-Path $NativeTests 'test_bvh_icp.cpp') -Destination $PackagedTests -Force
Copy-Item -LiteralPath (Join-Path $NativeTests 'fixtures\bvh_tetra_surface.json') -Destination $PackagedFixtures -Force
# 메시 검사 비교 시험에 필요한 순수 파이썬 원문을 동일한 상대 경로로 보존합니다.
$PackagedAlignment = Join-Path $SourcePackage 'tools\align\alignment'
New-Item -ItemType Directory -Path $PackagedAlignment -Force | Out-Null
foreach ($name in @('icp_cccore_solver.py', 'icp_cccore_contract.py', 'icp_cccore_overlap.py', 'icp_mesh_cleanup.py', 'icp_mesh_cleanup_native.py')) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "tools\align\alignment\$name") -Destination $PackagedAlignment -Force
}
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'tests\align\test_icp_mesh_cleanup_native.py') -Destination (Join-Path $SourcePackage 'tests\align') -Force

$SourceArchive = Join-Path $StagedRuntime 'cccore_rebuild_sources.zip'
Compress-Archive -Path (Join-Path $SourcePackage '*') -DestinationPath $SourceArchive -Force
$SelfTestResult = ConvertTo-PortableSelfTestResult (Invoke-NativeEnvironmentCheck $StagedRuntime)
$TestPython = if ($PythonPath) { [IO.Path]::GetFullPath($PythonPath) } else { Join-Path $EnvironmentRoot 'runtime\python\python.exe' }
if (!(Test-Path -LiteralPath $TestPython)) { throw "네이티브 계약 검사에 사용할 격리 Python이 없습니다." }
$oldWorker = $env:COLMAP_CCCORE_WORKER
try {
    $env:COLMAP_CCCORE_WORKER = Join-Path $StagedRuntime 'colmap_cccore_worker.exe'
    Invoke-Checked $TestPython @('-X','utf8','-I',(Join-Path $ProjectRoot 'tests\align\native\test_cccore_worker.py'),'--report',(Join-Path $StagedRuntime 'native_test_report.json'))
    Invoke-Checked $TestPython @('-X','utf8',(Join-Path $ProjectRoot 'tests\align\native\test_point_to_plane.py'))
}
finally { $env:COLMAP_CCCORE_WORKER = $oldWorker }
# 배포 보고서가 폐기되는 staging 경로 대신 런타임 내부 상대 경로를 기록하게 합니다.
$NativeTestReportPath = Join-Path $StagedRuntime 'native_test_report.json'
$NativeTestReport = Get-Content -LiteralPath $NativeTestReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
$NativeTestReport.worker = 'colmap_cccore_worker.exe'
$NativeTestReport | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $NativeTestReportPath -Encoding utf8
$Binaries = [ordered]@{}
foreach ($binary in $RuntimeBinaries) {
    $Binaries[$binary] = (Get-FileHash -LiteralPath (Join-Path $StagedRuntime $binary) -Algorithm SHA256).Hash.ToLowerInvariant()
}
$LicenseHashes = [ordered]@{}
foreach ($license in $RuntimeLicenses) {
    $LicenseHashes[$license] = (Get-FileHash -LiteralPath (Join-Path $StagedRuntime "licenses\$license") -Algorithm SHA256).Hash.ToLowerInvariant()
}
$MeshGridSourceHashes = [ordered]@{}
foreach ($source in $MeshGridPatch.files) { $MeshGridSourceHashes[$source.path] = $source.patched_sha256 }
$MeshGridReuseSourceHashes = [ordered]@{}
foreach ($source in $MeshGridReusePatch.files) { $MeshGridReuseSourceHashes[$source.path] = $source.patched_sha256 }
$MeshGridDomainSourceHashes = [ordered]@{}
foreach ($source in $MeshGridDomainPatch.files) { $MeshGridDomainSourceHashes[$source.path] = $source.patched_sha256 }
$IcpMeshGridSourceHashes = [ordered]@{}
foreach ($source in $IcpMeshGridPatch.files) { $IcpMeshGridSourceHashes[$source.path] = $source.patched_sha256 }
$MeshBvhSourceHashes = [ordered]@{}
foreach ($source in $MeshBvhPatch.files) { $MeshBvhSourceHashes[$source.path] = $source.patched_sha256 }
$PointToPlaneSourceHashes = [ordered]@{}
foreach ($source in $PointToPlanePatch.files) { $PointToPlaneSourceHashes[$source.path] = $source.patched_sha256 }
$BvhImplementationHashes = [ordered]@{}
foreach ($name in @('mesh_bvh.h', 'mesh_bvh.cpp')) {
    $BvhImplementationHashes[$name] = (Get-FileHash -LiteralPath (Join-Path $NativeSource $name) -Algorithm SHA256).Hash.ToLowerInvariant()
}
$Manifest = [ordered]@{
    mesh_cleanup_abi = 1
    mesh_cleanup_source_sha256 = [ordered]@{
        'mesh_cleanup.h' = (Get-FileHash -LiteralPath (Join-Path $NativeSource 'mesh_cleanup.h') -Algorithm SHA256).Hash.ToLowerInvariant()
        'mesh_cleanup.cpp' = (Get-FileHash -LiteralPath (Join-Path $NativeSource 'mesh_cleanup.cpp') -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    protocol_version = $Lock.protocol_version
    platform = 'windows-x64'
    core_commit = $Lock.cccore.commit
    core_patch = "$($Lock.cccore.patch_id)+$($ConcurrencyPatch.patch_id)+$($RegistrationSafetyPatch.id)+$($MeshGridPatch.id)+$($MeshGridReusePatch.id)+$($MeshGridDomainPatch.id)+$($IcpMeshGridPatch.id)+$($MeshBvhPatch.id)+$($PointToPlanePatch.id)"
    core_patch_sha256 = $Lock.cccore.patch_sha256
    concurrency_patch = $ConcurrencyPatch.patch_id
    concurrency_patch_sha256 = $ConcurrencyPatch.patch_sha256
    registration_safety_patch = $RegistrationSafetyPatch.id
    registration_safety_patch_sha256 = $RegistrationSafetyPatch.sha256
    patched_registration_sha256 = $RegistrationSafetyPatch.patched_sha256
    mesh_grid_memory_patch = $MeshGridPatch.id
    mesh_grid_memory_patch_sha256 = $MeshGridPatch.sha256
    mesh_grid_source_sha256 = $MeshGridSourceHashes
    mesh_grid_reuse_patch = $MeshGridReusePatch.id
    mesh_grid_reuse_patch_sha256 = $MeshGridReusePatch.sha256
    mesh_grid_reuse_source_sha256 = $MeshGridReuseSourceHashes
    mesh_grid_fixed_domain_patch = $MeshGridDomainPatch.id
    mesh_grid_fixed_domain_patch_sha256 = $MeshGridDomainPatch.sha256
    mesh_grid_fixed_domain_source_sha256 = $MeshGridDomainSourceHashes
    icp_mesh_grid_reuse_patch = $IcpMeshGridPatch.id
    icp_mesh_grid_reuse_patch_sha256 = $IcpMeshGridPatch.sha256
    icp_mesh_grid_reuse_source_sha256 = $IcpMeshGridSourceHashes
    mesh_bvh_search_patch = $MeshBvhPatch.id
    mesh_bvh_search_patch_sha256 = $MeshBvhPatch.sha256
    mesh_bvh_search_source_sha256 = $MeshBvhSourceHashes
    point_to_plane_patch = $PointToPlanePatch.id
    point_to_plane_patch_sha256 = $PointToPlanePatch.sha256
    point_to_plane_source_sha256 = $PointToPlaneSourceHashes
    bvh_source_sha256 = $BvhImplementationHashes
    dependencies = $Lock
    build_options = @{ CCCORELIB_SHARED=$true; CCCORELIB_USE_QT_CONCURRENT=$false; CCCORELIB_USE_TBB=$true; CCCORELIB_USE_CGAL=$false; configuration='Release' }
    cmake_version = ((& $CMake --version) | Select-Object -First 1)
    visual_studio_path = $VisualStudio
    visual_studio_redist_version = $RedistVersion.Name
    thread_backend = $SelfTestResult.thread_backend
    tbb_version = $SelfTestResult.tbb_version
    requested_max_threads = $SelfTestResult.requested_max_threads
    effective_max_threads = $SelfTestResult.effective_max_threads
    hardware_thread_count = $SelfTestResult.hardware_thread_count
    distance_multithread_support = $SelfTestResult.distance_multithread_support
    octree_multithread_support = $SelfTestResult.octree_multithread_support
    msvc_parallel_sort_max_concurrency = 'effective_max_threads'
    coordinate_type = 'float32'
    matrix_scalar_type = 'float64'
    binary_sha256 = $Binaries
    license_sha256 = $LicenseHashes
    worker_source_sha256 = (Get-FileHash -LiteralPath (Join-Path $NativeSource 'main.cpp') -Algorithm SHA256).Hash.ToLowerInvariant()
    cmake_source_sha256 = (Get-FileHash -LiteralPath (Join-Path $NativeSource 'CMakeLists.txt') -Algorithm SHA256).Hash.ToLowerInvariant()
    source_archive = 'cccore_rebuild_sources.zip'
    source_archive_sha256 = (Get-FileHash -LiteralPath $SourceArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    self_test = $SelfTestResult
    native_test_report = 'native_test_report.json'
    native_test_report_sha256 = (Get-FileHash -LiteralPath (Join-Path $StagedRuntime 'native_test_report.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    runtime_files = $ExpectedRuntimeFiles
    rebuild_command = '.\build_tools\prepare_cccore_runtime.ps1 -EnvironmentRoot <격리환경절대경로>'
    created_utc = [DateTime]::UtcNow.ToString('o')
}
$Manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $StagedRuntime 'runtime_manifest.json') -Encoding utf8
Assert-ExactFileSet $StagedRuntime $ExpectedRuntimeFiles
Assert-RuntimeManifest $StagedRuntime
$oldCleanupWorker = $env:COLMAP_CCCORE_WORKER
try {
    $env:COLMAP_CCCORE_WORKER = Join-Path $StagedRuntime 'colmap_cccore_worker.exe'
    Invoke-Checked $TestPython @('-X','utf8',(Join-Path $ProjectRoot 'tests\align\test_icp_mesh_cleanup_native.py'))
}
finally { $env:COLMAP_CCCORE_WORKER = $oldCleanupWorker }

$hadRuntime = Test-Path -LiteralPath $RuntimePath
$stagePublished = $false
try {
    if ($hadRuntime) {
        Rename-Item -LiteralPath $RuntimePath -NewName (Split-Path -Leaf $RuntimeBackup)
    }
    Rename-Item -LiteralPath $StagedRuntime -NewName $RuntimeName
    $stagePublished = $true
    $FinalSelfTestResult = ConvertTo-PortableSelfTestResult (Invoke-NativeEnvironmentCheck $RuntimePath)
    $Manifest.self_test = $FinalSelfTestResult
    $Manifest.thread_backend = $FinalSelfTestResult.thread_backend
    $Manifest.tbb_version = $FinalSelfTestResult.tbb_version
    $Manifest.requested_max_threads = $FinalSelfTestResult.requested_max_threads
    $Manifest.effective_max_threads = $FinalSelfTestResult.effective_max_threads
    $Manifest.hardware_thread_count = $FinalSelfTestResult.hardware_thread_count
    $Manifest.distance_multithread_support = $FinalSelfTestResult.distance_multithread_support
    $Manifest.octree_multithread_support = $FinalSelfTestResult.octree_multithread_support
    $Manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $RuntimePath 'runtime_manifest.json') -Encoding utf8
    Assert-ExactFileSet $RuntimePath $ExpectedRuntimeFiles
    Assert-RuntimeManifest $RuntimePath
}
catch {
    $publishError = $_
    try {
        if ($stagePublished -and (Test-Path -LiteralPath $RuntimePath)) { Remove-SafeTree $RuntimePath }
        if (Test-Path -LiteralPath $RuntimeBackup) {
            if (Test-Path -LiteralPath $RuntimePath) { throw "기존 런타임과 백업이 함께 남아 자동 롤백할 수 없습니다." }
            Rename-Item -LiteralPath $RuntimeBackup -NewName $RuntimeName
        }
    }
    catch {
        throw "런타임 교체 실패 후 롤백도 실패했습니다. 보존 백업을 확인하십시오: $RuntimeBackup"
    }
    throw $publishError
}
if (Test-Path -LiteralPath $RuntimeBackup) { Remove-SafeTree $RuntimeBackup }
Write-Output "CCCoreLib 독립 런타임 준비 완료: $RuntimePath"
}
finally {
    if (Test-Path -LiteralPath $StagedRuntime) { Remove-SafeTree $StagedRuntime }
    if (Test-Path -LiteralPath $StagingRoot) { Remove-SafeTree $StagingRoot }
}
