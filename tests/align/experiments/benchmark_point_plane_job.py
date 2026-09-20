"""실제 Blender에서 보존한 입력으로 전체 솔버의 세 등록 방식을 비교합니다."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import time


def file_hash(path):
    """대형 입력의 지문을 측정 구간 밖에서 제한된 메모리로 계산합니다."""
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            value.update(block)
    return value.hexdigest()


def main():
    """동일 원본·표본·품질 정책에서 명시한 등록 방식만 변경하여 측정합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('solver-directory', 'worker', 'input-directory', 'manifest', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--mode', choices=('point', 'plane', 'plane_normals', 'scale_plane', 'scale_plane_normals'), required=True)
    args = parser.parse_args()
    source_directory = args.solver_directory.resolve(strict=True)
    sys.path.insert(0, str(source_directory))
    import icp_cccore_contract as contract
    import icp_cccore_solver as solver
    if Path(solver.__file__).resolve().parent != source_directory:
        raise RuntimeError('요청한 솔버와 실제 가져온 모듈이 다릅니다.')
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    job = root/'job'
    job.mkdir()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    input_directory = args.input_directory.resolve(strict=True)
    source = json.loads((input_directory/'source.json').read_text(encoding='utf-8-sig'))
    if source['input_fingerprint'] != manifest['geometry']['input_fingerprint']:
        raise ValueError('실제 프로젝트 입력 지문이 보존한 마커 조건과 다릅니다.')
    inputs = {}
    for role in ('moving', 'reference'):
        if source['geometry'][role] != manifest['geometry'][role]:
            raise ValueError('실제 형상 설명이 보존된 실행 입력과 다릅니다.')
        for field in ('positions_file', 'triangles_file'):
            name = manifest['geometry'][role].get(field)
            if name:
                original, target = (input_directory/name).resolve(strict=True), (job/name).resolve()
                if not original.is_relative_to(input_directory) or not target.is_relative_to(job):
                    raise ValueError('입력 경로가 검증 폴더 밖을 가리킵니다.')
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(original, target)
                inputs[name] = file_hash(original)
    manifest['job_id'] = job.name
    manifest.update(contract.identity_payload(manifest))
    contract.write_json_atomic(job/'manifest.json', manifest)
    original_call = solver.NativeRunner.call

    def comparison_call(self, operation, data, model, **kwargs):
        """검증 프로세스에서만 지정한 목적함수·노말 필터를 명시적으로 선택합니다."""
        if operation == 'register':
            plane = args.mode != 'point' and (not args.mode.startswith('scale_') or kwargs.get('adjust_scale', False))
            kwargs.update(registration_method='POINT_TO_PLANE' if plane else 'POINT_TO_POINT',
                          normal_cosine_threshold=0.5 if plane and args.mode.endswith('_normals') else -1.0)
        return original_call(self, operation, data, model, **kwargs)

    def progress(event):
        """제품과 같은 진행 파일 쓰기를 사용하고 터미널 출력은 늘리지 않습니다."""
        contract.write_json_atomic(job/'progress.json', event)

    solver.NativeRunner.call = comparison_call
    started = time.perf_counter()
    try:
        result = solver.solve_cccore_job(job, args.worker.resolve(strict=True), progress_sink=progress)
    finally:
        solver.NativeRunner.call = original_call
    elapsed = time.perf_counter() - started
    report = dict(mode=args.mode, elapsed_seconds=elapsed, inputs=inputs,
                  geometry_fingerprint=source['input_fingerprint'],
                  solver_sha256=file_hash(Path(solver.__file__)), result=result.metadata,
                  delta_4x4=result.delta_4x4.tolist(), final_alignment_4x4=result.final_alignment_4x4.tolist())
    contract.write_json_atomic(root/'result.json', report)
    print(json.dumps(dict(mode=args.mode, elapsed_seconds=elapsed, quality=result.metadata['quality'],
                          best_stage=result.metadata['best_stage'],
                          rms=result.metadata['best_candidate_metrics']['mean_rms_normalized']),
                     ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
