"""보존한 실제 프로젝트 입력으로 준비 단계만 실행하고 격자·표본·메모리를 기록합니다."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import threading
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[3]


def arguments():
    """기준선과 수정본의 소스·실행 파일·동일 입력을 명시적으로 받습니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--solver-directory', type=Path, required=True)
    parser.add_argument('--worker', type=Path, required=True)
    parser.add_argument('--input-directory', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout-seconds', type=float, default=120.0)
    return parser.parse_args()


def read_json(path):
    """보존된 입력과 결과를 UTF-8로 읽습니다."""
    return json.loads(path.read_text(encoding='utf-8-sig'))


def array_hash(array):
    """배열 형식과 순서를 포함해 측정 출력의 지문을 만듭니다."""
    value = np.ascontiguousarray(array)
    digest = hashlib.sha256(f'{value.dtype.str}:{value.shape}'.encode('ascii'))
    digest.update(memoryview(value).cast('B'))
    return digest.hexdigest()


def main():
    """전체 ICP 없이 v5·v6 고정 범위를 포함한 실제 표본 준비를 측정합니다."""
    args = arguments()
    source_directory = args.solver_directory.resolve(strict=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(source_directory))
    import icp_cccore_contract as contract
    import icp_cccore_solver as solver
    from windows_paging import MemoryMonitor

    assert Path(solver.__file__).resolve().parent == source_directory
    input_directory = args.input_directory.resolve(strict=True)
    manifest = read_json(args.manifest)
    source = read_json(input_directory / 'source.json')
    assert source['input_fingerprint'] == manifest['geometry']['input_fingerprint']
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    job = output / 'native-job'
    job.mkdir()
    memory = MemoryMonitor()
    stopped = threading.Event()
    monitoring_errors = []
    started = time.perf_counter()

    def observe():
        """측정 프로세스와 그 자식의 실제 메모리를 주기적으로 관측합니다."""
        try:
            while not stopped.is_set():
                memory.sample(os.getpid())
                stopped.wait(0.1)
        except Exception as error:
            monitoring_errors.append(str(error))

    def cancelled():
        """시간 상한에 도달하면 제품의 정상 취소 경로를 사용합니다."""
        return time.perf_counter() - started > args.timeout_seconds

    monitor = threading.Thread(target=observe, daemon=True)
    monitor.start()
    try:
        geometries = {}
        for role in ('moving', 'reference'):
            descriptor = source['geometry'][role]
            assert descriptor == manifest['geometry'][role], role
            geometries[role] = solver._clean_mesh_geometry(contract.load_geometry(input_directory, descriptor), role)
        normalization = solver.make_normalization(geometries['moving'].positions, geometries['reference'].positions)
        normalized = {}
        for role, geometry in geometries.items():
            normalized[role] = solver._clean_mesh_geometry(contract.CcCoreGeometry(
                geometry.kind, normalization.apply(geometry.positions), geometry.triangles,
                geometry.provenance), role, native_precision=True)
        input_seconds = time.perf_counter() - started
        parameters = contract.resolve_parameters(manifest['parameters'])
        with solver.NativeRunner(job, args.worker.resolve(strict=True), parameters,
                                 cancel_probe=cancelled) as runner:
            policy = solver.prepare_overlap_policy(runner, normalized['moving'], normalized['reference'],
                manifest['reverse_registration'], marker_initial_rms=manifest['marker_initial_rms'] / normalization.unit)
            sample_started = time.perf_counter()
            full_samples = {}
            options = {'full_samples': full_samples}
            if contract.CC_NATIVE_PROTOCOL_VERSION in ('cccore-native-v5', 'cccore-native-v6'):
                options['normalization'] = normalization.to_mapping()
            registration, moving_evaluation, reference_evaluation, sampling = solver.prepare_candidate_samples(
                runner, normalized['moving'], normalized['reference'], policy,
                manifest['reverse_registration'], **options)
            sample_seconds = time.perf_counter() - sample_started
        total_seconds = time.perf_counter() - started
    finally:
        stopped.set()
        monitor.join(timeout=5)
        if monitor.is_alive():
            raise RuntimeError('메모리 관측 스레드가 종료되지 않았습니다.')
    if monitoring_errors:
        raise RuntimeError(monitoring_errors)
    arrays = {'registration': registration, 'moving_evaluation': moving_evaluation,
              'reference_evaluation': reference_evaluation,
              **{'full_' + name: value for name, value in full_samples.items()}}
    np.savez(output / 'samples.npz', **arrays)
    report = {
        'scope': 'PREPARATION_ONLY', 'input_fingerprint': manifest['input_fingerprint'],
        'geometry_fingerprint': source['input_fingerprint'],
        'initial_alignment_4x4': manifest['initial_alignment_4x4'],
        'normalization': normalization.to_mapping(), 'parameters': parameters,
        'solver_sha256': hashlib.sha256(Path(solver.__file__).read_bytes()).hexdigest(),
        'native_build_identity': solver.native_build_identity(args.worker.resolve()),
        'input_seconds': input_seconds, 'sample_preparation_seconds': sample_seconds,
        'total_seconds': total_seconds, 'native_commands': runner.counter,
        'native_calls': runner.physical_native_calls, 'session_statistics': runner.session_statistics,
        'sampling': sampling, 'sample_position_fingerprints': {name: array_hash(value) for name, value in arrays.items()},
        'sample_counts': {name: len(value) for name, value in arrays.items()},
        'memory': memory.report(), 'memory_sampling_interval_seconds': 0.1,
        'measurement_note': '입력 정리부터 정상 세션 종료까지 측정하며 결과 배열 저장 시간은 제외합니다.',
    }
    (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({name: report[name] for name in ('scope', 'input_seconds', 'sample_preparation_seconds',
        'total_seconds', 'native_commands', 'session_statistics', 'sample_counts')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
