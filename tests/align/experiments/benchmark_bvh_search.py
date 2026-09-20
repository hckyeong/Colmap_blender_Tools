"""보존한 실제 프로젝트 입력으로 BVH 거리와 전체 ICP를 같은 조건에서 계측합니다."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_fixed_domain_preparation import array_hash, read_json


def save(path, value):
    """수치 배열을 포함한 검증 결과를 UTF-8로 저장합니다."""
    def convert(item):
        """넘파이 배열과 스칼라만 JSON 기본형으로 변환합니다."""
        if isinstance(item, np.ndarray):
            return item.tolist()
        if isinstance(item, np.generic):
            return item.item()
        raise TypeError(type(item).__name__)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=convert), encoding='utf-8')


def arguments():
    """기준선과 수정본을 별도 프로세스에서 실행하도록 소스·워커 경로를 받습니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('solver-directory', 'worker', 'input-directory', 'manifest', 'samples', 'preserved-result', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--mode', choices=('search', 'icp'), required=True)
    parser.add_argument('--query-path', type=Path)
    parser.add_argument('--overlap-policy', type=Path)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--timeout-seconds', type=float, default=240)
    return parser.parse_args()


def load_input(args, contract, solver):
    """초기 배치가 반영된 좌표를 다시 이동하지 않고 같은 정규화·표본을 복원합니다."""
    manifest = read_json(args.manifest)
    source = read_json(args.input_directory / 'source.json')
    prepared = read_json(args.samples.parent / 'result.json')
    prior = read_json(args.preserved_result)
    assert manifest['input_fingerprint'] == prepared['input_fingerprint'] == prior['input_fingerprint']
    assert source['input_fingerprint'] == manifest['geometry']['input_fingerprint'] == prepared['geometry_fingerprint']
    assert prepared['initial_alignment_4x4'] == manifest['initial_alignment_4x4'] == prior['initial_alignment_4x4']
    normalization = prepared['normalization']
    assert normalization == prior['normalization']
    norm = solver.Normalization(np.asarray(normalization['origin_project_world']), normalization['unit_project_world'])
    with np.load(args.samples, allow_pickle=False) as archive:
        moving = archive['registration'].copy()
    assert moving.shape == (50000, 3)
    assert array_hash(moving) == prepared['sample_position_fingerprints']['registration']
    role = 'moving' if manifest['reverse_registration'] else 'reference'
    geometry = solver._clean_mesh_geometry(contract.load_geometry(args.input_directory, source['geometry'][role]), role)
    reference = solver._clean_mesh_geometry(contract.CcCoreGeometry(geometry.kind,
        norm.apply(geometry.positions), geometry.triangles, geometry.provenance), role, native_precision=True)
    identity = {'input_fingerprint': manifest['input_fingerprint'], 'geometry_fingerprint': source['input_fingerprint'],
                'normalization': normalization, 'source_sample_fingerprint': array_hash(moving),
                'model_positions_fingerprint': array_hash(reference.positions),
                'model_triangles_fingerprint': array_hash(reference.triangles),
                'model_triangle_count': len(reference.triangles), 'initial_alignment_reapplied': False}
    return moving, reference, manifest, identity


def search_series(args, solver, contract, moving, reference, parameters, identity):
    """같은 다섯 배치에서 예열 한 번과 반복 측정을 수행하고 모든 거리를 보존합니다."""
    if args.query_path is None:
        raise ValueError('검색 비교에는 고정 조회 경로가 필요합니다.')
    matrices = read_json(args.query_path)['matrices']
    indices = (0, 10, 20, 31, 41)
    queries = [np.ascontiguousarray(solver.transform_positions(moving, np.asarray(matrices[i])), dtype=np.float32) for i in indices]
    domain = solver.make_preparing_search_domain(contract.CcCoreGeometry(
        'POINT_CLOUD', np.concatenate(queries), None, {'experiment': 'same-surface-query'}), reference)
    reports, arrays = [], {}
    with solver.NativeRunner(args.output, args.worker, parameters) as runner:
        model = runner.geometry('reference-model', reference.positions, reference.triangles)
        for run in range(args.repeats + 1):
            for position, query in enumerate(queries):
                data = runner.geometry(f'query-{run}-{position}', query, cache_scope='COMMAND')
                started = time.perf_counter()
                response = runner.call('distance', data, model, stage='PREPARING', search_domain=domain)
                wall = time.perf_counter() - started
                arrays[f'distance-{run}-{position}'] = response.pop('distances')
                reports.append({'run': run, 'warmup': run == 0, 'path_position': indices[position],
                                'query_fingerprint': array_hash(query), 'wall_seconds': wall, 'response': response})
                save(args.output / 'results.json', reports)
                print(json.dumps({'run': run, 'position': indices[position], 'wall_seconds': wall,
                                  'timings': response['timings']}, ensure_ascii=False), flush=True)
    np.savez_compressed(args.output / 'distances.npz', **arrays)
    save(args.output / 'result.json', {**identity, 'status': 'completed', 'session_statistics': runner.session_statistics,
                                     'comparisons': len(reports), 'points_per_comparison': len(moving)})


def icp_series(args, solver, moving, reference, parameters, identity):
    """제품 반복 상한과 겹침 계산을 유지한 강체·배율 정렬을 새 세션마다 측정합니다."""
    if args.overlap_policy is None:
        raise ValueError('전체 정렬 비교에는 보존한 겹침 평가 정책이 필요합니다.')
    policy = read_json(args.overlap_policy)['overlap_policy']
    for run in range(args.repeats + 1):
        folder = args.output / f'run-{run}'
        folder.mkdir()
        started = time.perf_counter()
        with solver.NativeRunner(folder, args.worker, parameters,
                                 cancel_probe=lambda: time.perf_counter() - started > args.timeout_seconds) as runner:
            data = runner.geometry('registration-data', moving)
            model = runner.geometry('reference-model', reference.positions, reference.triangles)
            rigid_ratio = solver.registration_overlap_ratio(runner, data, model, policy, stage='RIGID')
            rigid = runner.call('register', data, model, stage='RIGID', overlap_ratio=rigid_ratio)
            print(json.dumps({'run': run, 'stage': 'rigid', 'timings': rigid['timings'],
                              'icp_statistics': rigid['icp_statistics']}, ensure_ascii=False), flush=True)
            transformed = solver.transform_positions(moving, rigid['matrix_4x4'])
            refined_data = runner.geometry('registration-rigid-data', transformed)
            scale_ratio = solver.registration_overlap_ratio(runner, refined_data, model, policy, stage='SCALE_REFINE')
            refined = runner.call('register', refined_data, model, adjust_scale=True, stage='SCALE_REFINE', overlap_ratio=scale_ratio)
        report = {**identity, 'status': 'completed', 'run': run, 'warmup': run == 0, 'rigid': rigid, 'scale_refine': refined,
            'rigid_matrix_4x4': rigid['matrix_4x4'],
            'refined_matrix_4x4': np.asarray(refined['matrix_4x4']) @ np.asarray(rigid['matrix_4x4']),
            'registration_seconds': rigid['timings']['kernel_seconds'] + refined['timings']['kernel_seconds'],
            'rigid_overlap_ratio': rigid_ratio, 'scale_overlap_ratio': scale_ratio,
            'session_statistics': runner.session_statistics, 'total_including_overlap_and_session': time.perf_counter() - started}
        save(args.output / f'icp-{run}.json', report)
        print(json.dumps({'run': run, 'registration_seconds': report['registration_seconds'],
                          'total_seconds': report['total_including_overlap_and_session']}, ensure_ascii=False), flush=True)


def main():
    """독립 결과 폴더에서 지정 소스·워커와 동일 입력의 지문을 기록하고 측정합니다."""
    args = arguments()
    if not 0 <= args.repeats <= 10 or not 0 < args.timeout_seconds <= 600:
        raise ValueError('반복 수 또는 시간 상한이 허용 범위를 벗어났습니다.')
    args.solver_directory = args.solver_directory.resolve(strict=True)
    args.worker = args.worker.resolve(strict=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.solver_directory))
    import icp_cccore_contract as contract
    import icp_cccore_solver as solver
    assert Path(solver.__file__).resolve().parent == args.solver_directory
    moving, reference, manifest, identity = load_input(args, contract, solver)
    parameters = contract.resolve_parameters(manifest['parameters'])
    save(args.output / 'configuration.json', {**identity, 'mode': args.mode, 'parameters': parameters,
        'warmups': 1, 'repeats': args.repeats, 'worker': str(args.worker),
        'worker_sha256': hashlib.sha256(args.worker.read_bytes()).hexdigest(),
        'native_build_identity': solver.native_build_identity(args.worker),
        'solver_file': str(Path(solver.__file__)), 'solver_sha256': hashlib.sha256(Path(solver.__file__).read_bytes()).hexdigest()})
    if args.mode == 'search':
        search_series(args, solver, contract, moving, reference, parameters, identity)
    else:
        icp_series(args, solver, moving, reference, parameters, identity)


if __name__ == '__main__':
    main()
