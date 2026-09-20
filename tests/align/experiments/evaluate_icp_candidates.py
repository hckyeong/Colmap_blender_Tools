"""보존한 독립 평가점을 고정하여 단일 ICP와 중첩 표본 후보의 제품 품질 판정을 비교합니다."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
FIXED = ROOT / 'build/verification/fixed-domain-implementation'
EXPERIMENT = ROOT / 'build/verification/icp-internal-implementation'
sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_fixed_domain_preparation import array_hash, read_json
from benchmark_icp_iterations import json_value


def arguments():
    """동일 입력과 원래 정규화 등록 입력 기준의 누적 후보 행렬을 명시적으로 받습니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--solver-directory', type=Path, default=ROOT / 'tools/align/alignment')
    parser.add_argument('--worker', type=Path, default=ROOT / '.cccore-runtime/cccore/colmap_cccore_worker.exe')
    parser.add_argument('--input-directory', type=Path, default=ROOT / 'build/verification/grid-cache-cause-20260920/live-input')
    parser.add_argument('--manifest', type=Path, default=FIXED / 'blender-operator-run/job-manifest.json')
    parser.add_argument('--preserved-result', type=Path, default=FIXED / 'blender-operator-run/job/result.json')
    parser.add_argument('--samples', type=Path, default=FIXED / 'preparation-series/fixed-1/samples.npz')
    parser.add_argument('--candidate', nargs=3, action='append', metavar=('NAME', 'JSON', 'FIELD'),
                        help='원래 정규화 등록 입력을 기준으로 한 누적 강체 행렬입니다.')
    parser.add_argument('--scale-candidate', nargs=3, action='append', default=[], metavar=('NAME', 'JSON', 'FIELD'),
                        help='같은 NAME의 원래 정규화 등록 입력 기준 누적 배율 보정 행렬입니다.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout-seconds', type=float, default=120)
    return parser.parse_args()


def load_candidate(specification, contract, input_fingerprint, normalization):
    """후보 행렬의 명시한 필드와 제공된 출처 식별자를 검사하고 원문 지문을 보존합니다."""
    name, filename, field = specification
    path = Path(filename).resolve(strict=True)
    document = read_json(path)
    if document.get('status') != 'completed':
        raise ValueError(f'완료되지 않은 후보입니다: {path}')
    if 'input_fingerprint' in document and document['input_fingerprint'] != input_fingerprint:
        raise ValueError(f'후보 입력 지문이 다릅니다: {path}')
    if 'normalization' in document and document['normalization'] != normalization:
        raise ValueError(f'후보 정규화가 다릅니다: {path}')
    matrix, _scale = contract.matrix_sim3(document[field], f'{name} 후보 행렬')
    return name, matrix, {'file': str(path), 'field': field,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'input_identity_present': 'input_fingerprint' in document,
        'coordinate_contract': '원래 정규화 등록 입력 기준이며 초기 배치는 이미 반영됨',
        'recorded_kernel_seconds': document.get('kernel_seconds'),
        'recorded_wall_seconds': document.get('total_wall_seconds')}


def select_workflow(solver, baseline, rigid, refined, rigid_delta, refined_delta, policy, normalization, *, scale_present):
    """제품 판정을 유지하되 실제 없는 배율 슬롯의 중복 설명만 보고에서 제외합니다."""
    metrics = {'baseline': baseline, 'rigid': rigid, 'scale_refine': refined}
    deltas = {'baseline': np.eye(4), 'rigid': rigid_delta, 'scale_refine': refined_delta}
    provisional = solver.select_candidate(metrics, {'additional_scale': 1.0}, policy)[0]
    diagnostics = solver.delta_diagnostics(normalization.world_delta(deltas[provisional]), normalization)
    best, quality, reasons, improvement, stop = solver.select_candidate(metrics, diagnostics, policy)
    if not scale_present:
        duplicate_reason = '배율 보정 후보는 초기 겹침 영역의 대응이나 분포를 유지하지 못해 제외했습니다.'
        rigid_reason = '크기 고정 후보는 초기 겹침 영역의 대응이나 분포를 유지하지 못해 제외했습니다.'
        if rigid_reason in reasons:
            reasons = [reason for reason in reasons if reason != duplicate_reason]
    return {'best_stage': best, 'quality': quality, 'review_reasons': reasons,
            'relative_improvement': improvement, 'solver_stop_reason': stop,
            'diagnostics': diagnostics, 'selected_metrics': metrics[best],
            'selected_delta_native_4x4': deltas[best],
            'selected_delta_project_world_4x4': normalization.world_delta(deltas[best])}


def main():
    """표본을 다시 뽑지 않고 원본 전체 메시의 정확 거리로 모든 후보를 순차 평가합니다."""
    args = arguments()
    if not 0 < args.timeout_seconds <= 120:
        raise ValueError('시간 상한은 0초 초과 120초 이하여야 합니다.')
    sys.path.insert(0, str(args.solver_directory.resolve(strict=True)))
    import icp_cccore_contract as contract
    import icp_cccore_solver as solver

    if contract.CC_NATIVE_PROTOCOL_VERSION != 'cccore-native-v6':
        raise ValueError('이 검증에는 실제 v6 런타임이 필요합니다.')
    manifest, preserved = read_json(args.manifest), read_json(args.preserved_result)
    source = read_json(args.input_directory / 'source.json')
    preparation = read_json(args.samples.parent / 'result.json')
    assert source['input_fingerprint'] == manifest['geometry']['input_fingerprint'] == preparation['geometry_fingerprint']
    assert manifest['input_fingerprint'] == preparation['input_fingerprint'] == preserved['input_fingerprint']
    assert preparation['initial_alignment_4x4'] == manifest['initial_alignment_4x4'] == preserved['initial_alignment_4x4']
    normalized = preparation['normalization']
    assert normalized == preserved['normalization']
    normalization = solver.Normalization(np.asarray(normalized['origin_project_world']), normalized['unit_project_world'])
    parameters = contract.resolve_parameters(manifest['parameters'])
    for key, value in solver.QUALITY_POLICY.items():
        assert preserved['quality_policy'][key] == value
    for seed in ('optimizer_seed', 'evaluation_seed', 'probe_seed'):
        assert parameters[seed] == preparation['parameters'][seed]
    names = ('moving_evaluation', 'reference_evaluation', 'full_moving_to_reference', 'full_reference_to_moving')
    with np.load(args.samples, allow_pickle=False) as archive:
        samples = {name: archive[name].copy() for name in names}
    fingerprints = {name: array_hash(value) for name, value in samples.items()}
    assert all(fingerprints[name] == preparation['sample_position_fingerprints'][name] for name in names)
    moving_samples = np.concatenate((samples['moving_evaluation'], samples['full_moving_to_reference']))
    reference_samples = np.concatenate((samples['reference_evaluation'], samples['full_reference_to_moving']))
    counts = {'moving_to_reference': len(samples['moving_evaluation']), 'reference_to_moving': len(samples['reference_evaluation'])}
    specifications = args.candidate or [
        ('single', str(EXPERIMENT / 'trace-series/rigid-cached-convergence.result.json'), 'matrix_4x4'),
        ('nested', str(EXPERIMENT / 'nested-rigid/result.json'), 'original_registration_matrix_4x4')]
    candidates = {}
    for spec in specifications:
        name, matrix, provenance = load_candidate(spec, contract, manifest['input_fingerprint'], normalized)
        if name in candidates:
            raise ValueError('후보 이름을 중복 지정할 수 없습니다.')
        candidates[name] = {'rigid': matrix, 'provenance': {'rigid': provenance}}
    for spec in args.scale_candidate:
        name, matrix, provenance = load_candidate(spec, contract, manifest['input_fingerprint'], normalized)
        if name not in candidates or 'scale_refine' in candidates[name]:
            raise ValueError('배율 후보는 기존 강체 이름마다 한 번만 지정해야 합니다.')
        candidates[name]['scale_refine'] = matrix
        provenance['composition'] = '원래 정규화 등록 입력 기준 누적 행렬 직접 사용'
        candidates[name]['provenance']['scale_refine'] = provenance
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    job = output / 'native-job'
    job.mkdir()
    started = time.perf_counter()
    report = {'scope': 'INDEPENDENT_EVALUATION_ONLY', 'status': 'running',
              'input_fingerprint': manifest['input_fingerprint'], 'geometry_fingerprint': source['input_fingerprint'],
              'normalization': normalized, 'initial_alignment_reapplied': False,
              'sample_fingerprints': fingerprints, 'sample_counts': {key: len(value) for key, value in samples.items()},
              'native_build_identity': solver.native_build_identity(args.worker.resolve(strict=True)),
              'solver_file': str(Path(solver.__file__).resolve()),
              'solver_sha256': hashlib.sha256(Path(solver.__file__).read_bytes()).hexdigest(),
              'quality_policy': solver.QUALITY_POLICY, 'candidates': {}}

    def cancelled():
        """평가 시간 상한에서 제품의 정상 취소 경로를 요청합니다."""
        return time.perf_counter() - started >= args.timeout_seconds

    try:
        geometries = {}
        for role in ('moving', 'reference'):
            assert source['geometry'][role] == manifest['geometry'][role]
            geometry = solver._clean_mesh_geometry(contract.load_geometry(args.input_directory, source['geometry'][role]), role)
            geometries[role] = solver._clean_mesh_geometry(contract.CcCoreGeometry(geometry.kind,
                normalization.apply(geometry.positions), geometry.triangles, geometry.provenance), role, native_precision=True)
        assert len(geometries['reference'].triangles) == 11303380
        report['geometry'] = {role: {'point_count': len(value.positions), 'triangle_count': len(value.triangles),
            'positions_fingerprint': array_hash(value.positions), 'triangles_fingerprint': array_hash(value.triangles)}
            for role, value in geometries.items()}
        with solver.NativeRunner(job, args.worker.resolve(), parameters, cancel_probe=cancelled) as runner:
            policy = solver.prepare_overlap_policy(runner, geometries['moving'], geometries['reference'],
                manifest['reverse_registration'], marker_initial_rms=manifest['marker_initial_rms'] / normalization.unit)
            report['overlap_policy'] = policy

            def evaluate(label, delta, stage):
                """동일 표본과 분모로 정확 거리·상위 오차·대응 분포를 평가합니다."""
                began = time.perf_counter()
                metrics = solver.evaluate_candidate(runner, label, delta, geometries['moving'], geometries['reference'],
                    moving_samples, reference_samples, normalization, policy, stage=stage, overlap_sample_counts=counts)
                return metrics, time.perf_counter() - began

            baseline, baseline_seconds = evaluate('baseline', np.eye(4), 'BASELINE')
            solver.assess_correspondence_coverage(baseline, policy)
            report.update(baseline_metrics=baseline, baseline_seconds=baseline_seconds)
            for index, (name, candidate) in enumerate(candidates.items()):
                rigid = np.linalg.inv(candidate['rigid']) if manifest['reverse_registration'] else candidate['rigid']
                rigid_metrics, rigid_seconds = evaluate(f'candidate-{index}-rigid', rigid, 'RIGID_EVALUATION')
                refined, refined_metrics, refined_seconds = rigid, rigid_metrics, 0.0
                if 'scale_refine' in candidate:
                    refined = np.linalg.inv(candidate['scale_refine']) if manifest['reverse_registration'] else candidate['scale_refine']
                    refined_metrics, refined_seconds = evaluate(f'candidate-{index}-scale', refined, 'REFINE_EVALUATION')
                report['candidates'][name] = {**select_workflow(solver, baseline, rigid_metrics, refined_metrics,
                    rigid, refined, policy, normalization, scale_present='scale_refine' in candidate),
                    'provenance': candidate['provenance'],
                    'rigid_metrics': rigid_metrics, 'scale_refine_metrics': refined_metrics if 'scale_refine' in candidate else None,
                    'comparison_scope': 'RIGID_AND_SCALE' if 'scale_refine' in candidate else 'RIGID_ONLY',
                    'scale_stage': 'evaluated' if 'scale_refine' in candidate else 'absent',
                    'product_pipeline_completed': False,
                    'scale_candidate_supplied': 'scale_refine' in candidate,
                    'assessment_note': '보존한 후보의 독립 평가입니다. 전체 제품 실행 완료나 배율 단계 수행을 의미하지 않습니다.',
                    'missing_scale_policy': '배율 단계가 없으면 선택 함수의 필수 슬롯에서만 강체 평가를 참조합니다. 실제 배율 후보는 없습니다.',
                    'rigid_evaluation_seconds': rigid_seconds, 'scale_evaluation_seconds': refined_seconds,
                    'rigid_delta_native_4x4': rigid,
                    'scale_delta_native_4x4': refined if 'scale_refine' in candidate else None}
        report['comparison_rule'] = {
            'threshold_source': '기존 QUALITY_POLICY 및 prepare_overlap_policy의 반환값을 그대로 사용',
            'candidate_selection': '대응 수와 공간 분포를 유지하는 후보 중 양방향 평균 RMS가 가장 작은 후보',
            'p95_rule': '선택 후보의 방향별 p95를 같은 고정 평가점의 초기 baseline과 비교',
            'pairwise_tolerance': None,
            'pairwise_note': '두 강체 방법 사이의 별도 합격 허용치는 기존 정책에 없으므로 추가하지 않습니다.',
        }
        report.update(status='completed', session_statistics=runner.session_statistics,
                      physical_native_calls=runner.physical_native_calls)
    except contract.CcCoreCancelled as error:
        report.update(status='cancelled', message=str(error))
    except Exception as error:
        report.update(status='error', message=str(error))
        raise
    finally:
        report['elapsed_seconds'] = time.perf_counter() - started
        (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=json_value), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'output': str(output / 'result.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
