"""해석적 평면과 비대칭 상자로 실제 v8 점–평면 등록을 검증합니다."""
import math
import json
import struct
import shutil
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_cccore_worker as native
from test_cccore_worker import apply_matrix, transform_points


def box_fixture():
    """서로 다른 세 변을 가진 상자의 면 내부 표본과 단위 노말을 만듭니다."""
    vertices, triangles, samples, normals = [], [], [], []
    for axis, extent in enumerate((1., 1.7, 2.3)):
        others = [index for index in range(3) if index != axis]
        for sign in (-1., 1.):
            start = len(vertices)
            for u, v in ((-1,-1),(1,-1),(1,1),(-1,1)):
                point = [0.,0.,0.]
                point[axis] = sign * extent
                point[others[0]] = u * (1.,1.7,2.3)[others[0]]
                point[others[1]] = v * (1.,1.7,2.3)[others[1]]
                vertices.append(tuple(point))
            triangles.extend(((start,start+1,start+2),(start,start+2,start+3)))
            for u in (-.65,-.2,.3,.7):
                for v in (-.6,-.1,.4,.65):
                    point = [0.,0.,0.]
                    point[axis] = sign * extent
                    point[others[0]] = u * (1.,1.7,2.3)[others[0]]
                    point[others[1]] = v * (1.,1.7,2.3)[others[1]]
                    normal = [0.,0.,0.]
                    normal[axis] = sign
                    samples.append(tuple(point))
                    normals.append(tuple(normal))
    return vertices, triangles, samples, normals


class PointToPlaneTest(unittest.TestCase):
    """기존 대형 회귀를 상속 실행하지 않고 실제 세션 보조 함수만 재사용합니다."""
    setUp = native.NativeContractTest.setUp
    tearDown = native.NativeContractTest.tearDown
    start_session = native.NativeContractTest.start_session
    session_geometry = native.NativeContractTest.session_geometry
    session_command = native.NativeContractTest.session_command
    session_command_payload = native.NativeContractTest.session_command_payload

    def terminal_result(self, root, identity, process, expected):
        """종료 파일의 식별자·상태와 실제 프로세스 종료 코드가 일치하는지 확인합니다."""
        process.wait(timeout=30)
        self.assertEqual(process.returncode, {"closed":0,"error":1,"cancelled":2}[expected])
        result = json.loads((root / "native/session-result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"],expected,result)
        self.assertEqual({key:result[key] for key in identity},identity)
        return result

    def registration_response(self, root, identity, process, data, model, options):
        """명령 응답 또는 경로 검증의 세션 종료 결과 중 실제 게시된 응답을 회수합니다."""
        command = self.session_command_payload(identity,1,"register",data=data,model=model)
        command["parameters"].update(options)
        pending = root / "native/command-0001.request.tmp"
        pending.write_text(json.dumps(command),encoding="utf-8")
        pending.replace(root / "native/command-0001.request.json")
        response = root / "native/command-0001.response.json"
        terminal = root / "native/session-result.json"
        deadline = time.monotonic()+30
        while not response.is_file() and not terminal.is_file():
            if process.poll() is not None:
                break
            self.assertLess(time.monotonic(),deadline,"명령 응답 또는 세션 종료 파일이 생성되지 않았습니다.")
            time.sleep(.005)
        path = response if response.is_file() else terminal
        self.assertTrue(path.is_file(),"응답 파일 없이 워커가 종료되었습니다.")
        return json.loads(path.read_text(encoding="utf-8"))

    def register(self, points, vertices, faces, normals=None, parameters=None, corrupt=None, cancel=False):
        """격리 v8 워커에서 한 번 등록하고 성공·실패와 무관하게 세션을 회수합니다."""
        thread_count = (parameters or {}).get("max_threads",4)
        root, identity, process, _ = self.start_session(protocol_version="cccore-native-v8",max_threads=thread_count)
        try:
            data = self.session_geometry(root, "data", points)
            model = self.session_geometry(root, "model", vertices, faces)
            if normals is not None:
                path = root / "native/data-normals.bin"
                path.write_bytes(struct.pack("<" + "d" * (3 * len(normals)), *(v for n in normals for v in n)))
                data["normals_file"] = "native/data-normals.bin"
            if corrupt:
                corrupt(root, data)
            if cancel:
                (root / "cancel.request").write_text("cancel", encoding="utf-8")
                return self.terminal_result(root,identity,process,"cancelled")
            options = {"registration_method":"POINT_TO_PLANE", "normal_cosine_threshold":.5 if normals is not None else -1,
                       "filter_out_farthest_points":False}
            options.update(parameters or {})
            result = self.registration_response(root,identity,process,data,model,options)
            if result["status"] == "completed":
                self.session_command(root, identity, 2, "close")
                self.terminal_result(root,identity,process,"closed")
            else:
                terminal = self.terminal_result(root,identity,process,result["status"])
                for key in ("code","native_code","message"):
                    self.assertEqual(terminal.get(key),result.get(key),key)
            return result
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=30)
            shutil.rmtree(root)

    def test_known_rigid_and_scale(self):
        """비대칭 상자의 알려진 역변환과 강체·배율 해를 독립 좌표로 비교합니다."""
        vertices, faces, points, normals = box_fixture()
        for scale in (1.,1.04):
            with self.subTest(scale=scale):
                result = self.register(transform_points(points, scale, .035, (.03,-.02,.04)), vertices, faces,
                    transform_points(normals, angle=.035), {"adjust_scale":scale != 1., "min_scale":.9,"max_scale":1.1})
                self.assertEqual(result["status"], "completed", result)
                for actual, expected in zip(apply_matrix(transform_points(points,scale,.035,(.03,-.02,.04)), result["matrix_4x4"]),points):
                    self.assertLess(math.dist(actual,expected), 2e-5)
                self.assertLess(result["final_rms"], 2e-6)
                stats = result["icp_statistics"]
                self.assertEqual(stats["registration_method"], "POINT_TO_PLANE")
                self.assertTrue(stats["normal_filter_enabled"])
                self.assertGreater(stats["point_to_plane_solve_count"], 0)
                self.assertGreaterEqual(stats["minimum_linear_rank"], 6)

    def test_same_shape_and_reversed_normals(self):
        """같은 형상과 반전 노말에서도 절댓값 내적 필터가 정합점을 보존합니다."""
        vertices, faces, points, normals = box_fixture()
        result = self.register(points, vertices, faces, [tuple(-v for v in n) for n in normals])
        self.assertEqual(result["status"], "completed", result)
        self.assertLess(result["final_rms"], 2e-6)
        self.assertEqual(result["icp_statistics"]["normal_filter_rejected_count"], 0)


    def test_plane_observable_axes_preserve_tangent_position(self):
        """무한 평면의 비관측 접선 이동은 보존하고 법선 방향 오차만 제거합니다."""
        points = [(x+.3,y-.2,.08) for x in (-.7,-.1,.4,.8) for y in (-.6,.2,.7)]
        result = self.register(points,[(-4,-4,0),(4,-4,0),(4,4,0),(-4,4,0)],[(0,1,2),(0,2,3)])
        self.assertEqual(result["status"], "completed",result)
        for actual, original in zip(apply_matrix(points,result["matrix_4x4"]),points):
            self.assertAlmostEqual(actual[0],original[0],delta=2e-6)
            self.assertAlmostEqual(actual[1],original[1],delta=2e-6)
            self.assertAlmostEqual(actual[2],0.,delta=2e-6)
        self.assertEqual(result["icp_statistics"]["minimum_linear_rank"],3)

    def test_normal_filter_rejects_incompatible_subset(self):
        """일부 직교 노말을 제외하더라도 나머지 독립 면으로 정렬합니다."""
        vertices, faces, points, normals = box_fixture()
        for index in range(0,len(normals),4):
            n = normals[index]
            normals[index] = (n[2],n[0],n[1])
        result = self.register(transform_points(points,translation=(.01,.02,.03)),vertices,faces,normals)
        self.assertEqual(result["status"],"completed",result)
        self.assertGreater(result["icp_statistics"]["normal_filter_rejected_count"],0)
        self.assertLess(result["final_rms"],2e-6)

    def test_all_normals_rejected(self):
        """모든 대응이 직교 노말로 거부되면 성공 행렬을 만들지 않습니다."""
        vertices, faces, points, normals = box_fixture()
        result = self.register(points,vertices,faces,[(n[2],n[0],n[1]) for n in normals])
        self.assertEqual(result["status"],"error",result)

    def test_scale_bound(self):
        """허용 범위 밖 상자 배율의 누적 해가 10퍼센트 경계를 넘지 않습니다."""
        vertices, faces, points, _ = box_fixture()
        for initial_scale, bound in ((1.2,.9),(.8,1.1)):
            with self.subTest(initial_scale=initial_scale):
                result = self.register(transform_points(points,scale=initial_scale),vertices,faces,
                    parameters={"adjust_scale":True,"min_scale":.9,"max_scale":1.1})
                self.assertEqual(result["status"],"completed",result)
                matrix = result["matrix_4x4"]
                scale = math.sqrt(sum(matrix[row][0]**2 for row in range(3)))
                self.assertAlmostEqual(scale,bound,delta=2e-6)
                transformed = apply_matrix(transform_points(points,scale=initial_scale),matrix)
                errors = []
                for point in transformed:
                    offsets = [abs(point[i])-extent for i,extent in enumerate((1.,1.7,2.3))]
                    errors.append(sum(max(offset,0.)**2 for offset in offsets) if max(offsets)>0 else max(offsets)**2)
                self.assertAlmostEqual(result["final_rms"],math.sqrt(sum(errors)/len(errors)),delta=2e-6)

    def test_invalid_method_and_threshold(self):
        """등록 방식과 노말 내적 임계값의 계약 밖 값을 거부합니다."""
        vertices, faces, points, normals = box_fixture()
        for options in ({"registration_method":"UNKNOWN"},{"normal_cosine_threshold":-.5},
                        {"normal_cosine_threshold":1.01},{"registration_method":"POINT_TO_POINT","normal_cosine_threshold":.5}):
            with self.subTest(options=options):
                self.assertEqual(self.register(points,vertices,faces,normals,parameters=options)["status"],"error")
        self.assertEqual(self.register(points,vertices,faces,parameters={"normal_cosine_threshold":.5})["status"],"error")

    def test_invalid_normals(self):
        """노말 길이·유한성·단위성 위반과 작업 폴더 밖 경로를 거부합니다."""
        vertices, faces, points, normals = box_fixture()
        cases = [normals[:-1], [(0.,0.,0.)]*len(points), [(float("nan"),0.,1.)]*len(points),[(0.,0.,2.)]*len(points)]
        for bad in cases:
            with self.subTest(normal=bad[0]):
                self.assertEqual(self.register(points,vertices,faces,bad)["status"],"error")

    def test_normals_path_escape(self):
        """파일이 존재해도 세션 밖 노말 경로를 허용하지 않습니다."""
        vertices, faces, points, normals = box_fixture()
        with tempfile.TemporaryDirectory(prefix="external-normals-",dir=self.root) as external:
            outside = Path(external) / "outside.bin"
            def escape(root,data):
                """별도 임시 폴더의 실제 노말 파일로 세션 밖 경로 거부를 검증합니다."""
                outside.write_bytes((root / data["normals_file"]).read_bytes())
                data["normals_file"] = f"../{outside.parent.name}/{outside.name}"
            self.assertEqual(self.register(points,vertices,faces,normals,corrupt=escape)["status"],"error")

    def test_cancel(self):
        """등록 입력을 준비한 세션의 선행 취소가 정상 취소로 종료됩니다."""
        vertices, faces, points, normals = box_fixture()
        self.assertEqual(self.register(points,vertices,faces,normals,cancel=True)["status"],"cancelled")


if __name__ == "__main__":
    unittest.main()
