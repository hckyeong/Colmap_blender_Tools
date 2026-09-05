"""보존된 실데이터에서 Small GICP와 CCCoreLib를 독립 프로세스로 비교합니다."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import ctypes
from ctypes import wintypes
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import time

import bpy
import numpy as np

SOURCE = Path(__file__).resolve().parents[3]
ENVIRONMENT = SOURCE.parent
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cloudcompare_surface import apply, file_hash, measure, sample_surface, write_obj
from tools.align.alignment.types import Sim3Transform
from tools.align.project_persistence.codec import encode_project_state
from tools.align.project_persistence.store import read_active_project_state, write_active_project_state

REFERENCE = "d32505aea1c84e2284d99a8733bd8440"
MOVING = "ff9b2c58e43e458c819950823fe73fff"
FIXTURE = ENVIRONMENT / "outputs/cloudcompare_surface_20260903_run01"
EVALUATION_SEEDS = (9173001, 9173002)


def plain(value):
    """검증 결과를 유한한 JSON 기본 자료형으로 변환합니다."""
    if isinstance(value, Mapping): return {str(k): plain(v) for k,v in value.items()}
    if isinstance(value, (list,tuple)): return [plain(v) for v in value]
    if isinstance(value, np.ndarray): return value.tolist()
    if isinstance(value, np.generic): return value.item()
    if isinstance(value, Path): return str(value)
    if hasattr(value, "value"): return plain(value.value)
    return value


def write_new_json(path, value):
    """기존 실험 산출물을 덮어쓰지 않고 UTF-8 JSON을 새로 기록합니다."""
    with Path(path).open("x",encoding="utf-8") as stream:
        json.dump(plain(value),stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.write("\n")


def restore_runtime():
    """현재 열린 보존 파일의 확정 상태를 새로운 RuntimeState로 복원합니다."""
    from tools.align.state import RuntimeState
    from tools.align.project_persistence.runtime import schedule_project_restore_if_available, _restore_timer
    runtime=RuntimeState()
    started=time.monotonic()
    if not schedule_project_restore_if_available(runtime):
        raise RuntimeError(runtime.project_restore.last_message or "저장 상태 복원을 시작하지 못했습니다.")
    while runtime.project_restore.phase=="RESTORING":
        if time.monotonic()-started>240: raise TimeoutError("저장 상태 복원 시간 제한을 넘었습니다.")
        _restore_timer(runtime)
        time.sleep(.01)
    if runtime.project_restore.phase!="IDLE": raise RuntimeError(runtime.project_restore.last_message)
    runtime.session_active=True
    return runtime,time.monotonic()-started


def prepare_runtime(runtime, backend):
    """저장 C를 유지하며 완료 마커를 정리하고 공개 카탈로그 API로 동일 A/P를 선택합니다."""
    runtime.marker_selection.clear()
    runtime.marker_alignment.clear()
    runtime.icp_alignment.clear()
    catalog=runtime.model_catalog
    if catalog.models[MOVING].registration_status=="REGISTERED":
        assert catalog.transfer_model_group(MOVING,source="MANUAL",has_marker_draft=False,completed_pair_count=0),catalog.message
    assert catalog.models[REFERENCE].is_anchor and catalog.models[REFERENCE].registration_status=="REGISTERED"
    catalog.clear_selected_model()
    assert catalog.select_model(MOVING,has_marker_draft=False,completed_pair_count=0),catalog.message
    assert catalog.toggle_alignment_selection(REFERENCE,has_marker_draft=False,completed_pair_count=0),catalog.message
    for model_id in (REFERENCE,MOVING):
        assert catalog.set_model_visibility(model_id,True)
        assert catalog.set_geometry_visibility(model_id,show_points=backend=="SMALL_GICP",show_surface=backend=="CCCORELIB")
    assert catalog.selected_reference_model_id==REFERENCE and catalog.selected_moving_model_id==MOVING
    assert not runtime.marker_selection.completed_pairs and runtime.marker_alignment.result is None


class MemoryCounters(ctypes.Structure):
    """공식 PROCESS_MEMORY_COUNTERS_EX 메모리 배치를 선언합니다."""
    _fields_=[("cb",wintypes.DWORD),("PageFaultCount",wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in
        ("PeakWorkingSetSize","WorkingSetSize","QuotaPeakPagedPoolUsage","QuotaPagedPoolUsage",
         "QuotaPeakNonPagedPoolUsage","QuotaNonPagedPoolUsage","PagefileUsage","PeakPagefileUsage","PrivateUsage")]


class ProcessEntry(ctypes.Structure):
    """공식 PROCESSENTRY32W 배치로 작업 프로세스의 자식만 식별합니다."""
    _fields_=[("dwSize",wintypes.DWORD),("cntUsage",wintypes.DWORD),("pid",wintypes.DWORD),
        ("heap",ctypes.c_size_t),("module",wintypes.DWORD),("threads",wintypes.DWORD),
        ("parent",wintypes.DWORD),("priority",wintypes.LONG),("flags",wintypes.DWORD),("exe",wintypes.WCHAR*260)]


class MemoryMonitor:
    """Windows 공식 API의 관측 메모리와 프로세스별 최고 작업 집합을 기록합니다."""
    def __init__(self):
        """메모리와 프로세스 조회용 읽기 전용 API를 연결합니다."""
        self.kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        self.kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
        self.kernel.OpenProcess.restype=wintypes.HANDLE
        self.kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
        self.kernel.K32GetProcessMemoryInfo.argtypes=(wintypes.HANDLE,ctypes.POINTER(MemoryCounters),wintypes.DWORD)
        self.kernel.CreateToolhelp32Snapshot.argtypes=(wintypes.DWORD,wintypes.DWORD)
        self.kernel.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
        for name in ("Process32FirstW","Process32NextW"):
            getattr(self.kernel,name).argtypes=(wintypes.HANDLE,ctypes.POINTER(ProcessEntry))
        self.processes={}
        self.observed_peak_sum=0
        self.samples=0
        self.errors={}

    def read(self,pid):
        """지정 PID의 실제 작업 집합과 최고 커밋 사용량을 조회합니다."""
        handle=self.kernel.OpenProcess(0x410,False,pid)
        if not handle:
            self.errors[str(pid)]=ctypes.get_last_error()
            return None
        try:
            counters=MemoryCounters();counters.cb=ctypes.sizeof(counters)
            if not self.kernel.K32GetProcessMemoryInfo(handle,ctypes.byref(counters),counters.cb):
                self.errors[str(pid)]=ctypes.get_last_error()
                return None
            return {"working_set_bytes":int(counters.WorkingSetSize),"peak_working_set_bytes":int(counters.PeakWorkingSetSize),
                    "private_bytes":int(counters.PrivateUsage),"peak_commit_bytes":int(counters.PeakPagefileUsage)}
        finally:self.kernel.CloseHandle(handle)

    def sample(self,root_pid):
        """조정자와 관측된 자식 프로세스만 0.1초 간격으로 함께 측정합니다."""
        snapshot=self.kernel.CreateToolhelp32Snapshot(2,0)
        if snapshot==ctypes.c_void_p(-1).value:
            self.errors["snapshot"]=ctypes.get_last_error();return
        table={}
        try:
            entry=ProcessEntry();entry.dwSize=ctypes.sizeof(entry)
            valid=self.kernel.Process32FirstW(snapshot,ctypes.byref(entry))
            while valid:
                table[int(entry.pid)]=(int(entry.parent),str(entry.exe))
                valid=self.kernel.Process32NextW(snapshot,ctypes.byref(entry))
        finally:self.kernel.CloseHandle(snapshot)
        descendants={root_pid}
        while True:
            added={pid for pid,(parent,_) in table.items() if parent in descendants}
            if added.issubset(descendants):break
            descendants.update(added)
        current_sum=0
        for pid in descendants:
            counters=self.read(pid)
            if counters is None:continue
            current_sum+=counters["working_set_bytes"]
            previous=self.processes.get(str(pid),{})
            self.processes[str(pid)]={"name":table.get(pid,(0,"종료된 작업"))[1],**{name:max(previous.get(name,0),value) for name,value in counters.items()}}
        self.observed_peak_sum=max(self.observed_peak_sum,current_sum)
        self.samples+=1

    def report(self):
        """표본 최고값과 조회 실패를 구분하고 측정 범위를 명시합니다."""
        return {"method":"Windows GetProcessMemoryInfo(PROCESS_MEMORY_COUNTERS_EX)","sampling_interval_seconds":.1,
                "samples":self.samples,"observed_worker_tree_peak_working_set_bytes":self.observed_peak_sum,
                "processes":self.processes,"read_errors":self.errors,
                "limitation":"조정자 시작 후 0.1초 표본에서 관측한 작업 집합 합계입니다. 짧은 자식 프로세스를 놓칠 수 있으며 정확한 동시 최고 메모리로 추정하지 않습니다.",
                "source":"https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters_ex"}


def prepare(output):
    """보존 사본과 같은 표시 표면을 독립 평가 입력으로 고정합니다."""
    output.mkdir(parents=True,exist_ok=False)
    original=FIXTURE/"before.blend"
    shutil.copy2(original,output/"before.blend")
    assert file_hash(original)==file_hash(output/"before.blend")
    snapshot=read_active_project_state()
    assert snapshot is not None
    saved={model.model_id:model for model in snapshot.models}
    old_manifest=json.loads((FIXTURE/"input_manifest.json").read_text(encoding="utf-8"))
    arrays=dict(np.load(FIXTURE/"inputs.npz",allow_pickle=False))
    for role,model_id,seed in (("reference",REFERENCE,EVALUATION_SEEDS[0]),("moving",MOVING,EVALUATION_SEEDS[1])):
        np.testing.assert_allclose(saved[model_id].alignment_matrix_float64,old_manifest["models"][role]["alignment"],atol=1e-12,rtol=0)
        arrays[role+"_samples"]=sample_surface(arrays[role+"_vertices"],arrays[role+"_faces"],seed,count=30000)
    np.savez_compressed(output/"evaluation_inputs.npz",**arrays)
    (output/"project_before.json").write_text(encode_project_state(snapshot),encoding="utf-8")
    manifest={"created_utc":datetime.now(timezone.utc).isoformat(),"fixture_blend":str(original),"fixture_sha256":file_hash(original),
        "fixture_manifest_sha256":file_hash(FIXTURE/"input_manifest.json"),"fixture_arrays_sha256":file_hash(FIXTURE/"inputs.npz"),
        "evaluation_inputs_sha256":file_hash(output/"evaluation_inputs.npz"),"evaluation_seeds":EVALUATION_SEEDS,"evaluation_count_per_surface":30000,
        "cc_optimizer_seed":1729,"cc_internal_evaluation_seed":104729,"reference_id":REFERENCE,"moving_id":MOVING,"models":old_manifest["models"],
        "comparison_contract":"독립 Blender 프로세스의 새 RuntimeState에서 같은 저장 C를 복원합니다. Small GICP는 점군, CCCoreLib는 연결 메시 표면을 입력합니다."}
    write_new_json(output/"input_manifest.json",manifest)
    write_new_json(output/"baseline.json",measure(arrays,np.eye(4)))
    print("독립 실데이터 비교 입력 준비 완료",flush=True)


def capture_job_records(directory,destination):
    """작업 정리 전에 생성된 JSON과 최종 행렬만 실험 증거로 복사합니다."""
    if not directory.is_dir() or destination.exists():return
    destination.mkdir()
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix.lower() in (".json",".txt"):
            target=destination/path.relative_to(directory)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,target)


def run_engine(output,backend):
    """공개 작업 시작·완료 경계를 사용하고 같은 독립 표면 표본으로 결과를 평가합니다."""
    from tools.align.alignment.icp_runtime import begin_icp_job,poll_icp_job,approve_icp_result,request_icp_cancel
    from tools.align.project_persistence.runtime import build_project_state_snapshot
    label="cccore" if backend=="CCCORELIB" else "small_gicp"
    ordinal=1
    while (output/f"{label}_attempt{ordinal:02d}").exists():ordinal+=1
    attempt=output/f"{label}_attempt{ordinal:02d}";attempt.mkdir()
    manifest=json.loads((output/"input_manifest.json").read_text(encoding="utf-8"))
    report={"backend":backend,"blender_pid":os.getpid(),"attempt_directory":str(attempt),"status":"failed"}
    handle=None;runtime=None;monitor=MemoryMonitor()
    try:
        runtime,restore_seconds=restore_runtime()
        prepare_runtime(runtime,backend)
        repository=runtime.model_repository
        before={model_id:repository.alignment_transform_for_model_id(model_id).matrix_float64.copy() for model_id in (REFERENCE,MOVING)}
        for role,model_id in (("reference",REFERENCE),("moving",MOVING)):
            np.testing.assert_allclose(before[model_id],manifest["models"][role]["alignment"],rtol=0,atol=1e-12)
        report.update(restore_seconds=restore_seconds,alignment_before=before,marker_pairs=0,marker_preview=False,
                      blender_memory_before=monitor.read(os.getpid()),options={"preset":"PRECISE","scale_refine_enabled":True,
                      "solver_backend":backend,"python":str(ENVIRONMENT/"runtime/python/python.exe"),"geometry":"mesh_surface" if backend=="CCCORELIB" else "colmap_points"})
        started=time.monotonic()
        handle=begin_icp_job(runtime,preset="PRECISE",scale_refine_enabled=True,solver_backend=backend,open3d_python_path=str(ENVIRONMENT/"runtime/python/python.exe"))
        report["input_parameters"]=plain(runtime.icp_alignment.input_data.parameters.to_mapping())
        report["initial_seed_alignment"]=runtime.icp_alignment.input_data.seed.initial_alignment_float64.tolist()
        report["input_fingerprint"]=handle.input_fingerprint
        last_notice=last_memory=0.
        while runtime.icp_alignment.is_running:
            now=time.monotonic()
            if now-last_memory>=.1:monitor.sample(handle.process.pid);last_memory=now
            if handle.process.poll() is not None:capture_job_records(handle.files.job_directory,attempt/"job_records")
            poll_icp_job(runtime)
            if now-last_notice>=10:
                progress=runtime.icp_alignment.progress
                print(f"{backend} 실행 {now-started:.1f}초: {getattr(progress,'message','')}",flush=True);last_notice=now
            if now-started>1800:
                request_icp_cancel(runtime)
                raise TimeoutError("정렬 작업이 30분 제한을 넘었습니다.")
            time.sleep(.02)
        report["elapsed_seconds"]=time.monotonic()-started
        report["memory"]=monitor.report()
        report["blender_memory_after"]=monitor.read(os.getpid())
        if not runtime.icp_alignment.has_preview:raise RuntimeError(runtime.icp_alignment.last_error or "유효한 미리보기가 없습니다.")
        result=runtime.icp_alignment.result
        arrays=dict(np.load(output/"evaluation_inputs.npz",allow_pickle=False))
        np.testing.assert_allclose(repository.alignment_transform_for_model_id(MOVING).matrix_float64,before[MOVING],atol=1e-12,rtol=0)
        final=result.final_alignment.matrix_float64
        delta=final@np.linalg.inv(before[MOVING])
        np.testing.assert_allclose(delta,result.icp_delta.matrix_float64,atol=1e-9,rtol=0)
        evaluation_started=time.monotonic()
        metrics=measure(arrays,delta)
        report.update(status="completed",final_alignment=final,delta=delta,delta_scale=Sim3Transform(matrix_float64=delta).uniform_scale,
                      final_scale=result.final_alignment.uniform_scale,quality=plain(result.quality),quality_status=result.quality_status,
                      requires_review=result.quality_status=="REVIEW",is_approvable=result.is_approvable,engine_metadata=plain(result.metadata),
                      evaluation=metrics,evaluation_seconds=time.monotonic()-evaluation_started,
                      source_counts={model_id:{"points":repository.models[model_id].points.count,"images":repository.models[model_id].images.count,
                      "mesh_vertices":repository.companion_meshes[model_id].vertex_count,"mesh_triangles":repository.companion_meshes[model_id].face_count} for model_id in (REFERENCE,MOVING)})
        if backend=="CCCORELIB":
            commit=approve_icp_result(runtime)
            np.testing.assert_allclose(repository.alignment_transform_for_model_id(MOVING).matrix_float64,final,atol=1e-12,rtol=0)
            snapshot=build_project_state_snapshot(runtime)
            write_active_project_state(snapshot)
            assert encode_project_state(read_active_project_state())==encode_project_state(snapshot)
            (attempt/"project_after.json").write_text(encode_project_state(snapshot),encoding="utf-8")
            destination=attempt/"after.blend"
            assert not destination.exists()
            bpy.ops.wm.save_as_mainfile(filepath=str(destination),check_existing=False)
            report["approval"]={"public_api":"approve_icp_result","result_blend":str(destination),"snapshot_sha256":file_hash(attempt/"project_after.json"),
                                "registration_status":runtime.model_catalog.models[MOVING].registration_status,
                                "backend_provenance":runtime.model_catalog.models[MOVING].alignment_provenance.backend_id}
        assert file_hash(FIXTURE/"before.blend")==manifest["fixture_sha256"]
        report["fixture_unchanged"]=True
    except Exception as error:
        report.update(status="failed",error_type=type(error).__name__,error=str(error),memory=monitor.report())
        if handle is not None:capture_job_records(handle.files.job_directory,attempt/"job_records")
        print(f"{backend} 실험 실패: {error}",flush=True)
    finally:
        write_new_json(attempt/"result.json",report)
    print(json.dumps({"engine":backend,"status":report["status"],"result":str(attempt/"result.json")},ensure_ascii=False),flush=True)


def bake_and_reload_model(runtime,model_id,output):
    """최종 C로 실제 COLMAP 5파일을 내보내고 다시 읽어 점·카메라·연결 메시를 확인합니다."""
    from tools.align.colmap_io import load_sparse_model
    from tools.align.colmap_io.binary_writer import write_binary_model
    from tools.align.merge_export.transforms import (compose_anchor_colmap_output_transform,transform_image_record,transform_rig_records,
        transform_frame_records,normalize_legacy_rigs_and_frames,pose_to_rotation_translation)
    from tools.align.mesh_io.obj_reader import read_obj
    repository=runtime.model_repository;model=repository.models[model_id]
    alignment=repository.alignment_transform_for_model_id(model_id)
    axes=repository.display_transform_for_model_id(model_id).matrix_float64
    anchor_axes=repository.display_transform_for_model_id(REFERENCE).matrix_float64
    transform=compose_anchor_colmap_output_transform(alignment,axes,anchor_axes)
    positions=apply(transform.matrix_float64,model.points.positions)
    images=replace(model.images,image_records={key:transform_image_record(value,transform) for key,value in model.images.image_records.items()})
    if model.rigs:
        rigs=transform_rig_records(model.rigs,transform);frames=transform_frame_records(model.frames,transform)
    else:
        legacy_rigs,frames=normalize_legacy_rigs_and_frames(model,transform=transform)
        rigs=transform_rig_records(legacy_rigs,transform)
    baked=replace(model,points=replace(model.points,positions=positions),images=images,rigs=rigs,frames=frames,
                  bounds_min=positions.min(axis=0),bounds_max=positions.max(axis=0))
    destination=output/model_id/"sparse/0"
    assert not destination.exists()
    written=write_binary_model(baked,destination)
    reloaded=load_sparse_model(destination)
    order=np.argsort(model.points.point_ids)
    np.testing.assert_array_equal(reloaded.points.point_ids,model.points.point_ids[order])
    np.testing.assert_allclose(reloaded.points.positions,positions[order],rtol=0,atol=1e-12)
    sample_indices=np.arange(0,model.points.count,max(1,model.points.count//256))[:256]
    source_points=model.points.positions[sample_indices];transformed_points=positions[sample_indices]
    max_camera_error=0.;max_projection_error=0.;projection_count=0
    for image_id,image in model.images.image_records.items():
        old_rotation,old_translation=pose_to_rotation_translation(image.cam_from_world)
        new_rotation,new_translation=pose_to_rotation_translation(reloaded.images.image_records[image_id].cam_from_world)
        old_camera=source_points@old_rotation.T+old_translation
        new_camera=transformed_points@new_rotation.T+new_translation
        max_camera_error=max(max_camera_error,float(np.max(np.abs(new_camera-transform.uniform_scale*old_camera))))
        valid=np.abs(old_camera[:,2])>1e-3
        if np.any(valid):
            old_xy=old_camera[valid,:2]/old_camera[valid,2:]
            new_xy=new_camera[valid,:2]/new_camera[valid,2:]
            np.testing.assert_allclose(new_xy,old_xy,rtol=1e-9,atol=1e-7)
            max_projection_error=max(max_projection_error,float(np.max(np.abs(new_xy-old_xy))))
            projection_count+=int(np.count_nonzero(valid))
    assert max_camera_error<1e-8
    for camera_id,camera in model.cameras.items():
        np.testing.assert_array_equal(camera.params_float64,reloaded.cameras[camera_id].params_float64)
    mesh=repository.companion_meshes[model_id]
    mesh_axes=repository.companion_display_transform_for_model_id(model_id).matrix_float64
    mesh_transform=np.linalg.inv(anchor_axes)@alignment.matrix_float64@mesh_axes
    mesh_vertices=apply(mesh_transform,mesh.vertices_float64)
    obj_path=output/model_id/"companion.obj"
    write_obj(obj_path,mesh_vertices,mesh.triangles_uint32)
    mesh_reloaded=read_obj(obj_path,geometry_only=True)
    np.testing.assert_allclose(mesh_reloaded.vertices_float64,mesh_vertices,atol=1e-12,rtol=0)
    return {"passed":True,"points":model.points.count,"images":model.images.count,"exported_files":{name:file_hash(path) for name,path in written.items()},
            "source_to_anchor":transform.matrix_float64,"point_reload_max_error":float(np.max(np.abs(reloaded.points.positions-positions[order]))),
            "camera_coordinate_invariance_max":max_camera_error,"normalized_projection_max_error":max_projection_error,"projection_checks":projection_count,
            "camera_intrinsics_unchanged":True,"mesh_reload_max_error":float(np.max(np.abs(mesh_reloaded.vertices_float64-mesh_vertices)))}


def verify(output):
    """새 Blender에서 승인 사본을 복원하고 공통 D 및 실제 내보내기 왕복을 검증합니다."""
    current=Path(bpy.data.filepath).resolve().parent
    result=json.loads((current/"result.json").read_text(encoding="utf-8"))
    assert result["status"]=="completed" and result["backend"]=="CCCORELIB"
    manifest=json.loads((output/"input_manifest.json").read_text(encoding="utf-8"))
    runtime,restore_seconds=restore_runtime()
    snapshot=read_active_project_state()
    assert encode_project_state(snapshot)==(current/"project_after.json").read_text(encoding="utf-8")
    np.testing.assert_allclose(runtime.model_repository.alignment_transform_for_model_id(MOVING).matrix_float64,result["final_alignment"],atol=1e-12,rtol=0)
    assert runtime.model_catalog.models[MOVING].alignment_provenance.backend_id=="CCCORELIB"
    arrays=dict(np.load(output/"evaluation_inputs.npz",allow_pickle=False))
    common={}
    for role,model_id in (("reference",REFERENCE),("moving",MOVING)):
        repository=runtime.model_repository
        after=repository.alignment_transform_for_model_id(model_id).matrix_float64
        before=np.asarray(manifest["models"][role]["alignment"])
        delta=np.asarray(result["delta"]) if role=="moving" else np.eye(4)
        np.testing.assert_allclose(after,delta@before,atol=1e-10,rtol=0)
        mesh=repository.companion_meshes[model_id]
        mesh_axes=repository.companion_display_transform_for_model_id(model_id).matrix_float64
        restored=apply(after@mesh_axes,mesh.vertices_float64)
        expected=apply(delta,arrays[role+"_vertices"])
        np.testing.assert_allclose(restored,expected,atol=1e-10,rtol=0)
        common[role]={"alignment":after,"delta":delta,"mesh_common_delta_max_error":float(np.max(np.abs(restored-expected)))}
    export_root=current/"colmap_roundtrip"
    export_root.mkdir(exist_ok=False)
    exports={role:bake_and_reload_model(runtime,model_id,export_root) for role,model_id in (("reference",REFERENCE),("moving",MOVING))}
    assert file_hash(FIXTURE/"before.blend")==manifest["fixture_sha256"]
    report={"passed":True,"separate_blender_pid":os.getpid(),"restore_seconds":restore_seconds,"common_delta":common,"exports":exports,"fixture_unchanged":True}
    write_new_json(current/"reopened_and_export_verified.json",report)
    print("별도 Blender 재열기와 실제 COLMAP·연결 메시 왕복 검증 완료",flush=True)


def summarize(output):
    """성공과 실패 시도를 모두 보존해 최종 비교 요약을 새로 기록합니다."""
    manifest=json.loads((output/"input_manifest.json").read_text(encoding="utf-8"))
    baseline=json.loads((output/"baseline.json").read_text(encoding="utf-8"))
    attempts=[json.loads(path.read_text(encoding="utf-8")) for path in sorted(output.glob("*_attempt*/result.json"))]
    context_path=output/"measurement_context.json"
    context=json.loads(context_path.read_text(encoding="utf-8")) if context_path.exists() else {"concurrent_load":"동시 부하 조건을 별도로 기록하지 않았습니다."}
    report={"input":manifest,"baseline":baseline,"attempts":attempts,"comparison":{},"saved_and_reloaded":[],"measurement_context":context}
    for attempt in attempts:
        if attempt["status"]!="completed":continue
        score=attempt["evaluation"]["symmetric"]["trimmed90_rms"]
        report["comparison"][attempt["backend"]]={"attempt_directory":attempt["attempt_directory"],"elapsed_seconds":attempt["elapsed_seconds"],"delta_scale":attempt["delta_scale"],
            "symmetric_trimmed90_rms":score,"symmetric_rms":attempt["evaluation"]["symmetric"]["rms"],
            "improvement_percent":100*(1-score/baseline["symmetric"]["trimmed90_rms"]),"quality_status":attempt["quality_status"],
            "observed_worker_tree_peak_working_set_bytes":attempt["memory"]["observed_worker_tree_peak_working_set_bytes"]}
    for path in output.glob("cccore_attempt*/reopened_and_export_verified.json"):
        report["saved_and_reloaded"].append(json.loads(path.read_text(encoding="utf-8")))
    write_new_json(output/"comparison.json",report)
    print(json.dumps(report["comparison"],ensure_ascii=False),flush=True)


def main():
    """명시한 실험 단계만 실행하고 새 출력 디렉터리의 경계를 확인합니다."""
    parser=argparse.ArgumentParser()
    parser.add_argument("stage",choices=("prepare","cccore","small","verify","summarize"))
    parser.add_argument("output")
    options=parser.parse_args(sys.argv[sys.argv.index("--")+1:])
    output=Path(options.output).resolve()
    output.relative_to(ENVIRONMENT/"outputs")
    assert output.name.startswith("cccore_engine_20260903_run")
    if options.stage=="prepare":prepare(output)
    elif options.stage=="cccore":run_engine(output,"CCCORELIB")
    elif options.stage=="small":run_engine(output,"SMALL_GICP")
    elif options.stage=="verify":verify(output)
    else:summarize(output)


if __name__=="__main__":
    main()
