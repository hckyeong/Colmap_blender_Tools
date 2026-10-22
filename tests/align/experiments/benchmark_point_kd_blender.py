"""사용자 장면을 저장하지 않고 같은 두 메시의 엔진과 수렴 설정을 비교합니다."""

from collections.abc import Mapping
import argparse
import ctypes
from ctypes import wintypes
import hashlib
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
import json
import logging
import os
from pathlib import Path
import sys
import time

import bpy
import numpy as np


def plain(value):
    """불변 매핑과 NumPy 값을 결과 JSON의 기본 자료형으로 변환합니다."""
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def write_report(path, report):
    """완료된 측정까지 결과를 원자적으로 보관합니다."""
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(plain(report), ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def file_identity(path):
    """대형 원본의 크기와 수정 시각을 측정 구간 밖에서 기록합니다."""
    item = Path(path)
    stat = item.stat()
    return {'path': str(item.resolve()), 'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def cpu_seconds(kernel, pid):
    """Windows의 읽기 전용 프로세스 핸들로 누적 CPU 시간을 조회합니다."""
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        return sum((value.dwHighDateTime << 32) | value.dwLowDateTime for value in times[2:]) * 1e-7
    finally:
        kernel.CloseHandle(handle)


def engine_identity(root):
    """측정한 정렬 Python 소스의 내용을 해시로 고정합니다."""
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root / 'alignment').glob('icp*.py'))}


def main():
    """고정 배치에서 보존한 이전 입력 준비·네이티브와 현재 공개 경로를 교차 측정합니다."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--package-root', required=True)
    parser.add_argument('--align-module', default='tools.align')
    parser.add_argument('--backends', nargs='+', default=['CCCORELIB', 'POINT_KD_ICP'])
    parser.add_argument('--point-kd-policies', nargs='+', choices=['STRICT', 'DEFAULT', 'LEGACY_001', 'BASELINE'])
    parser.add_argument('--legacy-point-kd-worker', type=Path)
    parser.add_argument('--baseline-point-kd-worker', type=Path)
    parser.add_argument('--baseline-point-kd-runtime', type=Path)
    parser.add_argument('--runs', type=int, default=5)
    parser.add_argument('--scenario', choices=['aligned', 'perturbed', 'reversed'], default='aligned')
    parser.add_argument('--export-geometry', action='store_true')
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    cases = (['POINT_KD_' + policy for policy in args.point_kd_policies]
             if args.point_kd_policies else args.backends)
    if 'POINT_KD_LEGACY_001' in cases and (args.legacy_point_kd_worker is None or not args.legacy_point_kd_worker.is_file()):
        parser.error('이전 0.001 정책 비교에는 보존한 실행 파일 경로가 필요합니다.')
    if 'POINT_KD_BASELINE' in cases and (args.baseline_point_kd_worker is None or not args.baseline_point_kd_worker.is_file()):
        parser.error('같은 종료식의 이전 구현 비교에는 보존한 실행 파일 경로가 필요합니다.')
    sys.path.insert(0, str(Path(args.package_root).resolve()))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from windows_paging import MemoryMonitor
    class BenchmarkMonitor(MemoryMonitor):
        """Blender와 작업기 메모리·CPU 관측치를 측정 결과에 함께 기록합니다."""
        def __init__(self):
            """측정 시작 직전 Blender의 누적 CPU 기준을 보존합니다."""
            super().__init__()
            self.kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
            self.cpu = {}
            self.blender_before = self.read(os.getpid())
            self.blender_cpu_before = cpu_seconds(self.kernel, os.getpid())
            self.blender_peak = self.blender_before['working_set_bytes']

        def sample(self, root_pid):
            """작업기 트리 표본과 현재 Blender 작업 집합을 함께 관측합니다."""
            super().sample(root_pid)
            current = self.read(os.getpid())
            if current:
                self.blender_peak = max(self.blender_peak, current['working_set_bytes'])
            for pid in self.processes:
                value = cpu_seconds(self.kernel, int(pid))
                if value is not None:
                    self.cpu[pid] = value

        def report(self):
            """표본 종료로 누락될 수 있는 CPU와 메모리의 한계를 명시합니다."""
            value = super().report()
            after = cpu_seconds(self.kernel, os.getpid())
            value.update(blender_before=self.blender_before, blender_after=self.read(os.getpid()),
                blender_observed_peak_working_set_bytes=self.blender_peak,
                blender_cpu_seconds=after - self.blender_cpu_before,
                observed_worker_cpu_seconds=self.cpu,
                cpu_limitation='작업기 CPU는 마지막 생존 표본까지의 누적값으로 종료 직전 구간이 빠질 수 있습니다.')
            return value
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / 'results.json'
    before = json.loads(Path(args.baseline).read_text(encoding='utf-8-sig'))
    report = {'completed': False, 'scenario': args.scenario, 'runs': [], 'backends': cases,
              'blender_version': bpy.app.version_string, 'logical_processors': os.cpu_count(),
              'source_blend_before': file_identity(bpy.data.filepath), 'module': args.align_module}
    def module(name):
        """검사할 소스 또는 설치본의 같은 정렬 하위 모듈을 가져옵니다."""
        return import_module(args.align_module + '.' + name)
    state = module('state')
    persistence = module('project_persistence.runtime')
    ir = module('alignment.icp_runtime')
    transforms = module('alignment.types')
    properties = module('properties')
    point_runtime = module('alignment.icp_point_kd_runtime')
    point_contract = module('alignment.icp_point_kd_contract')
    baseline_runtime = point_runtime
    if args.baseline_point_kd_runtime:
        path = args.baseline_point_kd_runtime.resolve(strict=True)
        spec = spec_from_file_location(args.align_module + '.alignment._benchmark_baseline_runtime', path)
        baseline_runtime = module_from_spec(spec)
        spec.loader.exec_module(baseline_runtime)
        report['baseline_python_runtime'] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    report['align_source_root'] = str(Path(state.__file__).parent)
    report['align_source_sha256'] = engine_identity(Path(state.__file__).parent)
    workers = {'POINT_KD_DEFAULT': Path(point_runtime.bundled_point_kd_worker_path())}
    if args.legacy_point_kd_worker:
        workers['POINT_KD_LEGACY_001'] = args.legacy_point_kd_worker.resolve()
    if args.baseline_point_kd_worker:
        workers['POINT_KD_BASELINE'] = args.baseline_point_kd_worker.resolve()
    report['native_workers'] = {key: {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                               for key, path in workers.items()}
    # 반복 폴링의 정보 로그 입출력을 두 엔진 모두 같은 조건으로 제외합니다.
    logging.disable(logging.INFO)
    runtime = state.RuntimeState()
    try:
        started = time.monotonic()
        assert persistence.schedule_project_restore_if_available(runtime), runtime.project_restore.last_message
        while runtime.project_restore.phase == 'RESTORING':
            if time.monotonic() - started > 300:
                raise TimeoutError('프로젝트 복원 제한 시간을 넘었습니다.')
            persistence._restore_timer(runtime)
            time.sleep(.01)
        assert runtime.project_restore.phase == 'IDLE', runtime.project_restore.last_message
        report['restore_seconds'] = time.monotonic() - started
        runtime.session_active = True
        runtime.marker_selection.clear()
        runtime.marker_alignment.clear()
        runtime.icp_alignment.clear()
        catalog, repository = runtime.model_catalog, runtime.model_repository
        names = {record.display_name: key for key, record in catalog.models.items()}
        moving, reference = names['test_2'], names['test_1']
        if args.scenario == 'reversed':
            moving, reference = reference, moving
        # 기준 레이어를 먼저 활성화하여 역할 반전 중에도 A가 하나 이상 남게 합니다.
        if catalog.models[reference].registration_status != 'REGISTERED':
            assert catalog.transfer_model_group(reference, source='MANUAL', has_marker_draft=False, completed_pair_count=0), catalog.message
        if catalog.models[moving].registration_status == 'REGISTERED':
            assert catalog.transfer_model_group(moving, source='MANUAL', has_marker_draft=False, completed_pair_count=0), catalog.message
        catalog.clear_selected_model()
        assert catalog.select_model(moving, has_marker_draft=False, completed_pair_count=0), catalog.message
        assert catalog.toggle_alignment_selection(reference, has_marker_draft=False, completed_pair_count=0), catalog.message
        originals = {entry['id']: np.asarray(entry['alignment_matrix'], dtype=np.float64) for entry in before['models']}
        for model_id in (moving, reference):
            catalog.set_geometry_visibility(model_id, show_points=False, show_surface=True)
            repository.set_alignment_transform(model_id, transforms.Sim3Transform(originals[model_id]))
        initial = originals[moving].copy()
        if args.scenario == 'perturbed':
            angle = np.deg2rad(.5)
            perturbation = np.eye(4)
            perturbation[:3, :3] = ((np.cos(angle), -np.sin(angle), 0), (np.sin(angle), np.cos(angle), 0), (0, 0, 1))
            perturbation[:3, 3] = (.6, -.4, .2)
            initial = perturbation @ initial
            report['perturbation'] = perturbation
        repository.set_alignment_transform(moving, transforms.Sim3Transform(initial))
        report['initial_alignment'] = initial
        report['moving_model_id'], report['reference_model_id'] = moving, reference
        report['input_files_before'] = [file_identity(entry['source']) for entry in before['models']]
        python_path = properties.bundled_cccore_python_path()
        report['python_path'] = python_path
        if args.export_geometry:
            geometry = module('alignment.icp_geometry')
            snapshot = geometry.create_cccore_geometry_snapshot(runtime, moving_model_id=moving,
                reference_model_ids=(reference,), initial_alignment_float64=initial)
            manifest = {'initial_alignment_4x4': initial.tolist(), 'provenance': plain(snapshot.provenance.to_mapping())}
            for key, item in (('moving', snapshot.moving), ('reference', snapshot.reference_combined)):
                positions_file, triangles_file = key + '_positions.bin', key + '_triangles.bin'
                np.asarray(item.positions_float64, dtype='<f8').tofile(output / positions_file)
                np.asarray(item.triangles_uint32, dtype='<u4').tofile(output / triangles_file)
                manifest[key] = {'kind': item.geometry_kind, 'point_count': len(item.positions_float64),
                    'positions_file': positions_file, 'triangle_count': len(item.triangles_uint32), 'triangles_file': triangles_file}
            write_report(output / 'geometry.json', manifest)
            del snapshot
        for repeat in range(args.runs + 1):
            order = cases if repeat % 2 == 0 else list(reversed(cases))
            for backend in order:
                runtime.icp_alignment.clear()
                repository.set_alignment_transform(moving, transforms.Sim3Transform(initial))
                monitor = BenchmarkMonitor()
                started = time.monotonic()
                if backend == 'POINT_KD_STRICT':
                    # 현재 종료식에 엄격한 허용치를 지정하며 이전 종료식 비교와는 구분합니다.
                    parameters = point_contract.PointKdRuntimeParameters(
                        max_threads=properties.configured_cc_cpu_thread_limit(),
                        relative_rms_tolerance=1e-6, translation_tolerance=1e-8, rotation_tolerance=1e-8)
                    handle = point_runtime.begin_point_kd_job(runtime, python_path, runtime_parameters=parameters)
                elif backend == 'POINT_KD_BASELINE':
                    # 보존한 입력 준비 모듈이 있으면 당시 파일 전달과 네이티브를 함께 사용합니다.
                    parameters = point_contract.PointKdRuntimeParameters(
                        max_threads=properties.configured_cc_cpu_thread_limit())
                    handle = baseline_runtime.begin_point_kd_job(runtime, python_path,
                        native_worker_path=str(args.baseline_point_kd_worker), runtime_parameters=parameters)
                elif backend == 'POINT_KD_LEGACY_001':
                    # 이전 바이너리의 실제 세 조건 동시 충족 정책과 당시 허용치를 유지합니다.
                    parameters = point_contract.PointKdRuntimeParameters(
                        max_threads=properties.configured_cc_cpu_thread_limit(),
                        relative_rms_tolerance=.001, translation_tolerance=.001, rotation_tolerance=.001)
                    handle = point_runtime.begin_point_kd_job(runtime, python_path,
                        native_worker_path=str(args.legacy_point_kd_worker), runtime_parameters=parameters)
                else:
                    selected_backend = 'POINT_KD_ICP' if backend == 'POINT_KD_DEFAULT' else backend
                    handle = ir.begin_icp_job(runtime, solver_backend=selected_backend, open3d_python_path=python_path)
                preparation = time.monotonic() - started
                manifest = json.loads(handle.files.manifest_path.read_text(encoding='utf-8'))
                input_files = list(handle.files.job_directory.glob('*.bin'))
                shared_buffers = [entry[key] for entry in manifest.get('geometry', {}).values() if isinstance(entry, dict)
                                  for key in ('positions_mapping', 'triangles_mapping', 'normals_mapping') if key in entry]
                transfer = {'input_file_count': len(input_files), 'input_file_bytes': sum(path.stat().st_size for path in input_files),
                            'shared_buffer_count': len(shared_buffers), 'shared_payload_bytes': sum(item['byte_count'] for item in shared_buffers)}
                last_memory = 0.0
                while runtime.icp_alignment.is_running:
                    now = time.monotonic()
                    if now - started > 600:
                        raise TimeoutError('정렬 측정 제한 시간을 넘었습니다.')
                    if now - last_memory > .1:
                        monitor.sample(handle.process.pid)
                        last_memory = now
                    ir.poll_icp_job(runtime)
                    time.sleep(.01)
                elapsed = time.monotonic() - started
                assert runtime.icp_alignment.has_preview, runtime.icp_alignment.last_error
                if shared_buffers:
                    from multiprocessing.shared_memory import SharedMemory
                    for item in shared_buffers:
                        try:
                            memory = SharedMemory(name=item['name'])
                        except FileNotFoundError:
                            continue
                        memory.close()
                        raise AssertionError('정렬 완료 뒤 공유 입력이 남아 있습니다.')
                    transfer['released_after_completion'] = True
                result = runtime.icp_alignment.result
                np.testing.assert_array_equal(repository.alignment_transform_for_model_id(moving).matrix_float64, initial)
                np.testing.assert_array_equal(repository.alignment_transform_for_model_id(reference).matrix_float64, originals[reference])
                row = {'repeat': repeat, 'warmup': repeat == 0, 'backend': backend, 'wall_seconds': elapsed,
                       'preparation_and_launch_seconds': preparation, 'final_alignment': result.final_alignment.matrix_float64,
                       'delta': result.icp_delta.matrix_float64, 'metadata': plain(result.metadata), 'memory': monitor.report(),
                       'input_transfer': transfer}
                report['runs'].append(row)
                write_report(report_path, report)
                print(f'{args.scenario} / {backend} / {repeat}: {elapsed:.3f}초', flush=True)
        report['source_blend_after'] = file_identity(bpy.data.filepath)
        report['input_files_after'] = [file_identity(entry['source']) for entry in before['models']]
        assert report['source_blend_before'] == report['source_blend_after']
        assert report['input_files_before'] == report['input_files_after']
        report['completed'] = True
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        ir.invalidate_icp(runtime, '독립 벤치마크 프로세스를 정리했습니다.')
        write_report(report_path, report)


if __name__ == '__main__':
    main()
