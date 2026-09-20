"""별도 Blender에서 보존한 세 마커로 실제 Align 연산자를 검증합니다."""

import argparse
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from importlib import import_module
import json
import os
from pathlib import Path
import shutil
import sys
import time
import traceback


def arguments():
    """명령행 또는 환경변수에서 입력과 새 출력 폴더를 받습니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name, variable in (("manifest", "ALIGN_VERIFY_MANIFEST"), ("project", "ALIGN_VERIFY_PROJECT"),
                           ("output", "ALIGN_VERIFY_OUTPUT"), ("package", "ALIGN_VERIFY_PACKAGE")):
        parser.add_argument("--" + name, default=os.environ.get(variable), required=not os.environ.get(variable))
    values = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    return parser.parse_args(values)


def plain(value):
    """배열·불변 자료형을 생략 없이 JSON 호환 값으로 변환합니다."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, Enum):
        return plain(value.value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_mapping"):
        return plain(value.to_mapping())
    if is_dataclass(value):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    if hasattr(value, "tolist"):
        return plain(value.tolist())
    raise TypeError(f"검증 자료를 직렬화할 수 없습니다: {type(value).__name__}")


def write_new_json(path, value):
    """기존 결과를 덮어쓰지 않고 UTF-8 JSON 파일을 한 번 생성합니다."""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(plain(value), stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def file_state(path):
    """원본의 크기와 나노초 수정 시각을 읽기 전용으로 기록합니다."""
    info = path.stat()
    return {"path": str(path), "size": info.st_size, "mtime_ns": info.st_mtime_ns}


class BlenderVerification:
    """현재 프로세스의 정상 GUI와 제품 연산자만 사용하는 검증 수명입니다."""

    def __init__(self, args):
        """입력·출력 충돌을 거부하고 별도 프로세스의 설치본과 원본 상태를 확인합니다."""
        import bpy
        import numpy as np
        self.bpy, self.np = bpy, np
        self.args = args
        self.started = time.perf_counter()
        self.project = Path(args.project).resolve(strict=True)
        self.manifest_path = Path(args.manifest).resolve(strict=True)
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8-sig"))
        self.output = Path(args.output).resolve()
        if bpy.app.background or Path(bpy.data.filepath).resolve() != self.project:
            raise RuntimeError("지정한 원본 프로젝트를 연 별도 GUI Blender에서 실행해야 합니다.")
        if self.output.exists():
            raise FileExistsError(f"출력 폴더가 이미 존재합니다: {self.output}")
        self.output.mkdir(parents=True, exist_ok=False)
        self.events = (self.output / "events.jsonl").open("x", encoding="utf-8")
        self.report = {"status": "실행 중", "pid": os.getpid(), "project": str(self.project),
                       "manifest": str(self.manifest_path), "package": args.package,
                       "timeout_seconds": 720, "cancel_grace_seconds": 15,
                       "blender": bpy.app.version_string, "stage_times": {}}
        self.runtime = self.icp = self.handle = None
        self.job = None
        self.cancel_started = None
        self.interrupted = False
        self.finished = False
        self.seen = set()
        self.native_calls = []
        self.last_progress = None
        self.prepared = False
        self.source_before = {}
        self.stage = "프로젝트 복원"
        self.stage_started = self.started
        origins = [self.manifest["geometry"][role]["provenance"]["sources"][0]
                   for role in ("moving", "reference")]
        self.origins = {item["owner_model_id"]: item for item in origins}
        paths = {self.project, *(Path(item["source_path"]).resolve(strict=True) for item in origins)}
        self.source_before = {str(path): file_state(path) for path in paths}
        write_new_json(self.output / "source-before.json", self.source_before)
        write_new_json(self.output / "baseline-manifest.json", self.manifest)
        package = import_module(args.package)
        base = args.package + ".tools.align."
        self.state_module = import_module(base + "state")
        self.runtime = self.state_module.RUNTIME
        self.icp = import_module(base + "alignment.icp_runtime")
        self.properties = import_module(base + "properties")
        self.report.update(addon_file=package.__file__, addon_version=package.__version__)
        if args.package not in bpy.context.preferences.addons:
            raise RuntimeError("격리 설치 확장이 현재 Blender 프로세스에서 활성화되지 않았습니다.")
        self.event("검증 시작", sources=self.source_before)

    def event(self, message, **values):
        """단계별 관측값을 추가 전용 로그에 즉시 기록합니다."""
        payload = {"elapsed_seconds": time.perf_counter() - self.started, "message": message, **values}
        self.events.write(json.dumps(plain(payload), ensure_ascii=False, allow_nan=False) + "\n")
        self.events.flush()
        print(message, json.dumps(plain(values), ensure_ascii=False), flush=True)

    def transition(self, name):
        """이전 단계의 실제 소요 시간을 보존하고 다음 단계를 시작합니다."""
        now = time.perf_counter()
        self.report["stage_times"][self.stage] = now - self.stage_started
        self.stage, self.stage_started = name, now
        self.event("단계 전환", stage=name)

    def prepare_markers(self):
        """복원한 모델 변환을 대조하고 보존된 세 쌍을 실제 마커 미리보기로 계산합니다."""
        bpy, np, runtime = self.bpy, self.np, self.runtime
        if runtime.icp_alignment.result is not None or runtime.marker_alignment.is_active:
            raise RuntimeError("별도 프로세스에 기존 정렬 미리보기가 있어 검증을 중단합니다.")
        moving = self.manifest["moving_model_id"]
        references = self.manifest["reference_model_ids"]
        if len(references) != 1:
            raise RuntimeError("이 검증은 기준 모델 한 개와 이동 모델 한 개가 필요합니다.")
        for model_id, origin in self.origins.items():
            np.testing.assert_allclose(runtime.model_repository.alignment_transform_for_model_id(model_id).matrix_float64,
                                       origin["committed_alignment_float64"], rtol=0, atol=1e-12)
            display = runtime.model_repository.display_transform_for_model_id(model_id)
            np.testing.assert_allclose(display.matrix_float64, origin["display_transform_float64"], rtol=0, atol=1e-12)
        runtime.marker_selection.clear()
        if bpy.ops.colmap_align.select_data_layer(model_id=references[0]) != {"FINISHED"}:
            raise RuntimeError("기준 모델 선택 연산자가 실패했습니다.")
        if bpy.ops.colmap_align.select_data_layer(model_id=moving, extend_selection=True) != {"FINISHED"}:
            raise RuntimeError("이동 모델 선택 연산자가 실패했습니다.")
        signatures = self.manifest["geometry"]["provenance"]["marker_signature"]
        if len(signatures) != 3:
            raise RuntimeError("보존된 마커가 세 쌍이 아닙니다.")
        pairs = []
        for pair_id, reference, moving_point in signatures:
            points = []
            for role, model_id, kind, source, point_id, source_id, position in (reference, moving_point):
                points.append(self.state_module.SelectedPoint(
                    model_role=role, model_id=model_id, point_kind=kind, geometry_source=source,
                    point3d_id=point_id, source_point_id=source_id, local_position=tuple(position)))
            pairs.append(self.state_module.MarkerPair(pair_id, *points))
        runtime.marker_selection.completed_pairs = pairs
        runtime.marker_selection.next_pair_id = max(pair.pair_id for pair in pairs) + 1
        self.properties.sync_marker_pair_list(bpy.context.window_manager)
        bpy.context.scene.colmap_align_settings.marker_use_uniform_scale = True
        configured_threads = self.properties.configured_cc_cpu_thread_limit()
        if configured_threads != self.manifest["parameters"]["cc_max_threads"]:
            raise RuntimeError("격리 설치본의 CPU 스레드 설정이 보존된 실행 조건과 다릅니다.")
        self.transition("세 마커 미리보기")
        if not bpy.ops.colmap_align.toggle_marker_preview.poll():
            raise RuntimeError("마커 미리보기 연산자의 실행 조건을 충족하지 못했습니다.")
        if bpy.ops.colmap_align.toggle_marker_preview() != {"FINISHED"}:
            raise RuntimeError("실제 마커 미리보기 연산자가 실패했습니다.")
        result = runtime.marker_alignment.result
        np.testing.assert_allclose(result.preview_alignment.matrix_float64,
                                   self.manifest["initial_alignment_4x4"], rtol=0, atol=1e-10)
        np.testing.assert_allclose(result.metrics.rms, self.manifest["marker_initial_rms"], rtol=0, atol=1e-12)
        if result.marker_signature != self.manifest["marker_signature"]:
            raise RuntimeError("복원된 세 마커의 지문이 원본 실행과 다릅니다.")
        write_new_json(self.output / "marker-result.json", result)
        self.prepared = True

    def start_alignment(self):
        """실제 Align 연산자를 호출하고 파일 정리 이전 수집을 위해 제품 폴링을 직접 진행합니다."""
        self.transition("Align 입력 준비 및 시작")
        if not self.bpy.ops.colmap_align.align_marker_preview.poll():
            raise RuntimeError("Align 연산자의 실행 조건을 충족하지 못했습니다.")
        result = self.bpy.ops.colmap_align.align_marker_preview()
        self.report["operator_result"] = sorted(result)
        self.handle = self.runtime.icp_alignment.job_handle
        if result != {"FINISHED"} or self.handle is None:
            raise RuntimeError(self.runtime.icp_alignment.last_error or "Align 작업이 시작되지 않았습니다.")
        callback = self.runtime.icp_timer
        if callback is not None and self.bpy.app.timers.is_registered(callback):
            self.bpy.app.timers.unregister(callback)
        self.job = Path(self.handle.files.job_directory)
        self.report["job_directory"] = str(self.job)
        actual = json.loads((self.job / "manifest.json").read_text(encoding="utf-8"))
        write_new_json(self.output / "job-manifest.json", actual)
        for field in ("moving_model_id", "reference_model_ids", "marker_signature", "geometry_mode",
                      "reverse_registration", "parameters"):
            if actual[field] != self.manifest[field]:
                raise RuntimeError(f"실제 작업과 보존 조건이 다릅니다: {field}")
        self.np.testing.assert_allclose(actual["initial_alignment_4x4"], self.manifest["initial_alignment_4x4"],
                                       rtol=0, atol=1e-10)
        actual_fingerprint = actual["geometry"]["input_fingerprint"]
        expected_fingerprint = self.manifest["geometry"]["input_fingerprint"]
        self.report["geometry_fingerprint"] = actual_fingerprint
        if actual_fingerprint != expected_fingerprint:
            raise RuntimeError("실제 연산자가 추출한 형상 지문이 보존 입력과 다릅니다.")
        self.transition("Align 계산")

    def collect(self):
        """제품이 작업 폴더를 정리하기 전에 명령·결과·진행 변화와 행렬 파일을 보존합니다."""
        if self.job is None:
            return
        paths = list(self.job.glob("*.json")) + list((self.job / "native").glob("*.json"))
        for path in paths:
            relative = path.relative_to(self.job)
            name = relative.as_posix()
            if name in self.seen:
                continue
            try:
                raw = path.read_bytes()
                payload = json.loads(raw)
            except (OSError, ValueError):
                continue
            if "progress" in path.name:
                if relative == Path("progress.json") and payload != self.last_progress:
                    self.event("작업 진행", progress=payload)
                    self.last_progress = payload
                continue
            destination = self.output / "job" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(raw)
            self.seen.add(name)
            if path.name.endswith(".response.json") and payload.get("operation") != "close":
                self.native_calls.append(payload)
        result_arrays = self.job / "result.npz"
        if not self.handle.is_running and result_arrays.is_file() and "result.npz" not in self.seen:
            destination = self.output / "job/result.npz"
            destination.parent.mkdir(parents=True, exist_ok=True)
            with result_arrays.open("rb") as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target)
            self.seen.add("result.npz")

    def request_stop(self, reason):
        """본 검증에서 시작한 작업에만 정상 취소를 요청하고 유예 시간을 기록합니다."""
        if self.cancel_started is None:
            self.interrupted = True
            self.cancel_started = time.perf_counter()
            self.report["interruption_reason"] = reason
            self.report["cancel_requested"] = self.icp.request_icp_cancel(self.runtime)
            self.event("검증 중단 요청", reason=reason)

    def finish(self, status):
        """전체 결과와 원본 무변경 증거를 기록하고 이 Blender만 저장 없이 종료합니다."""
        if self.finished:
            return
        self.finished = True
        try:
            self.collect()
            self.report["stage_times"][self.stage] = time.perf_counter() - self.stage_started
            self.report.update(status=status, elapsed_seconds=time.perf_counter() - self.started,
                               phase=self.runtime.icp_alignment.phase, last_error=self.runtime.icp_alignment.last_error)
            write_new_json(self.output / "native-calls.json", self.native_calls)
            result = self.runtime.icp_alignment.result
            if result is not None:
                write_new_json(self.output / "runtime-result.json", result)
                self.report["native_calls"] = plain(result.metadata.get("physical_native_calls", []))
                self.report["native_session_statistics"] = plain(result.metadata.get("session_statistics", {}))
        except Exception:
            self.report["status"] = "실패"
            self.report["finalization_error"] = traceback.format_exc()
        finally:
            try:
                try:
                    after = {path: file_state(Path(path)) for path in self.source_before}
                    write_new_json(self.output / "source-after.json", after)
                    self.report["sources_unchanged"] = after == self.source_before
                    if not self.report["sources_unchanged"]:
                        self.report["status"] = "실패"
                        self.report["source_error"] = "원본 파일의 크기 또는 수정 시각이 변경되었습니다."
                except Exception:
                    self.report.update(status="실패", sources_unchanged=False,
                                       source_error=traceback.format_exc())
                write_new_json(self.output / "summary.json", self.report)
                self.event("검증 종료", status=self.report["status"],
                           sources_unchanged=self.report.get("sources_unchanged", False))
            finally:
                self.events.close()
                self.bpy.ops.wm.quit_blender()

    def tick(self):
        """정상 복원·실제 연산·정상 취소를 GUI 타이머에서 제한 시간 안에 진행합니다."""
        try:
            now = time.perf_counter()
            if now - self.started > 720 and self.cancel_started is None:
                self.request_stop("720초 검증 제한 시간을 초과했습니다.")
            if self.cancel_started is not None:
                self.collect()
                if self.handle is not None:
                    self.icp.poll_icp_job(self.runtime)
                if self.handle is None or not self.handle.is_running:
                    self.finish("중단")
                    return None
                if now - self.cancel_started > 15:
                    self.collect()
                    self.icp.invalidate_icp(self.runtime, "검증 취소 유예 시간을 초과했습니다.")
                    self.finish("중단")
                    return None
                return .1
            if not self.prepared:
                if self.runtime.session.phase != "ACTIVE" or self.runtime.project_restore.phase != "IDLE":
                    return .2
                if self.runtime.workspace_setup_task is not None or self.runtime.import_in_progress:
                    return .2
                self.prepare_markers()
                return .1
            if self.handle is None:
                self.start_alignment()
                return .1
            self.collect()
            # 종료 직전 결과 파일 수집과 제품 정리의 경쟁을 막고 종료 후 검증을 그대로 사용합니다.
            if self.handle.is_running:
                return .1
            self.collect()
            self.icp.poll_icp_job(self.runtime)
            if self.runtime.icp_alignment.phase not in {"ICP_RUNNING", "ICP_CANCEL_PENDING"}:
                self.finish("완료" if self.runtime.icp_alignment.result is not None else "실패")
                return None
            return .1
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.event("검증 오류", error=self.report["error"])
            if self.handle is not None and self.handle.is_running:
                self.request_stop("검증 조건 불일치 또는 실행 오류")
                return .1
            self.finish("실패")
            return None


def main():
    """명시적으로 시작한 별도 Blender GUI에만 검증 타이머를 등록합니다."""
    args = arguments()
    import bpy
    output_existed = Path(args.output).exists()
    try:
        verification = BlenderVerification(args)
        verification.bpy.app.timers.register(verification.tick, first_interval=.5)
    except Exception:
        error = traceback.format_exc()
        print("검증 초기화 실패", error, flush=True)
        output = Path(args.output)
        if not output_existed and output.is_dir():
            failure = {"status": "실패", "error": error}
            before_path = output / "source-before.json"
            if before_path.is_file():
                try:
                    before = json.loads(before_path.read_text(encoding="utf-8"))
                    after = {path: file_state(Path(path)) for path in before}
                    write_new_json(output / "source-after.json", after)
                    failure["sources_unchanged"] = before == after
                except Exception:
                    failure.update(sources_unchanged=False, source_error=traceback.format_exc())
            write_new_json(output / "startup-error.json", failure)
        bpy.ops.wm.quit_blender()


if __name__ == "__main__":
    main()
