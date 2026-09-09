"""실제 독립 워커의 등록·삼각면 거리·입력·취소 계약을 검증합니다."""
import argparse
from array import array
import ctypes
import json
import math
import os
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ENVIRONMENT = Path(__file__).resolve().parents[3] / ".cccore-runtime"
WORKER = Path(os.environ.get("COLMAP_CCCORE_WORKER", str(ENVIRONMENT / "cccore/colmap_cccore_worker.exe")))
BASE = [(-1,-1,-1),(2,0,0),(0,3,0),(0,0,4),(1,2,1),(-2,1,3),(3,-1,2),(2,3,-2)]


def transform_points(points, scale=1, angle=0, translation=(0,0,0)):
    """알려진 Z 회전·균일 배율·이동을 점에 적용합니다."""
    c, s = math.cos(angle), math.sin(angle)
    return [(scale*(c*x-s*y)+translation[0], scale*(s*x+c*y)+translation[1], scale*z+translation[2]) for x,y,z in points]


def apply_matrix(points, matrix):
    """행 우선 결과 행렬을 원본 배정도 점에 적용합니다."""
    return [tuple(sum(matrix[r][c]*point[c] for c in range(3))+matrix[r][3] for r in range(3)) for point in points]


def point_rms(points, model):
    """작은 독립 기준 구현으로 최근접 점 RMS를 계산합니다."""
    return math.sqrt(sum(min(sum((a-b)**2 for a,b in zip(point, target)) for target in model) for point in points)/len(points))


class NativeContractTest(unittest.TestCase):
    """가짜 코어 없이 실행 파일과 파일 계약을 검사합니다."""

    def setUp(self):
        """검사별 격리 작업 폴더를 생성합니다."""
        self.assertTrue(WORKER.is_file(), f"네이티브 워커가 없습니다: {WORKER}")
        cache = ENVIRONMENT / "cache/cccore-tests"
        cache.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="cccore_", dir=cache)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        """검사에서 생성한 임시 파일만 정리합니다."""
        self.temporary.cleanup()

    def request(self, data=BASE, model=BASE, triangles=None, operation="register", scale=False, lower=.9, upper=1.1, max_threads=4):
        """기존 단발 등록의 필터 기본값을 유지하는 바이너리와 저수준 요청을 작성합니다."""
        for name, points in (("data", data), ("model", model)):
            (self.root/f"{name}.bin").write_bytes(struct.pack("<"+"d"*(len(points)*3), *(axis for p in points for axis in p)))
        request = {"protocol_version":"cccore-native-v2", "operation":operation,
                   "data":{"positions_file":"data.bin", "point_count":len(data)},
                   "model":{"positions_file":"model.bin", "point_count":len(model)},
                   "parameters":{"adjust_scale":scale,"min_scale":lower,"max_scale":upper,"overlap_ratio":1,"filter_out_farthest_points":False,
                                  "max_iterations":100,"sampling_limit":max(3,len(data),len(model)),"max_threads":max_threads},
                   "output_file":"result.json","progress_file":"progress.json","cancel_file":"cancel.flag","distances_file":"distances.bin"}
        if triangles is not None:
            (self.root/"triangles.bin").write_bytes(struct.pack("<"+"I"*(len(triangles)*3), *(i for t in triangles for i in t)))
            request["model"].update(triangles_file="triangles.bin",triangle_count=len(triangles))
        return request

    def run_request(self, request, expected="completed"):
        """실제 워커를 실행하고 표준 출력과 최종 파일의 일치를 확인합니다."""
        path = self.root/"request.json"
        path.write_text(json.dumps(request, ensure_ascii=False),encoding="utf-8")
        completed = subprocess.run([str(WORKER),"--request",str(path)],capture_output=True,text=True,encoding="utf-8",timeout=120)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"],expected,result)
        self.assertEqual(completed.returncode, {"completed":0,"cancelled":2,"error":1}[expected],result)
        self.assertEqual(result["protocol_version"],"cccore-native-v2")
        self.assertEqual(len(result["core_commit"]),40)
        self.assertEqual(result["thread_backend"],"oneTBB")
        self.assertEqual(result["tbb_version"],"2022.0.0")
        self.assertGreaterEqual(result["hardware_thread_count"],1)
        self.assertTrue(result["distance_multithread_support"])
        self.assertTrue(result["octree_multithread_support"])
        requested=request.get("parameters",{}).get("max_threads")
        if type(requested) is int and 1 <= requested <= 1024:
            self.assertEqual(result["requested_max_threads"],requested)
            self.assertEqual(result["effective_max_threads"],min(requested,result["hardware_thread_count"]))
        if (self.root/"result.json").is_file():
            self.assertEqual(json.loads((self.root/"result.json").read_text(encoding="utf-8")),result)
        return result

    def start_session(self, *, max_threads=4):
        """v4 파일 세션을 시작하고 ready 식별자와 자식 프로세스를 반환합니다."""
        root = self.root / "session-job"
        native = root / "native"
        native.mkdir(parents=True)
        identity = {
            "protocol_version": "cccore-native-v4",
            "session_id": "0123456789abcdef0123456789abcdef",
            "job_id": root.name,
            "input_fingerprint": "fedcba9876543210fedcba9876543210",
        }
        request = {
            **identity,
            "parameters": {"max_threads": max_threads},
            "ready_file": "native/session-ready.json",
            "result_file": "native/session-result.json",
            "cancel_file": "cancel.request",
            "command_directory": "native",
        }
        path = root / "native-session-request.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        process = subprocess.Popen(
            [str(WORKER), "--session", str(path.resolve())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ready_path = native / "session-ready.json"
        deadline = time.monotonic() + 30
        while not ready_path.is_file() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(ready_path.is_file(), "세션 ready 파일이 생성되지 않았습니다.")
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        self.assertEqual({name: ready[name] for name in identity}, identity)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["process_id"], process.pid)
        return root, identity, process, ready

    def session_geometry(self, root, name, points, triangles=None, *, cache_scope="SESSION"):
        """세션 캐시용 고정폭 지오메트리 파일과 수명 범위 설명을 작성합니다."""
        positions_name = f"native/{name}-positions.bin"
        (root / positions_name).write_bytes(
            struct.pack("<" + "d" * (len(points) * 3), *(axis for point in points for axis in point))
        )
        result = {
            "geometry_id": name, "positions_file": positions_name,
            "point_count": len(points), "cache_scope": cache_scope,
        }
        if triangles is not None:
            triangles_name = f"native/{name}-triangles.bin"
            (root / triangles_name).write_bytes(
                struct.pack("<" + "I" * (len(triangles) * 3), *(index for face in triangles for index in face))
            )
            result.update(triangles_file=triangles_name, triangle_count=len(triangles))
        return result

    def session_grid_geometry(self, root, name, side, z, *, mesh=False, cache_scope="SESSION"):
        """병렬 취소 회귀 검사에 쓸 규칙 격자와 수명 범위 설명을 작성합니다."""
        positions = array("d")
        for row in range(side):
            for column in range(side):
                positions.extend((column / side, row / side, z))
        positions_name = f"native/{name}-positions.bin"
        (root / positions_name).write_bytes(positions.tobytes())
        result = {
            "geometry_id": name, "positions_file": positions_name,
            "point_count": side * side, "cache_scope": cache_scope,
        }
        if mesh:
            triangles = array("I")
            for row in range(side - 1):
                for column in range(side - 1):
                    lower = row * side + column
                    triangles.extend((lower, lower + 1, lower + side, lower + 1, lower + side + 1, lower + side))
            triangles_name = f"native/{name}-triangles.bin"
            (root / triangles_name).write_bytes(triangles.tobytes())
            result.update(triangles_file=triangles_name, triangle_count=len(triangles) // 3)
        return result

    def session_command(self, root, identity, sequence, operation, *, data=None, model=None, stage="TEST", max_threads=4):
        """고정 순번 v4 명령을 제출하고 응답 또는 세션 실패를 기다립니다."""
        command = self.session_command_payload(
            identity, sequence, operation, data=data, model=model, stage=stage, max_threads=max_threads,
        )
        stem = f"native/command-{sequence:04d}"
        request_path = root / f"{stem}.request.json"
        response_path = root / f"{stem}.response.json"
        request_path.write_text(json.dumps(command), encoding="utf-8")
        deadline = time.monotonic() + 30
        while not response_path.is_file() and not (root / "native/session-result.json").is_file() and time.monotonic() < deadline:
            time.sleep(.005)
        if response_path.is_file():
            return json.loads(response_path.read_text(encoding="utf-8"))
        self.fail("세션 명령 응답이 제한 시간 안에 생성되지 않았습니다.")

    def session_command_payload(self, identity, sequence, operation, *, data=None, model=None, stage="TEST", max_threads=4):
        """오류 계약 검사에도 재사용할 순번별 v4 명령 객체를 만듭니다."""
        stem = f"native/command-{sequence:04d}"
        command = {
            **identity, "sequence": sequence, "operation": operation, "stage": stage,
            "response_file": f"{stem}.response.json", "progress_file": f"{stem}.progress.json",
        }
        if operation != "close":
            command.update({
                "data": data, "model": model,
                "parameters": {
                    "adjust_scale": False, "min_scale": 1.0, "max_scale": 1.0,
                    "overlap_ratio": 1.0, "filter_out_farthest_points": True, "max_iterations": 100,
                    "sampling_limit": max(int(data["point_count"]), int(model["point_count"])),
                    "max_threads": max_threads,
                },
                "distances_file": f"{stem}.distances.bin",
            })
        return command

    def open_without_delete_share(self, path):
        """원자 교체 경계를 재현하도록 삭제 공유 없는 Windows 읽기 핸들을 엽니다."""
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        return kernel, handle

    def finish_session(self, root, process, *, expected_code=0):
        """세션 자식의 종료 코드와 파일 기반 최종 결과를 회수합니다."""
        process.wait(timeout=30)
        self.assertEqual(process.returncode, expected_code)
        return json.loads((root / "native/session-result.json").read_text(encoding="utf-8"))

    def assert_failed_session(self, root, process, code, *, expected_code=1):
        """오류 세션의 종료 코드·표준 출력·원자 결과 파일이 같은지 검사합니다."""
        result = self.finish_session(root, process, expected_code=expected_code)
        stored = json.loads((root / "native/session-result.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, result)
        self.assertEqual(result["code"], code)
        return result

    def test_environment(self):
        """환경 검사가 DLL과 실제 합성 등록을 수행하는지 확인합니다."""
        process = subprocess.run([str(WORKER),"--check-environment"],capture_output=True,text=True,encoding="utf-8",timeout=30)
        self.assertEqual(process.returncode,0,process.stdout)
        result=json.loads(process.stdout)
        self.assertEqual(result["protocol_version"],"cccore-native-v4")
        self.assertEqual(result["self_test"],"passed")
        self.assertTrue(Path(result["library_path"]).is_file())
        self.assertTrue(Path(result["tbb_library_path"]).is_file())
        self.assertEqual(result["thread_backend"],"oneTBB")
        self.assertEqual(result["tbb_version"],"2022.0.0")
        self.assertTrue(result["distance_multithread_support"])
        self.assertTrue(result["octree_multithread_support"])
        self.assertEqual(result["requested_max_threads"],result["hardware_thread_count"])
        self.assertEqual(result["effective_max_threads"],result["hardware_thread_count"])

    def test_session_single_process_and_geometry_cache(self):
        """두 논리 연산이 한 PID에서 같은 지오메트리를 한 번만 적재하는지 확인합니다."""
        root, identity, process, ready = self.start_session()
        try:
            data = self.session_geometry(root, "data", BASE)
            model = self.session_geometry(root, "model", transform_points(BASE, translation=(.01, -.02, .03)))
            first = self.session_command(root, identity, 1, "distance", data=data, model=model, stage="BASELINE")
            second = self.session_command(root, identity, 2, "distance", data=data, model=model, stage="RIGID_EVALUATION")
            closed = self.session_command(root, identity, 3, "close", stage="CLOSE")
            final = self.finish_session(root, process)
            self.assertEqual(ready["process_id"], process.pid)
            self.assertEqual(first["operation_statistics"]["geometry_load_count"], 2)
            self.assertEqual(first["operation_statistics"]["geometry_cache_hits"], 0)
            self.assertEqual(second["operation_statistics"]["geometry_load_count"], 0)
            self.assertEqual(second["operation_statistics"]["geometry_cache_hits"], 2)
            self.assertEqual(closed, final)
            self.assertEqual(final["status"], "closed")
            self.assertEqual(final["statistics"]["geometry_load_count"], 2)
            self.assertEqual(final["statistics"]["geometry_cache_hits"], 2)
            self.assertEqual({item["geometry_id"] for item in final["geometry_statistics"]}, {"data", "model"})
            self.assertEqual(sum(item["load_count"] for item in final["geometry_statistics"]), 2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_session_command_geometry_is_released_after_response(self):
        """명령 전용 점군은 각 응답 뒤 해제되고 세션 모델만 다음 명령에서 재사용됩니다."""
        root, identity, process, _ready = self.start_session()
        try:
            first_data = self.session_geometry(root, "first-data", BASE, cache_scope="COMMAND")
            second_data = self.session_geometry(
                root, "second-data", transform_points(BASE, translation=(.01, 0, 0)),
                cache_scope="COMMAND",
            )
            model = self.session_geometry(root, "model", BASE, cache_scope="SESSION")
            self.session_command(root, identity, 1, "distance", data=first_data, model=model)
            second = self.session_command(root, identity, 2, "distance", data=second_data, model=model)
            closed = self.session_command(root, identity, 3, "close", stage="CLOSE")
            self.finish_session(root, process)
            self.assertEqual(second["operation_statistics"]["geometry_load_count"], 1)
            self.assertEqual(second["operation_statistics"]["geometry_cache_hits"], 1)
            statistics = {item["geometry_id"]: item for item in closed["geometry_statistics"]}
            self.assertTrue(statistics["first-data"]["released_after_command"])
            self.assertTrue(statistics["second-data"]["released_after_command"])
            self.assertFalse(statistics["model"]["released_after_command"])
            self.assertEqual(statistics["model"]["cache_scope"], "SESSION")
            self.assertEqual(
                closed["statistics"]["cache_resident_estimated_bytes"],
                statistics["model"]["estimated_bytes"],
            )
            self.assertGreater(
                closed["statistics"]["cache_peak_estimated_bytes"],
                closed["statistics"]["cache_resident_estimated_bytes"],
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_session_rejects_reuse_of_released_command_geometry(self):
        """응답 뒤 해제된 COMMAND 지오메트리 ID의 재사용을 명시적 수명 오류로 거부합니다."""
        root, identity, process, _ready = self.start_session()
        try:
            data = self.session_geometry(root, "ephemeral-data", BASE, cache_scope="COMMAND")
            model = self.session_geometry(root, "model", BASE, cache_scope="SESSION")
            self.session_command(root, identity, 1, "distance", data=data, model=model)
            command = self.session_command_payload(identity, 2, "distance", data=data, model=model)
            (root / "native/command-0002.request.json").write_text(json.dumps(command), encoding="utf-8")
            self.assert_failed_session(root, process, "GEOMETRY_LIFETIME_EXPIRED")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_session_terminal_boundary_cancel_prevents_success_response(self):
        """마지막 커널 확인 뒤 생성된 취소도 성공 응답 직전 강제 조회에서 감지합니다."""
        root, identity, process, _ready = self.start_session()
        kernel = None
        handle = None
        try:
            data = self.session_grid_geometry(root, "data", 700, .1)
            model = self.session_geometry(root, "model", BASE)
            distance_path = root / "native/command-0001.distances.bin"
            command = self.session_command_payload(identity, 1, "distance", data=data, model=model)
            (root / "native/command-0001.request.json").write_text(json.dumps(command), encoding="utf-8")
            progress_path = root / "native/command-0001.progress.json"
            deadline = time.monotonic() + 10
            serialized = False
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    serialized = json.loads(progress_path.read_text(encoding="utf-8")).get("substage") == "SERIALIZE"
                except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                    pass
                if serialized:
                    break
                time.sleep(.001)
            self.assertTrue(serialized, "마지막 커널 확인 뒤 직렬화 경계를 관측하지 못했습니다.")
            distance_path.write_bytes(b"")
            kernel, handle = self.open_without_delete_share(distance_path)
            temporary = Path(str(distance_path) + f".tmp-{process.pid}")
            deadline = time.monotonic() + 2
            while not temporary.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertTrue(temporary.is_file(), "거리 직렬화의 원자 교체 경계를 관측하지 못했습니다.")
            (root / "cancel.request").write_text("취소", encoding="utf-8")
            kernel.CloseHandle(handle)
            handle = None
            result = self.assert_failed_session(root, process, "CANCELLED", expected_code=2)
            self.assertEqual(result["session_exit_reason"], "cancelled")
            response = json.loads((root / "native/command-0001.response.json").read_text(encoding="utf-8"))
            self.assertEqual(response["status"], "cancelled")
            self.assertNotEqual(response["status"], "completed")
        finally:
            if handle is not None and kernel is not None:
                kernel.CloseHandle(handle)
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_session_registration_matches_v2_request(self):
        """최원거리점 필터를 같은 값으로 명시한 v4 세션과 v2 단발 등록을 비교합니다."""
        model_points = transform_points(BASE, angle=.02, translation=(.03, -.02, .04))
        baseline_request = self.request(model=model_points)
        baseline_request["parameters"]["filter_out_farthest_points"] = True
        baseline = self.run_request(baseline_request)
        root, identity, process, _ = self.start_session()
        try:
            data = self.session_geometry(root, "data", BASE)
            model = self.session_geometry(root, "model", model_points)
            current = self.session_command(root, identity, 1, "register", data=data, model=model, stage="RIGID")
            self.session_command(root, identity, 2, "close", stage="CLOSE")
            self.finish_session(root, process)
            for actual_row, expected_row in zip(current["matrix_4x4"], baseline["matrix_4x4"]):
                for actual, expected in zip(actual_row, expected_row):
                    self.assertAlmostEqual(actual, expected, delta=1e-12)
            self.assertAlmostEqual(current["final_rms"], baseline["final_rms"], delta=1e-12)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_session_rejects_missing_sequence(self):
        """첫 명령을 건너뛴 미래 순번을 누락·역순 오류로 종료합니다."""
        root, identity, process, _ = self.start_session()
        try:
            data = self.session_geometry(root, "data", BASE)
            model = self.session_geometry(root, "model", BASE)
            command = self.session_command_payload(identity, 2, "distance", data=data, model=model)
            (root / "native/command-0002.request.json").write_text(json.dumps(command), encoding="utf-8")
            self.assert_failed_session(root, process, "INVALID_SEQUENCE")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_session_rejects_duplicate_sequence(self):
        """처리한 명령 순번을 새 요청 파일로 다시 제출하면 중복 오류로 종료합니다."""
        root, identity, process, _ = self.start_session()
        try:
            data = self.session_geometry(root, "data", BASE)
            model = self.session_geometry(root, "model", BASE)
            self.assertEqual(self.session_command(root, identity, 1, "distance", data=data, model=model)["status"], "completed")
            duplicate = self.session_command_payload(identity, 1, "distance", data=data, model=model)
            (root / "native/command-0001.request.json").write_text(json.dumps(duplicate), encoding="utf-8")
            self.assert_failed_session(root, process, "DUPLICATE_SEQUENCE")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_session_rejects_identity_change(self):
        """명령의 session ID가 시작 요청과 다르면 계산 전에 세션을 종료합니다."""
        root, identity, process, _ = self.start_session()
        try:
            data = self.session_geometry(root, "data", BASE)
            model = self.session_geometry(root, "model", BASE)
            command = self.session_command_payload(identity, 1, "distance", data=data, model=model)
            command["session_id"] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            (root / "native/command-0001.request.json").write_text(json.dumps(command), encoding="utf-8")
            self.assert_failed_session(root, process, "SESSION_IDENTITY_MISMATCH")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_session_rejects_path_escape(self):
        """명령 응답 경로가 작업 루트 밖을 가리키면 접근 전에 거부합니다."""
        root, identity, process, _ = self.start_session()
        try:
            data = self.session_geometry(root, "data", BASE)
            model = self.session_geometry(root, "model", BASE)
            command = self.session_command_payload(identity, 1, "distance", data=data, model=model)
            command["response_file"] = "../escape.json"
            (root / "native/command-0001.request.json").write_text(json.dumps(command), encoding="utf-8")
            self.assert_failed_session(root, process, "PATH_ESCAPE")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_session_rejects_partial_json(self):
        """부분 기록된 명령 JSON을 처리하거나 다음 명령으로 넘기지 않습니다."""
        root, _, process, _ = self.start_session()
        try:
            (root / "native/command-0001.request.json").write_text('{"protocol_version":', encoding="utf-8")
            self.assert_failed_session(root, process, "INVALID_REQUEST")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_session_rejects_changed_geometry_descriptor(self):
        """이미 적재한 geometry ID에 다른 설명을 붙이는 재사용을 거부합니다."""
        root, identity, process, _ = self.start_session()
        try:
            data = self.session_geometry(root, "data", BASE)
            model = self.session_geometry(root, "model", BASE)
            self.assertEqual(self.session_command(root, identity, 1, "distance", data=data, model=model)["status"], "completed")
            changed = dict(data)
            changed["point_count"] -= 1
            response = self.session_command(root, identity, 2, "distance", data=changed, model=model)
            self.assertEqual(response["status"], "error")
            self.assertEqual(response["code"], "GEOMETRY_DESCRIPTOR_MISMATCH")
            self.assert_failed_session(root, process, "GEOMETRY_DESCRIPTOR_MISMATCH")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_session_cancel_file_stops_process(self):
        """세션 전체 cancel.request를 감지하면 취소 상태와 종료 코드 2를 반환합니다."""
        root, _, process, _ = self.start_session()
        try:
            (root / "cancel.request").write_text("취소", encoding="utf-8")
            result = self.assert_failed_session(root, process, "CANCELLED", expected_code=2)
            self.assertEqual(result["status"], "cancelled")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_session_parallel_cancel_latency(self):
        """병렬 옥트리·거리 계산 중 취소 요청을 2초 안에 래치해 세션을 종료합니다."""
        root, identity, process, _ = self.start_session(max_threads=8)
        try:
            data = self.session_grid_geometry(root, "cancel-data", 450, .005)
            model = self.session_grid_geometry(root, "cancel-model", 450, 0.0, mesh=True)
            command = self.session_command_payload(
                identity, 1, "distance", data=data, model=model, stage="CANCEL_RACE", max_threads=8,
            )
            request_path = root / "native/command-0001.request.json"
            progress_path = root / "native/command-0001.progress.json"
            request_path.write_text(json.dumps(command), encoding="utf-8")
            deadline = time.monotonic() + 20
            observed_parallel_stage = False
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    progress = json.loads(progress_path.read_text(encoding="utf-8"))
                    observed_parallel_stage = (
                        progress.get("status") == "running"
                        and progress.get("substage") in {"OCTREE", "KERNEL"}
                    )
                except (OSError, json.JSONDecodeError):
                    pass
                if observed_parallel_stage:
                    break
                time.sleep(.002)
            self.assertTrue(observed_parallel_stage, "병렬 계산 단계가 취소 전에 관측되지 않았습니다.")
            cancel_started = time.monotonic()
            (root / "cancel.request").write_text("취소", encoding="utf-8")
            result = self.assert_failed_session(root, process, "CANCELLED", expected_code=2)
            self.assertLessEqual(time.monotonic() - cancel_started, 2.0)
            self.assertEqual(result["status"], "cancelled")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_session_parallel_cancel_probe_race(self):
        """여러 병렬 거리 명령의 취소 조회 경쟁이 오취소나 입출력 오류를 만들지 않는지 반복합니다."""
        root, identity, process, _ = self.start_session(max_threads=8)
        try:
            data = self.session_grid_geometry(root, "race-data", 100, .005)
            model = self.session_grid_geometry(root, "race-model", 100, 0.0, mesh=True)
            responses = [
                self.session_command(
                    root, identity, sequence, "distance", data=data, model=model,
                    stage="CANCEL_PROBE_RACE", max_threads=8,
                )
                for sequence in range(1, 9)
            ]
            closed = self.session_command(root, identity, 9, "close", stage="CLOSE", max_threads=8)
            self.finish_session(root, process)
            self.assertTrue(all(response["status"] == "completed" for response in responses))
            self.assertEqual(closed["status"], "closed")
            self.assertEqual(closed["statistics"]["geometry_load_count"], 2)
            self.assertEqual(closed["statistics"]["geometry_cache_hits"], 14)
            self.assertEqual(closed["statistics"]["octree_build_count"], 16)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    @unittest.skipUnless(os.name == "nt", "Windows 경로 오류 계약 검사입니다.")
    def test_session_cancel_probe_path_error_fails_closed(self):
        """취소 경로가 오프라인 공유로 바뀌면 조회 오류를 정상 미존재로 숨기지 않습니다."""
        root, _, process, _ = self.start_session()
        moved_root = root.with_name(f"{root.name}-moved")
        linked = False
        try:
            root.rename(moved_root)
            try:
                os.symlink(
                    r"\\127.0.0.1\codex-cccore-cancel-probe-missing-share",
                    root,
                    target_is_directory=True,
                )
            except OSError as error:
                self.skipTest(f"검사용 디렉터리 심볼릭 링크를 만들 수 없습니다: {error}")
            linked = True
            failure_started = time.monotonic()
            process.wait(timeout=2.0)
            self.assertEqual(process.returncode, 1)
            self.assertLessEqual(time.monotonic() - failure_started, 2.0)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            if linked:
                root.rmdir()

    def test_hardware_thread_clamp(self):
        """하드웨어보다 큰 요청도 기록하고 모든 네이티브 단계의 실제 상한을 제한합니다."""
        result=self.run_request(self.request(max_threads=1024))
        self.assertEqual(result["requested_max_threads"],1024)
        self.assertEqual(result["effective_max_threads"],min(1024,result["hardware_thread_count"]))

    def test_rigid(self):
        """비대칭 점군의 회전과 이동을 정답 행렬과 비교합니다."""
        model=transform_points(BASE,angle=.02,translation=(.03,-.02,.04))
        result=self.run_request(self.request(model=model))
        self.assertLess(point_rms(apply_matrix(BASE,result["matrix_4x4"]),model),2e-6)
        self.assertAlmostEqual(result["scale"],1,places=12)
        self.assertLess(result["final_rms"],2e-6)

    def test_rigid_single_and_parallel_equivalence(self):
        """단일·병렬 등록이 같은 강체 정답과 기존 허용오차를 만족하는지 확인합니다."""
        model=transform_points(BASE,angle=.02,translation=(.03,-.02,.04))
        single=self.run_request(self.request(model=model,max_threads=1))
        parallel=self.run_request(self.request(model=model,max_threads=4))
        single_points=apply_matrix(BASE,single["matrix_4x4"])
        parallel_points=apply_matrix(BASE,parallel["matrix_4x4"])
        self.assertLess(point_rms(single_points,parallel_points),1e-6)
        self.assertAlmostEqual(single["final_rms"],parallel["final_rms"],delta=1e-7)

    def test_sim3_inside_boundary(self):
        """범위 내부 유사변환에서 배율과 행렬의 일치를 확인합니다."""
        model=transform_points(BASE,scale=1.07,angle=.025,translation=(.03,-.02,.04))
        result=self.run_request(self.request(model=model,scale=True))
        self.assertAlmostEqual(result["scale"],1.07,places=6)
        self.assertLess(point_rms(apply_matrix(BASE,result["matrix_4x4"]),model),2e-6)

    def test_scale_boundaries_preserve_rms_and_center(self):
        """경계 밖 정답에서도 실제 행렬 RMS와 중심 이동이 반환값과 일치해야 합니다."""
        centered=transform_points(BASE,translation=(1.8,-2.3,.9))
        for target_scale,lower,upper in ((1.15,.9,1.1),(.85,.9,1.1),(1.16,1/1.1,1/.9),(.86,1/1.1,1/.9)):
            with self.subTest(target_scale=target_scale,lower=lower,upper=upper):
                model=transform_points(centered,scale=target_scale,angle=.012,translation=(.015,-.012,.018))
                result=self.run_request(self.request(data=centered,model=model,scale=True,lower=lower,upper=upper))
                applied=apply_matrix(centered,result["matrix_4x4"])
                measured=point_rms(applied,model)
                self.assertAlmostEqual(result["scale"],min(upper,max(lower,target_scale)),places=6)
                self.assertAlmostEqual(measured,result["final_rms"],delta=3e-6)
                for axis in range(3):
                    self.assertAlmostEqual(sum(p[axis] for p in applied)/len(applied),sum(p[axis] for p in model)/len(model),delta=3e-6)

    def test_rigid_scaled_target(self):
        """강체 등록은 범위 밖 크기의 모델에서도 배율 1과 실제 RMS를 유지합니다."""
        data=transform_points(BASE,translation=(1.8,-2.3,.9))
        model=transform_points(data,scale=1.15,angle=.012,translation=(.015,-.012,.018))
        result=self.run_request(self.request(data=data,model=model,scale=False))
        self.assertEqual(result["scale"],1)
        self.assertAlmostEqual(point_rms(apply_matrix(data,result["matrix_4x4"]),model),result["final_rms"],delta=3e-6)

    def test_point_distance(self):
        """최근접 점 거리 전체 배열을 독립 정답과 비교합니다."""
        data=[(.1,.2,.3),(2.2,.1,0),(.2,2.7,.1)]
        result=self.run_request(self.request(data=data,operation="distance"))
        distances=struct.unpack("<"+"d"*len(data),(self.root/result["distances_file"]).read_bytes())
        for p,distance in zip(data,distances):
            self.assertAlmostEqual(distance,point_rms([p],BASE),delta=1e-6)

    def test_triangle_interior_distance(self):
        """꼭짓점 최근접으로 대체할 수 없는 삼각면 내부 거리를 검사합니다."""
        result=self.run_request(self.request(data=[(.25,.25,.5),(.1,.1,-.2),(2,0,0)],model=[(0,0,0),(1,0,0),(0,1,0)],triangles=[(0,1,2)],operation="distance"))
        distances=struct.unpack("<ddd",(self.root/result["distances_file"]).read_bytes())
        for actual,expected in zip(distances,(.5,.2,1)):
            self.assertAlmostEqual(actual,expected,delta=1e-6)

    def test_point_and_mesh_distance_single_and_parallel_equivalence(self):
        """C2C·C2M 거리 배열이 단일·병렬 실행에서 같은 값을 유지하는지 확인합니다."""
        cases=[
            ([(.1,.2,.3),(2.2,.1,0),(.2,2.7,.1)],BASE,None),
            ([(.25,.25,.5),(.1,.1,-.2),(2,0,0)],[(0,0,0),(1,0,0),(0,1,0)],[(0,1,2)]),
        ]
        for data,model,triangles in cases:
            with self.subTest(mesh=triangles is not None):
                single=self.run_request(self.request(data=data,model=model,triangles=triangles,operation="distance",max_threads=1))
                single_distances=struct.unpack("<"+"d"*len(data),(self.root/single["distances_file"]).read_bytes())
                parallel=self.run_request(self.request(data=data,model=model,triangles=triangles,operation="distance",max_threads=4))
                parallel_distances=struct.unpack("<"+"d"*len(data),(self.root/parallel["distances_file"]).read_bytes())
                for actual,expected in zip(parallel_distances,single_distances):
                    self.assertAlmostEqual(actual,expected,delta=1e-7)

    def test_multicell_distance_repeated_parallel_equivalence(self):
        """여러 옥트리 셀의 C2C·C2M 결과가 반복 병렬 실행에서도 단일 실행과 같은지 확인합니다."""
        rng=random.Random(90210)
        cloud_model=[(x/10,y/10,z/10) for z in range(11) for y in range(11) for x in range(11)]
        cloud_data=[(rng.random(),rng.random(),rng.random()) for _ in range(5000)]
        side=41
        mesh_model=[(x/(side-1),y/(side-1),0) for y in range(side) for x in range(side)]
        mesh_triangles=[]
        for y in range(side-1):
            for x in range(side-1):
                i=y*side+x
                mesh_triangles.extend(((i,i+1,i+side),(i+1,i+side+1,i+side)))
        mesh_data=[(rng.random(),rng.random(),.01+.2*rng.random()) for _ in range(5000)]
        for data,model,triangles in ((cloud_data,cloud_model,None),(mesh_data,mesh_model,mesh_triangles)):
            with self.subTest(mesh=triangles is not None):
                single=self.run_request(self.request(data=data,model=model,triangles=triangles,operation="distance",max_threads=1))
                expected=(self.root/single["distances_file"]).read_bytes()
                for repetition in range(3):
                    with self.subTest(repetition=repetition):
                        parallel=self.run_request(self.request(data=data,model=model,triangles=triangles,operation="distance",max_threads=4))
                        self.assertEqual((self.root/parallel["distances_file"]).read_bytes(),expected)

    def test_real_size_triangle_distance(self):
        """실데이터 규모의 64800개 삼각면과 30000개 점에서 해석적 거리와 처리 시간을 검사합니다."""
        side=181
        model=[(x/(side-1),y/(side-1),0) for y in range(side) for x in range(side)]
        triangles=[]
        for y in range(side-1):
            for x in range(side-1):
                i=y*side+x
                triangles.extend(((i,i+1,i+side),(i+1,i+side+1,i+side)))
        rng=random.Random(301903)
        data=[(rng.random(),rng.random(),.05) for _ in range(30000)]
        result=self.run_request(self.request(data=data,model=model,triangles=triangles,operation="distance"))
        distances=struct.unpack("<"+"d"*len(data),(self.root/result["distances_file"]).read_bytes())
        self.assertLess(max(abs(distance-.05) for distance in distances),1e-6)
        self.assertLess(result["elapsed_seconds"],15,result)
        print(f"실데이터 규모 삼각면 거리 검사: {result['elapsed_seconds']:.3f}초",flush=True)

    def test_mesh_rigid_and_sim3(self):
        """비대칭 닫힌 메시의 면 위 표본을 강체·유사변환으로 등록합니다."""
        vertices=[(0,0,0),(3,0,0),(.3,2,0),(.2,.5,4)]
        triangles=[(0,2,1),(0,1,3),(1,2,3),(2,0,3)]
        rng=random.Random(9812)
        data=[]
        for face in triangles:
            for _ in range(100):
                u,v=rng.random(),rng.random()
                if u+v>1:u,v=1-u,1-v
                data.append(tuple(vertices[face[0]][i]*(1-u-v)+vertices[face[1]][i]*u+vertices[face[2]][i]*v for i in range(3)))
        for scale in (1,1.025):
            with self.subTest(scale=scale):
                model=transform_points(vertices,scale=scale,angle=.012,translation=(.014,-.009,.011))
                result=self.run_request(self.request(data=data,model=model,triangles=triangles,scale=scale!=1))
                expected=transform_points(data,scale=scale,angle=.012,translation=(.014,-.009,.011))
                actual=apply_matrix(data,result["matrix_4x4"])
                error=math.sqrt(sum(sum((a-b)**2 for a,b in zip(p,q)) for p,q in zip(actual,expected))/len(data))
                self.assertLess(error,5e-5)
                self.assertLess(result["final_rms"],1e-5)
                self.assertAlmostEqual(result["scale"],scale,delta=2e-5)

    def test_mesh_farthest_filter_preserves_partial_overlap_correspondences(self):
        """원거리점 제거와 부분 겹침 필터를 함께 거쳐도 일치하는 표면의 항등 변환을 보존합니다."""
        vertices = [(0, 0, 0), (3, 0, 0), (.3, 2, 0), (.2, .5, 4)]
        triangles = [(0, 2, 1), (0, 1, 3), (1, 2, 3), (2, 0, 3)]
        rng = random.Random(9812)
        data = []
        for face in triangles:
            for _ in range(100):
                u, v = rng.random(), rng.random()
                if u + v > 1:
                    u, v = 1 - u, 1 - v
                data.append(tuple(vertices[face[0]][axis] * (1 - u - v)
                                  + vertices[face[1]][axis] * u + vertices[face[2]][axis] * v
                                  for axis in range(3)))
        outliers = [(100.0 + index, 120.0 + index, 150.0 + index) for index in range(5)]
        contaminated = data + outliers
        for overlap in (.6, .9):
            for adjust_scale in (False, True):
                with self.subTest(overlap=overlap, adjust_scale=adjust_scale):
                    request = self.request(data=contaminated, model=vertices, triangles=triangles, scale=adjust_scale)
                    request["parameters"].update(overlap_ratio=overlap, filter_out_farthest_points=True)
                    result = self.run_request(request)
                    actual = apply_matrix(data, result["matrix_4x4"])
                    error = math.sqrt(sum(sum((a - b) ** 2 for a, b in zip(p, q))
                                          for p, q in zip(actual, data)) / len(data))
                    self.assertLess(error, 5e-5)
                    self.assertLess(result["final_rms"], 1e-5)
                    self.assertAlmostEqual(result["scale"], 1.0, delta=2e-5)
                    self.assertLess(result["final_point_count"], len(contaminated))

    def test_pre_cancelled(self):
        """연산 전 취소를 명시적 취소 상태와 종료 코드로 반환합니다."""
        request=self.request()
        (self.root/"cancel.flag").write_text("취소",encoding="utf-8")
        self.run_request(request,"cancelled")

    def test_cancel_running_process(self):
        """실행 중인 작업이 취소 파일을 감지해 종료하는지 확인합니다."""
        rng=random.Random(2)
        data=[(rng.random(),rng.random(),rng.random()) for _ in range(100000)]
        request=self.request(data=data,model=transform_points(data,translation=(.01,.01,.01)))
        path=self.root/"request.json"
        path.write_text(json.dumps(request),encoding="utf-8")
        process=subprocess.Popen([str(WORKER),"--request",str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8")
        try:
            deadline=time.monotonic()+15
            while not (self.root/"progress.json").is_file() and time.monotonic()<deadline and process.poll() is None:
                time.sleep(.005)
            (self.root/"cancel.flag").write_text("취소",encoding="utf-8")
            stdout,_=process.communicate(timeout=30)
            self.assertEqual(json.loads(stdout)["status"],"cancelled",stdout)
            self.assertEqual(process.returncode,2)
        finally:
            if process.poll() is None:process.kill();process.wait()

    def test_invalid_inputs(self):
        """좌표·형상·범위 오류와 명시적 최원거리점 필터 계약 위반을 거부합니다."""
        cases=[("nan","INVALID_COORDINATE"),("length","BINARY_LENGTH_MISMATCH"),("scale","INVALID_SCALE_RANGE"),
               ("count","INVALID_COUNT"),("line","DEGENERATE_INPUT"),("sample","SAMPLING_LIMIT_EXCEEDED"),
               ("triangle_index","INVALID_TRIANGLE_INDEX"),("triangle_flat","DEGENERATE_TRIANGLE"),
               ("threads_zero","INVALID_COUNT"),("threads_negative","INVALID_COUNT"),
               ("threads_boolean","INVALID_COUNT"),("threads_float","INVALID_COUNT"),
               ("filter_number","INVALID_PARAMETER")]
        for case,code in cases:
            with self.subTest(case=case):
                request=self.request()
                if case=="nan":(self.root/"data.bin").write_bytes(struct.pack("<"+"d"*24,float("nan"),*([1]*23)))
                if case=="length":(self.root/"data.bin").write_bytes(b"x")
                if case=="scale":request["parameters"]["min_scale"]=-1
                if case=="count":request["data"]["point_count"]=2**40
                if case=="line":request=self.request(data=[(0,0,0),(1,1,1),(2,2,2)])
                if case=="sample":request["parameters"]["sampling_limit"]=3
                if case=="triangle_index":request=self.request(triangles=[(0,1,100)])
                if case=="triangle_flat":request=self.request(triangles=[(0,1,1)])
                if case=="threads_zero":request["parameters"]["max_threads"]=0
                if case=="threads_negative":request["parameters"]["max_threads"]=-1
                if case=="threads_boolean":request["parameters"]["max_threads"]=True
                if case=="threads_float":request["parameters"]["max_threads"]=1.5
                if case=="filter_number":request["parameters"]["filter_out_farthest_points"]=1
                result=self.run_request(request,"error")
                self.assertEqual(result["code"],code)
                (self.root/"result.json").unlink(missing_ok=True)

    def test_legacy_request_preserves_omitted_filter(self):
        """과거 단발 요청은 필터 생략과 세 점 이상인 낮은 겹침 비율을 유지합니다."""
        request = self.request(data=BASE * 5, model=BASE * 5)
        request["parameters"]["overlap_ratio"] = 0.09
        request["parameters"].pop("filter_out_farthest_points")
        self.run_request(request)

    def test_session_registration_rejects_v4_parameter_violations(self):
        """새 세션은 필터 생략·숫자 필터·낮은 겹침 비율을 거부합니다."""
        base_root = self.root
        for case, code in (("missing", "INVALID_PARAMETER"), ("number", "INVALID_PARAMETER"),
                           ("overlap", "INVALID_OVERLAP")):
            self.root = base_root / case
            root, identity, process, _ = self.start_session()
            try:
                data = self.session_geometry(root, "data", BASE)
                model = self.session_geometry(root, "model", BASE)
                request = self.session_command_payload(identity, 1, "register", data=data, model=model)
                if case == "missing":
                    request["parameters"].pop("filter_out_farthest_points")
                elif case == "number":
                    request["parameters"]["filter_out_farthest_points"] = 1
                else:
                    request["parameters"]["overlap_ratio"] = 0.09
                (root / "native/command-0001.request.json").write_text(json.dumps(request), encoding="utf-8")
                with self.subTest(case=case):
                    self.assert_failed_session(root, process, code)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                self.root = base_root

    def run_with_progress_lock(self, hold_seconds):
        """삭제 공유를 허용하지 않는 읽기 핸들로 Windows 파일 교체 경쟁을 재현합니다."""
        rng=random.Random(2718)
        data=[(rng.random(),rng.random(),rng.random()) for _ in range(200000)]
        request=self.request(data=data,model=transform_points(data,translation=(.03,-.02,.01)))
        request["parameters"]["max_iterations"]=2
        path=self.root/"request.json"
        path.write_text(json.dumps(request),encoding="utf-8")
        process=subprocess.Popen([str(WORKER),"--request",str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8")
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
        kernel.CreateFileW.restype=ctypes.c_void_p
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=None
        try:
            deadline=time.monotonic()+10
            while not (self.root/"progress.json").is_file() and time.monotonic()<deadline and process.poll() is None:
                time.sleep(.001)
            handle=kernel.CreateFileW(str(self.root/"progress.json"),0x80000000,3,None,3,0,None)
            self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
            time.sleep(hold_seconds)
            kernel.CloseHandle(handle)
            handle=None
            stdout,_=process.communicate(timeout=30)
            return json.loads(stdout),process.returncode
        finally:
            if handle is not None:kernel.CloseHandle(handle)
            if process.poll() is None:process.kill();process.wait()

    def test_progress_short_sharing_hold(self):
        """짧은 읽기 점유 뒤에는 원자 교체를 완료하고 작업을 계속합니다."""
        result,code=self.run_with_progress_lock(.15)
        self.assertEqual(result["status"],"completed",result)
        self.assertEqual(code,0)
        self.assertEqual(json.loads((self.root/"result.json").read_text(encoding="utf-8")),result)

    def test_progress_long_sharing_hold(self):
        """재시도 한도를 넘는 파일 점유는 명시적 오류와 일치하는 결과 파일로 종료합니다."""
        result,code=self.run_with_progress_lock(.8)
        self.assertEqual(result["status"],"error",result)
        self.assertEqual(result["code"],"OUTPUT_IO_ERROR")
        self.assertIn(result["native_code"],(5,32,33))
        self.assertEqual(code,1)
        self.assertEqual(json.loads((self.root/"result.json").read_text(encoding="utf-8")),result)

    def test_path_collision(self):
        """입력과 출력의 대소문자만 다른 충돌도 원본을 보존하며 거부합니다."""
        request=self.request()
        before=(self.root/"data.bin").read_bytes()
        request["output_file"]="DATA.BIN"
        result=self.run_request(request,"error")
        self.assertEqual(result["code"],"PATH_COLLISION")
        self.assertEqual((self.root/"data.bin").read_bytes(),before)

    def test_path_escape(self):
        """작업 폴더 밖 경로를 파일 접근 전에 거부합니다."""
        request=self.request()
        request["data"]["positions_file"]="../escape.bin"
        result=self.run_request(request,"error")
        self.assertEqual(result["code"],"PATH_ESCAPE")


if __name__ == "__main__":
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument("--report")
    options,remaining=parser.parse_known_args()
    program=unittest.main(argv=[sys.argv[0]]+remaining,verbosity=2,exit=False)
    if options.report:
        Path(options.report).write_text(json.dumps({"worker":str(WORKER),"tests_run":program.result.testsRun,
            "succeeded":program.result.wasSuccessful(),"failures":[str(test) for test,_ in program.result.failures],
            "errors":[str(test) for test,_ in program.result.errors]},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    sys.exit(0 if program.result.wasSuccessful() else 1)
