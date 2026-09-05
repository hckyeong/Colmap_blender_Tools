"""저장된 Align 프로젝트로 CloudCompare 표면 정렬을 재현하고 결과 사본을 만듭니다."""
from pathlib import Path
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import shutil
import sys

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

SOURCE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SOURCE))
from tools.align.project_persistence.store import read_active_project_state, write_active_project_state
from tools.align.project_persistence.codec import encode_project_state, decode_project_state
from tools.align.display_axes import create_model_display_transform
from tools.align.alignment.types import Sim3Transform, AlignmentProvenanceRecord
from tools.align.mesh_io.obj_reader import read_obj


def write_json(path, value):
    """유한한 수치만 허용하여 실험 결과를 UTF-8 JSON으로 기록합니다."""
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def file_hash(path):
    """입력 원본과 저장 사본의 무결성을 SHA-256으로 확인합니다."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def apply(matrix, points):
    """열벡터 규약의 변환을 원본을 변경하지 않고 점 배열에 적용합니다."""
    return points @ matrix[:3, :3].T + matrix[:3, 3]


def write_obj(path, vertices, faces):
    """현재 표시 좌표의 삼각 메시를 재질 없는 OBJ 복사본으로 기록합니다."""
    with Path(path).open("w", encoding="ascii", newline="\n") as f:
        np.savetxt(f, vertices, fmt="v %.17g %.17g %.17g")
        np.savetxt(f, faces.astype(np.int64) + 1, fmt="f %d %d %d")


def sample_surface(vertices, faces, seed, count=30000):
    """삼각형 면적에 비례하는 독립 평가 표본을 고정 난수로 생성합니다."""
    triangles = vertices[faces]
    areas = np.linalg.norm(np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]), axis=1)
    if not np.isfinite(areas).all() or areas.sum() <= 0:
        raise ValueError("평가할 메시의 면적이 올바르지 않습니다.")
    rng = np.random.default_rng(seed)
    chosen = triangles[rng.choice(len(faces), size=count, p=areas / areas.sum())]
    u = np.sqrt(rng.random((count, 1)))
    v = rng.random((count, 1))
    return (1 - u) * chosen[:, 0] + u * (1 - v) * chosen[:, 1] + u * v * chosen[:, 2]


def surface_distances(points, vertices, faces):
    """Blender BVH로 표본에서 상대 메시 삼각면까지의 최단거리를 측정합니다."""
    bvh = BVHTree.FromPolygons(vertices.tolist(), faces.tolist(), all_triangles=True)
    distances = np.asarray([bvh.find_nearest(Vector(p))[3] for p in points], dtype=np.float64)
    if not np.isfinite(distances).all():
        raise ValueError("표면 거리 계산에 유효하지 않은 결과가 있습니다.")
    return distances


def statistics(distances):
    """동일 표본의 평균·RMS·백분위와 90% 절사 RMS를 계산합니다."""
    ordered = np.sort(distances)
    return dict(count=len(ordered), mean=float(np.mean(ordered)),
                rms=float(np.sqrt(np.mean(ordered ** 2))),
                trimmed90_rms=float(np.sqrt(np.mean(ordered[:int(len(ordered) * .9)] ** 2))),
                median=float(np.median(ordered)), p90=float(np.quantile(ordered, .9)),
                p95=float(np.quantile(ordered, .95)), p99=float(np.quantile(ordered, .99)),
                maximum=float(ordered[-1]))


def measure(data, delta):
    """같은 면적 표본으로 양방향 거리와 원래 높이 구간별 오차를 비교합니다."""
    moving = apply(delta, data["moving_vertices"])
    samples = apply(delta, data["moving_samples"])
    forward = surface_distances(samples, data["reference_vertices"], data["reference_faces"])
    backward = surface_distances(data["reference_samples"], moving, data["moving_faces"])
    bounds = np.quantile(data["moving_samples"][:, 2], [0, .25, .5, .75, 1])
    bands = []
    for i in range(4):
        mask = (data["moving_samples"][:, 2] >= bounds[i]) & (
            data["moving_samples"][:, 2] <= bounds[i + 1] if i == 3
            else data["moving_samples"][:, 2] < bounds[i + 1])
        bands.append(statistics(forward[mask]))
    return dict(moving_to_reference=statistics(forward),
                reference_to_moving=statistics(backward),
                symmetric=statistics(np.concatenate([forward, backward])),
                moving_height_quartiles=bands)


def prepare(output):
    """검증한 저장 상태와 현재 표시 메시를 실험 입력으로 동결합니다."""
    output.mkdir(parents=True, exist_ok=False)
    source_blend = Path(bpy.data.filepath).resolve()
    snapshot = read_active_project_state()
    if snapshot is None or len(snapshot.models) != 2:
        raise ValueError("저장된 두 컴포넌트가 필요합니다.")
    references = [m for m in snapshot.models if m.catalog.is_anchor]
    moving = [m for m in snapshot.models if not m.catalog.is_anchor]
    if len(references) != 1 or len(moving) != 1:
        raise ValueError("기준과 이동 컴포넌트를 유일하게 결정할 수 없습니다.")
    before = output / "before.blend"
    shutil.copy2(source_blend, before)
    if file_hash(before) != file_hash(source_blend):
        raise ValueError("원본 복사본 검증에 실패했습니다.")
    payload = encode_project_state(snapshot)
    (output / "project_before.json").write_text(payload, encoding="utf-8")
    arrays = {}
    info = dict(source_blend=str(source_blend), source_blend_sha256=file_hash(source_blend),
                project_uuid=snapshot.project_uuid, saved_at_utc=snapshot.saved_at_utc, models={})
    for role, model, seed in [("reference", references[0], 20260903), ("moving", moving[0], 20260904)]:
        if model.companion_source is None or model.companion_axes is None:
            raise ValueError("연결 메시와 표시 축 정보가 필요합니다.")
        path = Path(model.companion_source.path_absolute)
        if not path.is_file() or file_hash(path) != model.companion_source.source_fingerprint:
            raise ValueError("연결 메시 원본이 저장 당시와 다릅니다.")
        if not Path(model.source.path_absolute).is_dir():
            raise ValueError("COLMAP 원본 폴더를 찾을 수 없습니다.")
        axes = model.companion_axes
        display = create_model_display_transform(
            axes.preset_id, axes.axis_forward, axes.axis_up,
            rotation_xyz_degrees=axes.rotation_xyz_degrees)
        if display.fingerprint != axes.display_transform_fingerprint:
            raise ValueError("연결 메시 표시 축 지문이 다릅니다.")
        alignment = Sim3Transform(matrix_float64=model.alignment_matrix_float64)
        mesh = read_obj(path, geometry_only=True)
        world = alignment.matrix_float64 @ display.matrix_float64
        vertices = apply(world, mesh.vertices_float64)
        arrays[role + "_vertices"] = vertices
        arrays[role + "_faces"] = mesh.triangles_uint32
        arrays[role + "_samples"] = sample_surface(vertices, mesh.triangles_uint32, seed)
        write_obj(output / (role + ".obj"), vertices, mesh.triangles_uint32)
        info["models"][role] = dict(id=model.model_id, name=model.display_name, source=str(path),
            source_sha256=file_hash(path), vertex_count=mesh.vertex_count, triangle_count=mesh.triangle_count,
            alignment=alignment.matrix_float64.tolist(), mesh_axes=display.matrix_float64.tolist(),
            bounds=[vertices.min(axis=0).tolist(), vertices.max(axis=0).tolist()])
    np.savez_compressed(output / "inputs.npz", **arrays)
    write_json(output / "input_manifest.json", info)
    metrics = measure(arrays, np.eye(4))
    write_json(output / "baseline.json", metrics)
    print(json.dumps(dict(stage="입력 준비 완료", models=info["models"], baseline=metrics["symmetric"]), ensure_ascii=False))


def read_delta(path):
    """CC의 단정밀도 반올림만 검증 후 가장 가까운 양의 균일 변환으로 정규화합니다."""
    raw = np.loadtxt(path)
    if raw.shape != (4, 4) or not np.isfinite(raw).all() or not np.allclose(raw[3], [0, 0, 0, 1], atol=1e-12):
        raise ValueError("CC 결과가 유효한 4×4 행렬이 아닙니다.")
    u, singular, vt = np.linalg.svd(raw[:3, :3])
    rotation = u @ vt
    if np.linalg.det(rotation) <= 0 or singular.min() <= 0 or np.ptp(singular) / singular.mean() > 1e-5:
        raise ValueError("반사 또는 비균일 스케일 결과를 거부했습니다.")
    delta = raw.copy()
    delta[:3, :3] = singular.mean() * rotation
    correction = float(np.max(np.abs(raw - delta)))
    if correction > 1e-6:
        raise ValueError("CC 결과의 반올림 보정 범위를 초과했습니다.")
    transform = Sim3Transform(matrix_float64=delta)
    return transform, correction


def evaluate(output):
    """두 정렬 후보를 동일한 독립 표본으로 평가하고 적용 행렬을 검증합니다."""
    data = dict(np.load(output / "inputs.npz", allow_pickle=False))
    manifest = json.loads((output / "input_manifest.json").read_text(encoding="utf-8"))
    baseline = json.loads((output / "baseline.json").read_text(encoding="utf-8"))
    results = {}
    for label in ("rigid", "similarity"):
        folder = output / label
        transform, correction = read_delta(folder / "moving_REGISTRATION_MATRIX.txt")
        delta = transform.matrix_float64
        metrics = measure(data, delta)
        saved_mesh = read_obj(folder / "moving_REGISTERED.obj", geometry_only=True)
        expected = apply(delta, data["moving_vertices"])
        if saved_mesh.vertex_count != len(expected):
            raise ValueError("CC 결과의 정점 수가 입력과 다릅니다.")
        deviation = float(np.max(np.linalg.norm(expected - saved_mesh.vertices_float64, axis=1)))
        if deviation > 1e-4:
            raise ValueError("CC 출력 메시와 행렬의 변환 결과가 일치하지 않습니다.")
        composed = Sim3Transform(matrix_float64=delta @ np.asarray(manifest["models"]["moving"]["alignment"]))
        results[label] = dict(delta=delta.tolist(), alignment=composed.matrix_float64.tolist(),
            delta_scale=transform.uniform_scale, float_projection_max=correction,
            cc_mesh_max_deviation=deviation, metrics=metrics)
    selected = min(results, key=lambda k: results[k]["metrics"]["symmetric"]["trimmed90_rms"])
    before_score = baseline["symmetric"]["trimmed90_rms"]
    after_score = results[selected]["metrics"]["symmetric"]["trimmed90_rms"]
    if after_score >= before_score:
        raise ValueError("비교 지표가 개선되지 않아 자동 적용을 중단했습니다.")
    report = dict(baseline=baseline, candidates=results, selected=selected,
                  trimmed90_improvement_percent=100 * (1 - after_score / before_score))
    write_json(output / "evaluation.json", report)
    print(json.dumps(report, ensure_ascii=False))



def publish(output):
    """검증된 공통 정렬을 새 프로젝트 사본에 기록하고 원본 불변성을 확인합니다."""
    manifest = json.loads((output / "input_manifest.json").read_text(encoding="utf-8"))
    report = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    original = Path(manifest["source_blend"])
    if file_hash(original) != manifest["source_blend_sha256"]:
        raise ValueError("실험 중 원본 저장 파일이 바뀌어 적용을 중단했습니다.")
    snapshot = read_active_project_state()
    expected = decode_project_state((output / "project_before.json").read_text(encoding="utf-8"))
    if snapshot is None or encode_project_state(snapshot) != encode_project_state(expected):
        raise ValueError("현재 연 프로젝트가 실험 시작 상태와 다릅니다.")
    selected = report["candidates"][report["selected"]]
    moving_id = manifest["models"]["moving"]["id"]
    final = Sim3Transform(matrix_float64=selected["alignment"])
    provenance = AlignmentProvenanceRecord(
        model_id=moving_id, method="ICP", backend_id="CLOUDCOMPARE_SURFACE",
        algorithm_version="CloudCompare-2.13.2-mesh-ICP",
        input_fingerprint=manifest["source_blend_sha256"],
        result_fingerprint=final.fingerprint,
        icp_rmse=selected["metrics"]["symmetric"]["trimmed90_rms"],
        icp_status="PASS",
        marker_geometry_dependencies=tuple((m.model_id, "COMPANION") for m in snapshot.models))
    models = []
    for model in snapshot.models:
        if model.model_id == moving_id:
            model = replace(model, alignment_matrix_float64=tuple(tuple(float(v) for v in row) for row in final.matrix_float64),
                catalog=replace(model.catalog, registration_status="REGISTERED", alignment_provenance=provenance,
                                is_visible=True, show_surface=True, show_points=False))
        models.append(model)
    result = replace(snapshot, models=tuple(models), payload_fingerprint="",
                     saved_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    result = decode_project_state(encode_project_state(result))
    write_active_project_state(result)
    if encode_project_state(read_active_project_state()) != encode_project_state(result):
        raise ValueError("결과 저장 상태를 다시 읽은 값이 다릅니다.")
    # 공통 정렬과 원본 COLMAP에서 앵커 COLMAP으로 가는 행렬을 구분하여 보관합니다.
    reference = next(m for m in result.models if m.catalog.is_anchor)
    moving = next(m for m in result.models if m.model_id == moving_id)
    axes = []
    for model in (reference, moving):
        a = model.current_axes
        axes.append(create_model_display_transform(a.preset_id, a.axis_forward, a.axis_up,
                    rotation_xyz_degrees=a.rotation_xyz_degrees).matrix_float64)
    np.savetxt(output / "moving_alignment_final.txt", final.matrix_float64, fmt="%.17g")
    np.savetxt(output / "surface_delta.txt", np.asarray(selected["delta"]), fmt="%.17g")
    np.savetxt(output / "colmap_source_to_anchor.txt", np.linalg.inv(axes[0]) @ final.matrix_float64 @ axes[1], fmt="%.17g")
    destination = output / "surface_aligned.blend"
    if destination.exists():
        raise FileExistsError("기존 결과 파일을 자동 덮어쓰지 않습니다.")
    bpy.ops.wm.save_as_mainfile(filepath=str(destination), check_existing=False)
    (output / "project_after.json").write_text(encode_project_state(result), encoding="utf-8")
    if file_hash(original) != manifest["source_blend_sha256"]:
        raise ValueError("원본 저장 파일 지문이 달라졌습니다.")
    write_json(output / "published.json", dict(result_blend=str(destination), before_blend=str(output / "before.blend"),
        source_unchanged=True, moving_model_id=moving_id, selected=report["selected"]))
    print("표면 정렬 적용 사본 저장 완료: " + str(destination))



def verify_saved(output):
    """결과 사본을 다시 복원하여 점군·카메라·메시의 공통 변환을 검증합니다."""
    import time
    from tools.align.state import RuntimeState
    from tools.align.project_persistence.runtime import schedule_project_restore_if_available, _restore_timer
    from tools.align.merge_export.transforms import transform_cam_from_world, pose_to_rotation_translation
    runtime = RuntimeState()
    assert schedule_project_restore_if_available(runtime), "저장 사본 복원을 시작하지 못했습니다."
    deadline = time.monotonic() + 120
    while runtime.project_restore.phase == "RESTORING":
        if time.monotonic() > deadline:
            raise RuntimeError("결과 사본 검증 시간 초과")
        _restore_timer(runtime)
    assert runtime.project_restore.phase == "IDLE", runtime.project_restore.last_message
    manifest = json.loads((output / "input_manifest.json").read_text(encoding="utf-8"))
    evaluation = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    chosen = evaluation["candidates"][evaluation["selected"]]
    repository = runtime.model_repository
    delta = np.asarray(chosen["delta"])
    data = dict(np.load(output / "inputs.npz", allow_pickle=False))
    models = {}
    for role in ("reference", "moving"):
        model_id = manifest["models"][role]["id"]
        model = repository.models[model_id]
        mesh = repository.companion_meshes[model_id]
        alignment = repository.alignment_transform_for_model_id(model_id).matrix_float64
        expected_alignment = np.asarray(chosen["alignment"] if role == "moving" else manifest["models"][role]["alignment"])
        assert np.allclose(alignment, expected_alignment, rtol=0, atol=1e-12)
        mesh_axes = repository.companion_display_transform_for_model_id(model_id).matrix_float64
        actual_mesh = apply(alignment @ mesh_axes, mesh.vertices_float64)
        expected_mesh = apply(delta, data[role + "_vertices"]) if role == "moving" else data[role + "_vertices"]
        assert np.allclose(actual_mesh, expected_mesh, rtol=0, atol=1e-10)
        main_axes = repository.display_transform_for_model_id(model_id).matrix_float64
        complete = Sim3Transform(matrix_float64=alignment @ main_axes)
        points = model.points.positions[::max(1, model.points.count // 100)][:100]
        transformed = apply(complete.matrix_float64, points)
        maximum = 0.
        for image in model.images.image_records.values():
            old_r, old_t = pose_to_rotation_translation(image.cam_from_world)
            new_r, new_t = pose_to_rotation_translation(transform_cam_from_world(image.cam_from_world, complete))
            old_camera = points @ old_r.T + old_t
            new_camera = transformed @ new_r.T + new_t
            error = float(np.max(np.abs(new_camera - complete.uniform_scale * old_camera)))
            maximum = max(maximum, error)
        assert maximum < 1e-8, "점군과 카메라의 공통 변환이 영상 투영 관계를 보존하지 않습니다."
        models[role] = dict(points=model.points.count, cameras=model.images.count,
            mesh_vertices=mesh.vertex_count, mesh_triangles=mesh.face_count,
            camera_coordinate_invariance_max=maximum,
            mesh_alignment_max=float(np.max(np.abs(actual_mesh - expected_mesh))))
    assert file_hash(manifest["source_blend"]) == manifest["source_blend_sha256"]
    write_json(output / "saved_result_verified.json", dict(passed=True, models=models, source_unchanged=True))
    print(json.dumps(dict(passed=True, models=models, source_unchanged=True), ensure_ascii=False))


def main():
    """명시한 단계만 실행하고 출력 경로가 실험 outputs 아래인지 확인합니다."""
    args = sys.argv[sys.argv.index("--") + 1:]
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("prepare", "evaluate", "publish", "verify"))
    parser.add_argument("output")
    options = parser.parse_args(args)
    output = Path(options.output).resolve()
    output.relative_to(SOURCE.parent / "outputs")
    if options.stage == "prepare":
        prepare(output)
    elif options.stage == "evaluate":
        evaluate(output)
    elif options.stage == "publish":
        publish(output)
    else:
        verify_saved(output)


if __name__ == "__main__":
    main()
