"""보존한 5만 표본과 전체 기준면으로 강체 또는 배율 ICP의 짧은 반복을 측정합니다."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_fixed_domain_preparation import array_hash, read_json
from windows_paging import MemoryMonitor


def arguments():
    """기준선·수정본 소스와 보존 입력 및 한 단계의 독립 출력 위치를 받습니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('solver-directory', 'worker', 'input-directory', 'manifest', 'samples', 'preserved-result', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--stage', choices=('rigid', 'scale_refine'), required=True)
    parser.add_argument('--max-iterations', type=int, choices=(5, 10), default=5)
    parser.add_argument('--timeout-seconds', type=float, default=120)
    return parser.parse_args()


def json_value(value):
    """제품 응답에 포함된 배열과 넘파이 수치를 JSON 값으로 변환합니다."""
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(type(value).__name__)


def main():
    """초기 배치를 중복 적용하지 않고 보존 정규화와 표본으로 한 번만 등록합니다."""
    args = arguments()
    source_directory = args.solver_directory.resolve(strict=True)
    sys.path.insert(0, str(source_directory))
    import icp_cccore_contract as contract
    import icp_cccore_solver as solver

    assert Path(solver.__file__).resolve().parent == source_directory
    if not 0 < args.timeout_seconds <= 120:
        raise ValueError('시간 상한은 0초 초과 120초 이하여야 합니다.')
    output = args.output.resolve()
    input_directory = args.input_directory.resolve(strict=True)
    manifest = read_json(args.manifest)
    source = read_json(input_directory / 'source.json')
    preparation = read_json(args.samples.parent / 'result.json')
    preserved = read_json(args.preserved_result)
    parameters = contract.resolve_parameters(manifest['parameters'])
    assert source['input_fingerprint'] == manifest['geometry']['input_fingerprint'] == preparation['geometry_fingerprint']
    assert preparation['input_fingerprint'] == manifest['input_fingerprint'] == preserved['input_fingerprint']
    assert preparation['initial_alignment_4x4'] == manifest['initial_alignment_4x4']
    normalization_data = preparation['normalization']
    assert normalization_data == preserved['normalization']
    normalization = solver.Normalization(np.asarray(normalization_data['origin_project_world']), normalization_data['unit_project_world'])
    with np.load(args.samples, allow_pickle=False) as samples:
        registration = samples['registration'].copy()
    assert registration.shape == (50000, 3)
    sample_fingerprint = array_hash(registration)
    assert sample_fingerprint == preparation['sample_position_fingerprints']['registration']
    for seed in ('optimizer_seed', 'probe_seed', 'evaluation_seed'):
        assert preparation['parameters'][seed] == parameters[seed]
    stages = {stage['stage']: stage for stage in preserved['registration_stages']}
    initial_matrix = np.eye(4)
    if args.stage == 'scale_refine':
        initial_matrix = np.asarray(stages['rigid']['matrix_4x4'])
        registration = solver.transform_positions(registration, initial_matrix)
    parameters.update(max_iterations=args.max_iterations, scale_refine_max_iterations=args.max_iterations)
    reverse = manifest['reverse_registration']
    role = 'moving' if reverse else 'reference'
    assert source['geometry'][role] == manifest['geometry'][role]
    output.mkdir(parents=True, exist_ok=False)
    memory = MemoryMonitor()
    stopped = threading.Event()
    monitor_errors = []
    started = time.perf_counter()
    registration_started = None

    def observe():
        """실행 프로세스와 자식 워커의 메모리를 함께 기록합니다."""
        try:
            while not stopped.is_set():
                memory.sample(os.getpid())
                stopped.wait(.1)
        except Exception as error:
            monitor_errors.append(str(error))

    def cancelled():
        """한 번의 등록 시간이 상한에 도달하면 제품의 정상 취소를 요청합니다."""
        return registration_started is not None and time.perf_counter() - registration_started >= args.timeout_seconds

    monitor = threading.Thread(target=observe, daemon=True)
    monitor.start()
    report = {'status': 'error', 'stage': args.stage, 'max_iterations': args.max_iterations,
              'manifest': str(args.manifest.resolve()), 'samples': str(args.samples.resolve()),
              'preserved_result': str(args.preserved_result.resolve()), 'input_directory': str(input_directory),
              'input_fingerprint': manifest['input_fingerprint'], 'geometry_fingerprint': source['input_fingerprint'],
              'sample_fingerprint': sample_fingerprint, 'registration_input_fingerprint': array_hash(registration),
              'normalization': normalization_data, 'initial_native_matrix_4x4': initial_matrix,
              'seed_parameters': {key: parameters[key] for key in ('optimizer_seed', 'probe_seed', 'evaluation_seed')},
              'solver_file': str(Path(solver.__file__).resolve()),
              'solver_sha256': hashlib.sha256(Path(solver.__file__).read_bytes()).hexdigest(),
              'worker': str(args.worker.resolve(strict=True)),
              'native_build_identity': solver.native_build_identity(args.worker.resolve()),
              'parameters': parameters, 'timeout_seconds': args.timeout_seconds,
              'initial_alignment_note': '보존 좌표에 초기 배치가 반영되어 있으므로 초기 배치 행렬을 다시 적용하지 않습니다.'}
    try:
        geometry = solver._clean_mesh_geometry(contract.load_geometry(input_directory, source['geometry'][role]), role)
        model_geometry = solver._clean_mesh_geometry(contract.CcCoreGeometry(
            geometry.kind, normalization.apply(geometry.positions), geometry.triangles, geometry.provenance),
            role, native_precision=True)
        assert len(model_geometry.triangles) == 11303380
        report['model_triangle_count'] = len(model_geometry.triangles)
        report['model_positions_fingerprint'] = array_hash(model_geometry.positions)
        report['model_triangles_fingerprint'] = array_hash(model_geometry.triangles)
        job = output / 'native-job'
        job.mkdir()
        with solver.NativeRunner(job, args.worker.resolve(), parameters, cancel_probe=cancelled) as runner:
            data = runner.geometry('registration-data', registration)
            model = runner.geometry('reference-model', model_geometry.positions, model_geometry.triangles)
            registration_started = time.perf_counter()
            result = runner.call('register', data, model, adjust_scale=args.stage == 'scale_refine',
                reverse=reverse, stage=args.stage.upper(), overlap_ratio=stages[args.stage]['estimated_overlap_ratio'])
            report.update(status='completed', result=result,
                matrix_4x4=result['matrix_4x4'], final_rms=result['final_rms'],
                final_rms_project_world=result['final_rms'] * normalization.unit,
                icp_statistics=result.get('icp_statistics'))
        report['session_statistics'] = runner.session_statistics
    except contract.CcCoreCancelled as error:
        report.update(status='cancelled', message=str(error))
    except Exception as error:
        report.update(status='error', message=str(error), error_type=type(error).__name__)
        raise
    finally:
        stopped.set()
        monitor.join(timeout=5)
        report.update(total_seconds=time.perf_counter() - started, memory=memory.report(),
            registration_wall_seconds=None if registration_started is None else time.perf_counter() - registration_started,
            memory_monitor_errors=monitor_errors)
        (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=json_value), encoding='utf-8')
    print(json.dumps({key: report.get(key) for key in ('status', 'stage', 'total_seconds', 'registration_wall_seconds', 'final_rms', 'icp_statistics')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
