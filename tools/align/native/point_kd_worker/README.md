# 원본점 KD 강체·Sim(3) 정렬 워커

`colmap_point_kd_worker`는 `point-kd-native-v1` 규약의 독립 C++17 실행 파일입니다. CCCoreLib의 프로토콜이나 엔진 식별자를 사용하지 않습니다. 원본 유효점의 전체 배열을 유지하며, 삼각형 표면 샘플·BVH·GPU를 사용하지 않습니다. 엔진 1.4.0부터 기본 강체 모드와 명시적 Sim(3) 배율 보정 모드를 지원합니다.

## 입력과 실행

```powershell
colmap_point_kd_worker.exe --check-environment
colmap_point_kd_worker.exe --request 'C:\작업\request.json'
```

환경 조회는 실제 KD 적격점 검색과 SVD 자체 검사를 실행하고 `protocol_version`, `engine: POINT_KD`, `status: READY`, `self_test: passed`를 표준 출력으로 반환합니다.

요청 예시:

```json
{
  "protocol_version": "point-kd-native-v1",
  "source": {
    "positions_file": "source.bin",
    "point_count": 1000,
    "triangles_file": "source-triangles.bin",
    "triangle_count": 1800
  },
  "target": {
    "positions_file": "target.bin",
    "point_count": 2000,
    "normals_file": "target-normals.bin"
  },
  "method": "POINT_TO_PLANE",
  "parameters": {
    "max_threads": 4,
    "max_iterations": 30,
    "trim_fraction": 0.05,
    "normal_angle_degrees": 45,
    "relative_rms_tolerance": 0.001,
    "translation_tolerance": 0.001,
    "rotation_tolerance": 0.001
  },
  "cancel_file": "cancel",
  "progress_file": "progress.json",
  "result_file": "result.json"
}
```

- 위치와 선택적 법선은 헤더 없는 리틀엔디언 `float64 N×3`, 삼각형은 `uint32 M×3`입니다. 위치는 이미 현재 세계좌표로 변환된 값입니다. 파일 길이 불일치·비유한 위치·정점 번호 범위 오류는 입력 전체 실패입니다. 원본 파일을 수정하지 않습니다.
- `triangles_file`과 `normals_file`은 선택적입니다. 둘 다 있으면 명시한 법선을 사용합니다. 삼각형 입력에서는 **변환 후 표면의 면적가중 정점 법선**을 생성합니다. 비유한·길이 0 법선은 0으로 표시하고 법선이 필요한 대응에서 제외합니다.
- 파일 경로는 요청 파일의 부모 폴더 안으로 제한합니다. 절대 경로도 이 제한을 지켜야 합니다. 원본·요청·진행·취소·결과 경로 충돌은 쓰기 전에 거부합니다.
- `source`를 질의, `target`을 고정 대상으로 사용합니다. 작은 쪽 질의 선택과 점군–메시 방향, 원래 이동 대상의 역변환은 호출하는 Python 계층의 책임입니다.
- `POINT_TO_POINT`는 반사를 배제하는 SVD 강체 풀이입니다. `POINT_TO_PLANE`은 중심·대각선으로 정규화한 6자유도 선형계이며 대상 법선과 전체 계수 6을 요구합니다. 계수가 부족하면 다른 방식으로 우회하지 않습니다.

### 선택적 균일 배율 보정

`parameters.scale_mode`의 기본값은 `FIXED`입니다. `SIM3`를 명시하면 점대점에서 중심화한 질의 분산과 보정된 특이값 합으로 배율을 구합니다. 점평면에서는 정규화 질의 `q`와 법선 `n`의 행 `[q×n, n, q·n]`으로 7자유도 계를 풀며 로그 배율 증분을 지수화합니다. 반사·전단·비유한 배율·계수 부족을 실패로 반환하고 강체 모드로 우회하지 않습니다. 원본 배열은 보존하며 대상 KD-tree는 계속 작업당 한 번만 만듭니다. 법선에는 누적 균일 배율을 제외한 회전을 적용합니다.

Sim(3)의 종료 정책은 `SIM3_RELATIVE_DELTA_V1`입니다. 기존 상대 변화 조건을 만족하면서 `abs(log(이번 추가 배율)) < scale_tolerance`여야 수렴합니다. 기본 `scale_tolerance=1e-6`이며 0 초과 1 이하만 허용합니다. 배율 안정 조건은 이 프로젝트에서 추가한 것으로 Artec 배율 종료식을 재현한 것이 아닙니다. 강체의 `ARTEC_RELATIVE_DELTA_V1`과 기존 기본 허용치는 유지합니다. 결과에는 `scale_mode`, 누적 `additional_scale`, 반복별 `increment_scale`·`log_scale_change`가 포함됩니다. 역질의의 최종 P 배율은 Python 계층에서 역변환해 기록합니다.

## 검색과 반복 계약

대상의 모든 원본점으로 잎 크기 16의 정확 KD 트리를 작업당 한 번 생성합니다. 반경 안에서 법선 조건까지 만족하는 가장 가까운 원본점을 찾습니다. 가장 가까운 공간점의 법선이 부적격이어도 검색을 계속하며, 거리 동률은 원본 번호가 작은 점을 선택합니다.

엔진 `1.2.0`은 Artec에서 확인한 최장축 경계상자의 공간 중간 위치로 원본 번호 배열을 양방향 분할합니다. 왼쪽은 중간값 이하, 오른쪽은 초과이며 두 구간이 모두 있을 때만 재귀 분할합니다. 동일 좌표처럼 분리 불가능하면 16점을 넘는 잎을 허용합니다. 좌표 배열은 이동하거나 줄이지 않습니다. 배정도 극단값에서는 합의 오버플로를 피하도록 두 경계값을 각각 절반으로 나눈 뒤 더합니다.

적재한 `std::array<double,3>` 연속 배열의 소유권을 그대로 넘기고 `Eigen::Map`으로 계산합니다. 좌표 및 명시 법선을 별도 Eigen 배열로 복사하지 않으며 KD 트리도 같은 소유 버퍼를 참조합니다. Artec의 단정도 저장이나 법선 원뿔 가지치기는 포함하지 않습니다. 결과의 `kd_build_policy`와 `input_buffer_policy`로 구현을 식별하고 알고리즘·작업 규약은 유지합니다.

엔진 `1.3.0`은 파일 입력과 함께 Windows 작업별 공유 메모리를 지원합니다. `positions_mapping`·`triangles_mapping`·`normals_mapping`은 각각 기존 `_file` 대신 `{ "name": "Local\\ColmapPointKd_<32자리 소문자 UUID>", "byte_count": <배열 바이트 수> }`를 받습니다. 같은 배열에 파일과 매핑을 동시에 지정할 수 없습니다. 공유 버퍼는 64바이트 헤더 `<8sQ32s16x` 뒤 같은 자료형의 연속 배열이며 표식 `CPKDIPC1`, 실제 배열 길이, 이름의 UUID, 영으로 채운 예약 영역을 검사합니다. 읽기 전용 매핑을 계산 소유 배열 하나에 4MiB 단위로 복사하고 즉시 해제합니다. 끝까지 복사가 없는 구조는 아닙니다.

Windows의 Python 준비 경로는 원본 전체 해시·검증을 유지하면서 변환 좌표와 합친 면 번호를 공유 버퍼에 직접 작성합니다. 별도의 변환 좌표 불변 사본과 대형 바이너리 작업 파일은 생성하지 않습니다. 입력 메모리는 작업이 소유하고 완료·취소·실패 시 해제합니다. 진단을 보존해도 메모리를 계속 보유하지 않으므로 공유 입력 작업의 진단 JSON만으로 계산을 재실행할 수는 없습니다. 다른 운영체제의 파일 경로는 유지하며 공유 메모리 실패를 파일 전달로 조용히 우회하지 않습니다. 이 전달 방식은 프로젝트의 개선이며 Artec에서 확인한 방식이라고 주장하지 않습니다.

양쪽 입력에 법선이 있으면 `abs(dot) >= cos(45°)`를 적용하고 양쪽의 유효 법선을 요구합니다(`BOTH_VALID_ABS_DOT`). 점평면에서 질의 법선이 없으면 대상의 유효 법선만 요구합니다(`TARGET_VALID_ONLY`). 점대점에서 한쪽 법선이 없으면 법선 비교를 사용하지 않습니다(`DISABLED_MISSING_SIDE`).

질의는 oneTBB로 병렬 실행합니다. 부동소수점 합산은 스레드 수와 무관한 크기 2048의 고정 블록에서 수행한 뒤 블록 번호 순서로 합칩니다. 먼 5%는 `floor(대응 수 × 0.05)`개이며 대응 마스크만 0으로 만듭니다. 원본점과 KD 트리는 삭제하거나 재표본화하지 않습니다. 거리 동률은 질의 원본 번호가 큰 대응부터 제거합니다.

`D`는 두 입력 경계상자 대각선 중 작은 값입니다. 정규화 중심은 두 경계상자의 합집합 중심입니다. 대상의 최대 4096개 고정 번호에서 자기 자신을 제외한 정확 KD 최근접 거리의 중앙값 `h`를 측정합니다. 기본 반경은 `max(0.01D, 3h, 2marker_rms)`이며 `0.05D`를 넘으면 실패합니다. 검증용 명시 옵션 `maximum_correspondence_distance`가 있으면 그 반경을 사용하며 자동 확장하지 않습니다.

각 갱신은 직전 대응을 고정한 목적함수로 검사합니다. 증가한 갱신은 적용하지 않고 `REVIEW / OBJECTIVE_INCREASE`로 반환합니다. 승인한 갱신 뒤에는 반드시 새 대응을 검색합니다. `ARTEC_RELATIVE_DELTA_V1`은 `|E_k−E_(k−1)|/(E_(k−1)+1e-9) < τE` 또는 `(‖r_k−r_(k−1)‖/(‖r_(k−1)‖+1e-9) < τR 그리고 ‖t_k−t_(k−1)‖/(‖t_(k−1)‖+1e-9) < τT)`이면 수렴합니다. `t`는 이번 갱신 행렬의 이동, `r`은 회전축×라디안각 벡터이고 처음 이력은 영벡터입니다. 오차 조건을 먼저 검사하며 통과하면 변환 이력은 변경하지 않습니다. 기본 허용치는 세 항목 모두 단정도 0.001을 배정도로 승격한 `0.0010000000474974513`입니다. 명시한 허용치는 그대로 존중합니다. 점평면의 `E`는 재검색 후 접평면 잔차 RMS이고 점대점은 유클리드 RMS입니다. 30회 상한은 수렴 검사보다 우선하며 `REVIEW / MAX_ITERATIONS`로 반환합니다.

갱신 전후 대응 수가 같아야 한다는 수렴 조건은 없습니다. 한 갱신에서 대응 수가 절반 이하로 감소하면 기존의 별도 보호 분기 `REVIEW / CORRESPONDENCE_COLLAPSE`로 중단합니다. 50%는 정확도 보증값이 아니라 급격한 관측 변화에 대한 검토 분기입니다. 최소 대응 수를 충족하지 못하면 `FAIL`이며, 급감 전에 적용한 유한 변환은 검토 결과에 명시됩니다. 이번 변경은 종료식만 재현합니다. 좌표·잔차의 배정도 계산, 기존 보호 분기와 마지막 승인 후보 반환은 유지하며 Artec의 최저 오차 후보 선택까지 복제하지 않습니다.

## 결과와 취소

결과는 표준 출력과 `result_file`에 같은 JSON으로 기록합니다. 상태는 `SUCCESS`, `REVIEW`, `FAIL`, `CANCELLED`이며 `FAIL`의 프로세스 종료 코드는 2입니다. 취소는 정상적으로 종료 코드 0과 명시한 취소 상태를 반환합니다.

주요 필드는 `delta_4x4`(source→target), `initial_rms`, `final_rms`, `iterations`, `termination_reason`, `source_count`, `target_count`, `correspondence_count`, `rejected_distance_count`, `rejected_normal_count`, `trimmed_count`, `normal_policy`, `valid_normal_count`, `timing_seconds`, `iteration_stats`입니다. `initial_rms`·`final_rms`와 반복 통계의 `rms`는 제거 후 유클리드 거리입니다. `initial_objective_rms`·`final_objective_rms`와 반복 통계의 `objective_rms`·`relative_rms_change`는 수렴에 사용하는 목적함수 RMS입니다. 점평면 결과에는 같은 접평면 잔차를 뜻하는 `point_to_plane_rms`도 포함하며, 유클리드 RMS 변화는 `euclidean_relative_rms_change`로 별도 기록합니다.

`termination_policy`·`termination_epsilon`·`termination_tolerances`로 종료식과 실제 설정을 기록합니다. 반복별 `relative_translation_change`·`relative_rotation_change`는 갱신 벡터의 상대 변화이며, 오차 조기 종료나 보호 분기로 평가하지 않은 경우 `null`입니다. `convergence_branch`와 `increment_4x4`로 판정 근거를 보존합니다. 기존 `normalized_translation`·`rotation_radians`는 관찰 지표이며 수렴 판정에 사용하지 않습니다.

진행 파일은 `protocol_version`, `stage`, 증가하는 `progress`, `iteration`, `max_iterations`, 현재까지 반영한 `delta_4x4`, `initial_rms`, `current_rms`, `correspondence_count`를 제공합니다. 적재·법선·생성·간격·검색·제거·풀이·갱신에서 취소를 검사합니다. 취소 파일 조회는 공유 원자 상태로 25ms에 한 번만 실행하고 완료 경계에서는 강제 확인합니다.

JSON은 같은 폴더의 임시 파일을 완성한 뒤 원자 교체합니다. Windows의 일시적 읽기 잠금에 따른 접근 거부·공유 위반만 5ms 간격으로 최대 1초 재시도합니다. 그 외 쓰기 오류를 숨기지 않습니다.

빌드와 의존성 검증은 [재빌드 안내](REBUILD.md), 라이선스는 [제삼자 고지](THIRD_PARTY_NOTICES.md)를 참고하십시오.
