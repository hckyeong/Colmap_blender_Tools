"""보존된 실제 5만 점 네이티브 명령을 세 등록 방식으로 순차 비교합니다."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def digest(path):
    """대형 입력을 메모리에 복사하지 않고 바이트 지문을 계산합니다."""
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def save(path, value):
    """유한한 비교 결과와 명령을 UTF-8 JSON으로 원자 게시합니다."""
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def wait_file(path, process, timeout, terminal_path=None):
    """응답 또는 명시적 세션 실패를 보존하며 원자적으로 게시되는 파일을 기다립니다."""
    deadline = time.monotonic() + timeout
    while not path.is_file():
        if terminal_path is not None and terminal_path.is_file():
            return json.loads(terminal_path.read_text(encoding='utf-8'))
        if process.poll() is not None:
            raise RuntimeError(f'워커 조기 종료: {process.returncode}, {path}')
        if time.monotonic() >= deadline:
            raise TimeoutError(f'응답 제한 시간 초과: {path}')
        time.sleep(.01)
    return json.loads(path.read_text(encoding='utf-8'))


def run_mode(args, source, mode):
    """독립 세션에 동일 입력 하드링크를 연결해 캐시 조건과 초기 위치를 맞춥니다."""
    root = args.output / mode
    root.mkdir()
    (root / 'native').mkdir()
    command = json.loads(json.dumps(source))
    identity = {'protocol_version':'cccore-native-v8', 'session_id':uuid.uuid4().hex,
                'job_id':root.name, 'input_fingerprint':source['input_fingerprint']}
    command.update(identity, sequence=1, stage='COMPARISON', response_file='native/command-0001.response.json',
                   progress_file='native/command-0001.progress.json', distances_file='native/command-0001.distances.bin')
    inputs = {}
    for role in ('data','model'):
        descriptor = command[role]
        descriptor.pop('normals_file',None)
        for key in ('positions_file','triangles_file'):
            if key not in descriptor:
                continue
            original = (args.job_root / descriptor[key]).resolve(strict=True)
            if not original.is_relative_to(args.job_root.resolve()):
                raise ValueError('입력 파일이 지정 작업 폴더 밖에 있습니다.')
            target = root / 'native' / f'{role}-{key}.bin'
            os.link(original,target)
            descriptor[key] = target.relative_to(root).as_posix()
            inputs[f'{role}_{key}'] = {'sha256':digest(original),'size':original.stat().st_size}
    if mode == 'plane_normals':
        target = root / 'native/data-normals.bin'
        os.link(args.normals_file,target)
        command['data']['normals_file'] = target.relative_to(root).as_posix()
        inputs['normals'] = {'sha256':digest(target),'size':target.stat().st_size}
    command['parameters'].update(registration_method='POINT_TO_POINT' if mode == 'point' else 'POINT_TO_PLANE',
        normal_cosine_threshold=.5 if mode == 'plane_normals' else -1)
    if args.max_iterations is not None:
        command['parameters']['max_iterations'] = args.max_iterations
    session = {**identity, 'parameters':{'max_threads':command['parameters']['max_threads']},
               'ready_file':'native/session-ready.json','result_file':'native/session-result.json',
               'cancel_file':'cancel.request','command_directory':'native'}
    save(root / 'native-session-request.json',session)
    started = time.perf_counter()
    report = {'mode':mode,'inputs':inputs,'parameters':command['parameters'],'status':'error'}
    process = subprocess.Popen([str(args.worker),'--session',str((root / 'native-session-request.json').resolve())],
                               stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        report['ready'] = wait_file(root / 'native/session-ready.json',process,30)
        registration_started = time.perf_counter()
        save(root / 'native/command-0001.request.json',command)
        result = wait_file(root / 'native/command-0001.response.json',process,args.timeout_seconds,
                           root / 'native/session-result.json')
        report.update(status=result['status'],result=result,registration_seconds=time.perf_counter()-registration_started)
        if result['status'] == 'completed':
            save(root / 'native/command-0002.request.json',{**identity,'sequence':2,'operation':'close','stage':'COMPARISON',
                 'response_file':'native/command-0002.response.json','progress_file':'native/command-0002.progress.json'})
        report['session_result'] = wait_file(root / 'native/session-result.json',process,30)
        process.wait(timeout=30)
        expected_status = 'closed' if result['status'] == 'completed' else result['status']
        if report['session_result']['status'] != expected_status or process.returncode != {'closed':0,'error':1,'cancelled':2}[expected_status]:
            raise RuntimeError('등록 응답과 세션 종료 상태·프로세스 종료 코드가 다릅니다.')
    except Exception as error:
        report['error'] = str(error)
        (root / 'cancel.request').write_text('cancel',encoding='utf-8')
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=30)
        raise
    finally:
        report['total_seconds'] = time.perf_counter()-started
        save(root / 'comparison.json',report)
    return report


def main():
    """동일한 실제 등록 입력과 출처 노말로 세 방식을 차례로 실행합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('worker','job-root','request','normals-file','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--max-iterations',type=int)
    parser.add_argument('--timeout-seconds',type=float,default=600)
    parser.add_argument('--modes',nargs='+',choices=('point','plane','plane_normals'),default=['point','plane','plane_normals'])
    args = parser.parse_args()
    source = json.loads(args.request.read_text(encoding='utf-8'))
    if source['operation'] != 'register' or source['data']['point_count'] != 50000:
        raise ValueError('보존된 실제 5만 점 등록 명령이 필요합니다.')
    if 'triangles_file' not in source['model']:
        raise ValueError('비교 대상은 삼각면 메시여야 합니다.')
    if args.normals_file.stat().st_size != 50000 * 3 * 8:
        raise ValueError('출처 노말은 등록 표본과 같은 순서인 5만 행 float64 배열이어야 합니다.')
    if args.timeout_seconds <= 0 or (args.max_iterations is not None and args.max_iterations < 1):
        raise ValueError('시간과 반복 상한은 양수여야 합니다.')
    args.output.mkdir(parents=True,exist_ok=False)
    report = {'worker_sha256':digest(args.worker),'request_sha256':digest(args.request),
              'initial_alignment_reapplied':False,'runs':[]}
    for mode in args.modes:
        report['runs'].append(run_mode(args,source,mode))
        save(args.output / 'comparison.json',report)
    print(json.dumps({'output':str(args.output),'statuses':[run['status'] for run in report['runs']]},ensure_ascii=False))


if __name__ == '__main__':
    main()
