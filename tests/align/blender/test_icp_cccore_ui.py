"""자동 CC 정렬의 형상 차단·화면 지표·실행 경로·환경 검사 연결을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import patch

import bpy


def _add_test_paths() -> None:
    """실험 소스와 형상 회귀 자료를 Blender 탐색 경로에 연결합니다."""
    folder = Path(__file__).resolve().parent
    for path in (folder.parents[2], folder):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _selected_runtime():
    """직접 정렬이 가능한 실제 A/P 다중 선택 런타임을 구성합니다."""
    from test_icp_cccore_geometry import _runtime

    runtime = _runtime()
    runtime.session_active = True
    assert runtime.model_catalog.toggle_alignment_selection("기준", has_marker_draft=False, completed_pair_count=0)
    return runtime


def test_defaults_and_legacy_profiles() -> None:
    """정식·개발 기본은 CC이고 명시적 구엔진 비교 프로필만 Small GICP인지 확인합니다."""
    from tools.align import distribution_profile
    from tools.align.alignment.icp_solver_backend import backend_algorithm_version

    assert backend_algorithm_version("CCCORELIB") == "cccore-icp-v3"
    for profile in ("CCCORELIB", "SMALL_GICP", "FULL"):
        with patch.object(distribution_profile, "DISTRIBUTION_PROFILE", profile):
            expected = "SMALL_GICP" if profile == "SMALL_GICP" else "CCCORELIB"
            assert distribution_profile.default_icp_backend() == expected
            items = tuple(item[0] for item in distribution_profile.icp_backend_enum_items())
            if profile != "FULL":
                assert items == (expected,)
            else:
                assert "CCCORELIB" in items and "SMALL_GICP" in items


def test_explicit_small_gicp_profile_rejects_surface_input() -> None:
    """비교판은 점군을 구엔진으로 전달하고 표면 입력을 다른 데이터로 바꾸지 않고 차단합니다."""
    from tools.align.operators import marker_alignment
    from test_icp_cccore_geometry import _surface

    runtime = _selected_runtime()
    with patch.object(marker_alignment, "RUNTIME", runtime), \
         patch.object(marker_alignment, "_AUTOMATIC_ICP_BACKEND", "SMALL_GICP"), \
         patch.object(marker_alignment, "restore_input_block_reason", return_value=None), \
         patch.object(marker_alignment, "begin_icp_job") as begin, \
         patch.object(marker_alignment, "_register_icp_poll_timer"), \
         patch.object(marker_alignment, "request_redraw"), \
         patch.object(marker_alignment, "_report_status") as report, \
         patch.object(marker_alignment, "configured_icp_python_path", return_value="비교/python.exe"):
        assert marker_alignment.direct_align_start_block_reason() is None
        assert marker_alignment.COLMAPALIGN_OT_align_marker_preview.execute(SimpleNamespace(), SimpleNamespace()) == {"FINISHED"}
        assert begin.call_args.kwargs["solver_backend"] == "SMALL_GICP"
        assert "Small GICP" in report.call_args.args[2] and "CC 강체" not in report.call_args.args[2]
        _surface(runtime, "이동")
        assert "희소 점군만" in marker_alignment.align_start_block_reason(SimpleNamespace())


def test_rna_defaults_and_scale_descriptions() -> None:
    """실제 Blender 속성 등록에서 CC 기본값과 두 배율 단계의 설명을 확인합니다."""
    from tools.align.properties import COLMAPALIGN_PG_settings
    from tools.align.operators.marker_alignment import COLMAPALIGN_OT_align_marker_preview

    bpy.utils.register_class(COLMAPALIGN_PG_settings)
    try:
        properties = COLMAPALIGN_PG_settings.bl_rna.properties
        assert properties["icp_solver_backend"].default == "CCCORELIB"
        assert "초기 정렬" in properties["marker_use_uniform_scale"].description
        assert "별도" in properties["marker_use_uniform_scale"].description
        assert "±10%" in COLMAPALIGN_OT_align_marker_preview.bl_description
    finally:
        bpy.utils.unregister_class(COLMAPALIGN_PG_settings)


def test_cpu_thread_preferences_auto_manual_and_snapshot() -> None:
    """자동·수동 상한과 실행 매개변수 지문이 작업 시작 값으로 고정되는지 확인합니다."""
    from tools.align import properties
    from tools.align.alignment.icp_cccore_runtime import CcCoreRuntimeParameters

    bpy.utils.register_class(properties.COLMAPALIGN_AP_preferences)
    try:
        thread_property = properties.COLMAPALIGN_AP_preferences.bl_rna.properties["cc_cpu_thread_limit"]
        assert thread_property.default == 0
        assert thread_property.hard_min == 0 and thread_property.hard_max == 1024
    finally:
        bpy.utils.unregister_class(properties.COLMAPALIGN_AP_preferences)
    with patch.object(properties.os, "cpu_count", return_value=16):
        assert properties.cc_cpu_thread_counts(0) == (16, 15, 15)
        assert properties.cc_cpu_thread_counts(1) == (16, 15, 1)
        assert properties.cc_cpu_thread_counts(32) == (16, 15, 16)
        with patch.object(
            properties,
            "addon_preferences",
            return_value=SimpleNamespace(cc_cpu_thread_limit=32),
        ):
            assert properties.configured_cc_cpu_thread_limit() == 16
    with patch.object(properties.os, "cpu_count", return_value=None):
        assert properties.cc_cpu_thread_counts(0) == (1, 1, 1)
    assert properties.cc_cpu_thread_counts(0, 1025) == (1025, 1024, 1024)
    assert properties.cc_cpu_thread_counts(0, 1026) == (1026, 1024, 1024)
    assert properties.cc_cpu_thread_counts(1024, 4096) == (4096, 1024, 1024)
    first = CcCoreRuntimeParameters(cc_max_threads=15)
    changed = CcCoreRuntimeParameters(cc_max_threads=1)
    assert first.to_mapping()["cc_max_threads"] == 15
    assert first.fingerprint != changed.fingerprint
    assert first.to_mapping()["cc_max_threads"] == 15


def test_geometry_guard_covers_direct_and_marker_preview_paths() -> None:
    """A 형상 혼합을 직접 Align과 활성 마커 미리보기 경로 모두에서 차단합니다."""
    from test_icp_cccore_geometry import _add_layer, _select, _surface
    from tools.align.operators import marker_alignment

    runtime = _selected_runtime()
    _add_layer(runtime, "숨긴메시기준", standalone=True)
    runtime.model_catalog.promote_model_for_icp_commit("숨긴메시기준")
    runtime.model_catalog.set_model_visibility("숨긴메시기준", False)
    _select(runtime, "이동")
    runtime.model_catalog.toggle_alignment_selection("기준", has_marker_draft=False, completed_pair_count=0)
    with patch.object(marker_alignment, "RUNTIME", runtime), patch.object(marker_alignment, "restore_input_block_reason", return_value=None):
        for preview in (False, True):
            with patch.object(marker_alignment, "is_marker_preview_active", return_value=preview):
                reason = marker_alignment.align_start_block_reason(SimpleNamespace())
                assert "혼합" in reason and "숨긴메시기준" in reason
        _surface(runtime, "기준")
        assert marker_alignment.direct_align_start_block_reason() is None
        runtime.model_repository.companion_meshes.pop("기준")
        with patch.object(marker_alignment, "is_marker_preview_active", return_value=True):
            assert "원본이 없습니다" in marker_alignment.align_start_block_reason(SimpleNamespace())


def test_align_execution_always_dispatches_cccore() -> None:
    """Align 오퍼레이터가 고정 CC 엔진과 배율 보정을 실제 공개 실행 경계로 전달합니다."""
    from tools.align.operators import marker_alignment

    runtime = _selected_runtime()
    with patch.object(marker_alignment, "RUNTIME", runtime), patch.object(marker_alignment, "restore_input_block_reason", return_value=None), \
         patch.object(marker_alignment, "begin_icp_job") as begin, patch.object(marker_alignment, "_register_icp_poll_timer"), \
         patch.object(marker_alignment, "request_redraw"), patch.object(marker_alignment, "_report_status") as report, \
         patch.object(marker_alignment, "configured_icp_python_path", return_value="전용정렬/python.exe"):
        result = marker_alignment.COLMAPALIGN_OT_align_marker_preview.execute(SimpleNamespace(), SimpleNamespace())
    assert result == {"FINISHED"}
    assert begin.call_args.args == (runtime,)
    assert begin.call_args.kwargs["solver_backend"] == "CCCORELIB"
    assert begin.call_args.kwargs["scale_refine_enabled"] is True
    assert begin.call_args.kwargs["open3d_python_path"] == "전용정렬/python.exe"
    assert "±10%" in report.call_args.args[2] and "Small GICP" not in report.call_args.args[2]


def test_begin_job_freezes_preference_thread_limit() -> None:
    """작업 시작 뒤 Preferences 값이 바뀌어도 Manifest와 현재 입력 스냅샷은 유지됩니다."""
    from tools.align import properties
    from tools.align.alignment import icp_cccore_runtime

    runtime = _selected_runtime()
    captured = {}

    def fake_launch(files, manifest, _python_path, _native_path):
        """실제 프로세스 없이 생성된 작업 파일과 Manifest를 검사 대상으로 보관합니다."""
        captured["manifest"] = manifest
        return SimpleNamespace(files=files, job_id=manifest["job_id"])

    configured = SimpleNamespace(value=7)

    def configured_limit(_context=None):
        """테스트 중 변경할 Preferences 해석값을 반환합니다."""
        return configured.value

    handle = None
    try:
        with patch.object(properties, "configured_cc_cpu_thread_limit", new=configured_limit), \
             patch.object(icp_cccore_runtime, "_launch", side_effect=fake_launch):
            handle = icp_cccore_runtime.begin_cccore_job(runtime, "사용하지않음/python.exe")
            configured.value = 1
        input_data = runtime.icp_alignment.input_data
        assert input_data.parameters.cc_max_threads == 7
        assert input_data.parameters.to_mapping()["cc_max_threads"] == 7
        assert captured["manifest"]["parameters"]["cc_max_threads"] == 7
        assert captured["manifest"]["parameter_fingerprint"] == input_data.parameters.fingerprint
        assert captured["manifest"]["input_fingerprint"] == input_data.input_fingerprint
        changed_parameters = icp_cccore_runtime.CcCoreRuntimeParameters(cc_max_threads=1)
        changed_input_fingerprint = icp_cccore_runtime._fingerprint({
            "geometry": input_data.snapshot.input_fingerprint,
            "parameters": changed_parameters.fingerprint,
            "marker_signature": input_data.seed.marker_signature,
            "algorithm": icp_cccore_runtime.CC_ALGORITHM_VERSION,
        })
        assert changed_input_fingerprint != input_data.input_fingerprint
    finally:
        if handle is not None:
            shutil.rmtree(handle.files.job_directory, ignore_errors=True)


def test_cc_distance_log_and_progress_message() -> None:
    """CC 결과에는 거리 분포·추가 배율·검토 사유가 보이고 실제 진행 문구를 유지합니다."""
    from tools.align.ui import panels

    baseline = {"rms": 0.5, "trimmed_rms_90": 0.4, "median": 0.3, "p95": 0.9, "count": 30000}
    best = {"rms": 0.2, "trimmed_rms_90": 0.1, "median": 0.05, "p95": 0.4, "count": 30000}
    result = SimpleNamespace(solver_backend="CCCORELIB", quality={"status": "REVIEW"},
        metadata={"baseline_metrics": baseline, "best_candidate_metrics": best,
                  "review_reasons": ["일부 공간 구간에서 거리 오차가 증가했습니다."]},
        icp_delta=SimpleNamespace(uniform_scale=1.025))
    state = SimpleNamespace(result=result, input_data=SimpleNamespace(seed=SimpleNamespace(uses_marker_preview=True)),
                            progress=SimpleNamespace(backend_id="CCCORELIB", stage="RIGID", message="강체 정렬 17회 진행 중"))
    with patch.object(panels, "RUNTIME", SimpleNamespace(icp_alignment=state)):
        entries = "\n".join(panels._icp_result_entries())
        assert "사용자 확인 필요" in entries and "마커 초기값" in entries
        assert "거리 RMS 0.5" in entries and "가까운 90% RMS 0.1" in entries
        assert "중앙값" in entries and "95백분위 거리" in entries and "30,000개" in entries
        assert "+75.00%" in entries and "×1.025 (+2.50%)" in entries
        assert "검토 · 일부 공간 구간" in entries
        assert not any(word in entries for word in ("Point RMSE", "Plane", "fitness", "노멀"))
        assert panels._icp_progress_entries() == ("강체 정렬 17회 진행 중",)


def test_python_resolution_is_limited_to_explicit_or_dedicated_paths() -> None:
    """패키지·실험 표시·명시적 설정만 실행 경로 권한으로 사용하는지 확인합니다."""
    from tools.align import properties

    source = Path(r"E:\검증전용\source")
    module = source / "tools" / "align" / "properties.py"
    package_python = module.parent / "runtime" / "python" / "python.exe"
    development_python = source.parent / "runtime" / "python" / "python.exe"
    marker = source / "test_env" / "manage.py"
    existing = set()

    def available(path):
        """테스트에서 명시한 설치 파일만 존재하는 것으로 판정합니다."""
        return path in existing

    with patch.object(properties, "__file__", str(module)), patch.object(Path, "is_file", new=available), \
         patch.object(properties, "includes_optional_backends", return_value=False):
        assert properties.configured_icp_python_path() == ""
        existing.add(development_python)
        assert properties.configured_icp_python_path() == ""
        existing.add(marker)
        assert properties.configured_icp_python_path() == str(development_python)
        existing.add(package_python)
        assert properties.configured_icp_python_path() == str(package_python)
    with patch.object(properties, "includes_optional_backends", return_value=True), \
         patch.object(properties, "addon_preferences", return_value=SimpleNamespace(open3d_python_path="명시적/python.exe")):
        assert properties.configured_icp_python_path() == "명시적/python.exe"


class _Layout:
    """환경설정 화면이 실제 제출하는 오퍼레이터와 문구를 기록합니다."""

    def __init__(self):
        """레이아웃 호출에서 기록할 버튼과 문구 목록을 초기화합니다."""
        self.operators = []
        self.labels = []
        self.properties = []

    def prop(self, _owner, property_name, **_kwargs):
        """실제 설정 속성 표시 호출을 화면 없이 받아들입니다."""
        self.properties.append(property_name)

    def label(self, *, text, **kwargs):
        """환경설정의 상태 안내 문구를 기록합니다."""
        self.labels.append(text)

    def operator(self, identifier, **kwargs):
        """환경검사 화면에 노출한 실행 버튼을 기록합니다."""
        self.operators.append(identifier)


def test_cc_environment_button_dispatch_and_registration() -> None:
    """환경검사 버튼과 등록 목록이 CC 검사 helper를 정확한 경로로 호출합니다."""
    from tools.align import properties
    from tools.align.operators import marker_alignment, CLASSES
    from tools.align.alignment import icp_cccore_runtime

    layout = _Layout()
    with patch.object(properties, "includes_optional_backends", return_value=False):
        properties.COLMAPALIGN_AP_preferences.draw(SimpleNamespace(layout=layout), SimpleNamespace())
    assert layout.operators == ["colmap_align.validate_cccore_environment"]
    assert layout.properties == ["cc_cpu_thread_limit"]
    assert any("다음 작업부터 적용" in label for label in layout.labels)
    assert not any("Small GICP" in label for label in layout.labels)
    operator = marker_alignment.COLMAPALIGN_OT_validate_cccore_environment
    assert operator in CLASSES
    with patch.object(marker_alignment, "configured_icp_python_path", return_value="전용/python.exe"), \
         patch.object(marker_alignment, "_report_status") as report, \
         patch.object(icp_cccore_runtime, "validate_cccore_environment", return_value={
             "worker_python_version": "3.12.10",
             "native_environment": {
                 "hardware_thread_count": 16,
                 "effective_max_threads": 15,
             },
         }) as validate:
        assert operator.execute(SimpleNamespace(), SimpleNamespace()) == {"FINISHED"}
        validate.assert_called_once_with("전용/python.exe")
        message = report.call_args.args[2]
        assert "CC 정렬 환경 확인 완료" in message
        assert "oneTBB 병렬" in message and "실제 15스레드" in message and "자동 15스레드" in message
    with patch.object(marker_alignment, "configured_icp_python_path", return_value=""), \
         patch.object(marker_alignment, "_report_status") as report, \
         patch.object(icp_cccore_runtime, "validate_cccore_environment", side_effect=RuntimeError("전용 엔진이 없습니다")):
        assert operator.execute(SimpleNamespace(), SimpleNamespace()) == {"CANCELLED"}
        assert report.call_args.args[2] == "전용 엔진이 없습니다"


def main() -> None:
    """실제 Blender에서 CC 화면·설정 연결 회귀를 순서대로 실행합니다."""
    _add_test_paths()
    tests = tuple(value for name, value in globals().items() if name.startswith("test_") and callable(value))
    for test in tests:
        test()
    print(f"CC 화면 연결: {len(tests)}개 검사 통과")


if __name__ == "__main__":
    main()
