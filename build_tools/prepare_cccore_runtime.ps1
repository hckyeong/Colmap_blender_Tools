[CmdletBinding()]
param(
    [string]$EnvironmentRoot = "",
    [string]$RuntimePath = ""
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
$RuntimeBinaries = @('colmap_cccore_worker.exe','CCCoreLib.dll','tbb12.dll','msvcp140.dll','vcruntime140.dll','vcruntime140_1.dll','concrt140.dll')
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
    <# Manifest가 허용 파일과 배포 파일 해시를 빠짐없이 연결하는지 검사합니다. #>
    param([string]$Root)
    $manifestPath = Join-Path $Root 'runtime_manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.protocol_version -ne $Lock.protocol_version -or $manifest.tbb_version -ne $Lock.tbb.version) {
        throw "Runtime Manifest의 프로토콜 또는 oneTBB 버전이 잠금 정보와 다릅니다."
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
$allowedChangedFiles = @('CMakeLists.txt','src/RegistrationTools.cpp','include/DgmOctree.h','src/DgmOctree.cpp','src/DistanceComputationTools.cpp')
$changedFiles = @(& git -C $CoreSource diff --name-only HEAD)
foreach ($changedFile in $changedFiles) {
    if ($changedFile -notin $allowedChangedFiles) { throw "CCCoreLib 원문에 예상하지 않은 변경이 있습니다: $changedFile" }
}
if ((& git -C $CoreSource ls-files --others --exclude-standard)) { throw "CCCoreLib 캐시에 추적되지 않은 파일이 있습니다." }
$PatchFile = Join-Path $NativeSource $Lock.cccore.patch_file
Assert-Hash $PatchFile $Lock.cccore.patch_sha256
$RegistrationSource = Join-Path $CoreSource "src\RegistrationTools.cpp"
$registrationHash = Get-NormalizedSha256 $RegistrationSource
if ($registrationHash -eq $Lock.cccore.original_registration_normalized_sha256) {
    $normalized = [IO.File]::ReadAllText($RegistrationSource).Replace("`r`n", "`n")
    [IO.File]::WriteAllText($RegistrationSource, $normalized, [Text.UTF8Encoding]::new($false))
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', '--check', $PatchFile)
    Invoke-Checked git @('-C', $CoreSource, '-c', 'core.autocrlf=false', 'apply', $PatchFile)
}
Assert-Hash $RegistrationSource $Lock.cccore.patched_registration_sha256
$ConcurrencyPatch = $Lock.cccore.concurrency_patch
$ConcurrencyPatchFile = Join-Path $NativeSource $ConcurrencyPatch.patch_file
Assert-Hash $ConcurrencyPatchFile $ConcurrencyPatch.patch_sha256
$originalConcurrencyFiles = 0
$patchedConcurrencyFiles = 0
foreach ($source in $ConcurrencyPatch.files) {
    $sourcePath = Join-Path $CoreSource $source.path
    $sourceHash = Get-NormalizedSha256 $sourcePath
    if ($sourceHash -eq $source.original_normalized_sha256) { $originalConcurrencyFiles++ }
    elseif ($sourceHash -eq $source.patched_sha256) { $patchedConcurrencyFiles++ }
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
foreach ($source in $ConcurrencyPatch.files) { Assert-Hash (Join-Path $CoreSource $source.path) $source.patched_sha256 }
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
    "-DCORE_PATCH=$($Lock.cccore.patch_id)+$($ConcurrencyPatch.patch_id)")
Invoke-Checked $CMake @('--build',$BuildPath,'--config','Release','--parallel','8')
foreach ($binary in @('colmap_cccore_worker.exe','CCCoreLib.dll')) {
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
Copy-Item -LiteralPath (Join-Path $NativeSource 'main.cpp'), (Join-Path $NativeSource 'CMakeLists.txt'), (Join-Path $NativeSource 'dependencies.lock.json'), (Join-Path $NativeSource 'REBUILD.md') -Destination $PackagedNative -Force
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
$SourceArchive = Join-Path $StagedRuntime 'cccore_rebuild_sources.zip'
Compress-Archive -Path (Join-Path $SourcePackage '*') -DestinationPath $SourceArchive -Force
$SelfTestResult = ConvertTo-PortableSelfTestResult (Invoke-NativeEnvironmentCheck $StagedRuntime)
$TestPython = Join-Path $EnvironmentRoot 'runtime\python\python.exe'
if (!(Test-Path -LiteralPath $TestPython)) { throw "네이티브 계약 검사에 사용할 격리 Python이 없습니다." }
$oldWorker = $env:COLMAP_CCCORE_WORKER
try {
    $env:COLMAP_CCCORE_WORKER = Join-Path $StagedRuntime 'colmap_cccore_worker.exe'
    Invoke-Checked $TestPython @('-X','utf8','-I',(Join-Path $ProjectRoot 'tests\align\native\test_cccore_worker.py'),'--report',(Join-Path $StagedRuntime 'native_test_report.json'))
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
$Manifest = [ordered]@{
    protocol_version = $Lock.protocol_version
    platform = 'windows-x64'
    core_commit = $Lock.cccore.commit
    core_patch = "$($Lock.cccore.patch_id)+$($ConcurrencyPatch.patch_id)"
    core_patch_sha256 = $Lock.cccore.patch_sha256
    concurrency_patch = $ConcurrencyPatch.patch_id
    concurrency_patch_sha256 = $ConcurrencyPatch.patch_sha256
    patched_registration_sha256 = $Lock.cccore.patched_registration_sha256
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
