[CmdletBinding()]
param(
    [string]$BlenderPath = "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
    [string]$ReportDirectory = "",
    [string]$StartAt = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$reportRoot = if ($ReportDirectory) { [IO.Path]::GetFullPath($ReportDirectory) } else { Join-Path $projectRoot "build\align-source-tests" }
New-Item -ItemType Directory -Path $reportRoot -Force | Out-Null

# 실험 관리기에 있던 필수 Align 회귀 목록을 정식 소스 검증 경계로 이관합니다.
$scripts = @(
    "test_colmap_io.py", "test_mesh_extension.py", "test_embedded_obj.py",
    "test_embedded_obj_optimization.py", "test_mesh_normal_reuse.py", "test_duplicate_sources.py", "test_marker_sim3.py",
    "test_icp_direct_align.py", "test_icp_cccore_geometry.py", "test_icp_cccore_ui.py",
    "test_icp_cccore_runtime.py", "test_icp_cccore_persistence.py",
    "test_merge_export_transforms.py", "test_merge_export_writer.py", "test_merge_export_runtime.py",
    "test_project_persistence_codec.py", "test_project_persistence_store.py",
    "test_companion_obj_reader.py", "test_companion_import.py",
    "test_companion_display.py", "test_companion_persistence.py",
    "test_display_axes.py", "test_geometry_axes_display.py", "test_geometry_axes_persistence.py",
    "test_geometry_axes_runtime.py", "test_geometry_axes_ui.py", "test_geometry_axes_export.py",
    "test_marker_state.py", "test_marker_alignment_runtime.py", "test_marker_alignment_integration.py",
    "test_marker_preview_combined_clouds.py", "test_mesh_surface_picking.py",
    "test_project_restore.py", "test_project_restore_roundtrip.py",
    "test_marker_geometry_state.py", "test_marker_geometry_ui.py",
    "test_companion_marker_alignment.py", "test_companion_marker_picking.py",
    "test_companion_marker_persistence.py"
)
if ($StartAt) {
    $index = [array]::IndexOf($scripts, $StartAt)
    if ($index -lt 0) { throw "재개할 검사가 목록에 없습니다: $StartAt" }
    $scripts = $scripts[$index..($scripts.Count - 1)]
}
$results = @()
try {
    foreach ($script in $scripts) {
        $log = Join-Path $reportRoot ($script + ".log")
        & $BlenderPath --background --factory-startup --python-exit-code 1 --python (Join-Path $projectRoot "tests\align\blender\$script") *> $log
        $results += [ordered]@{ script = $script; exit_code = $LASTEXITCODE }
        if ($LASTEXITCODE -ne 0) { Get-Content -LiteralPath $log -Tail 18; throw "Align 소스 회귀 실패: $script" }
        Write-Output "$script 통과"
    }
}
finally {
    [ordered]@{ passed = ($results.Count -eq $scripts.Count -and @($results | Where-Object { $_.exit_code -ne 0 }).Count -eq 0); start_at = $StartAt; results = $results } |
        ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $reportRoot "report.json") -Encoding UTF8
}
