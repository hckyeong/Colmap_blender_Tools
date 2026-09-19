# 독립 CCCoreLib 작업 계약

이 디렉터리는 Blender 또는 CloudCompare GUI를 불러오지 않는 Windows x64 실행 파일의 소스입니다. 상위 Python 조정자가 공통 좌표 정규화, 결정적 표본화, 강체→유사변환 두 단계와 독립 평가를 담당합니다.

`colmap_cccore_worker.exe --session <JSON 절대경로>`는 Align 작업 하나의 등록·거리 명령을 단일 프로세스에서 순차 처리합니다. 세션의 요청·응답·진행·취소·종료 결과는 작업 폴더의 원자 파일로만 교환하며 표준 입출력 파이프를 사용하지 않습니다. 기존 `--request` v2는 수치 기준선과 호환 검사에만 유지하며 v4 세션과 혼용하지 않습니다. `--check-environment`는 실제로 로드한 공유 DLL 경로와 비대칭 점군 합성 등록 검사를 반환합니다.

## 파일 계약

- 세션 `protocol_version`: `cccore-native-v4`
- 시작 요청은 `session_id`, `job_id`, `input_fingerprint`, `parameters.max_threads`와 고정된 ready·result·cancel·command 경로를 포함합니다.
- 각 명령은 1부터 단조 증가하는 `sequence`, `operation`, `stage`, 동일한 세션 식별자와 순번별 고정 response·progress·distance 경로를 포함합니다.
- `operation`: `register`, `distance`, 세션 종료용 `close`
- `data`, `model`: 캐시 키인 `geometry_id`, `positions_file`, `point_count`와 `cache_scope`
- 메시 `model`만 `triangles_file`, `triangle_count`를 추가합니다.
- 위치는 리틀엔디언 `float64` 세 성분, 면은 리틀엔디언 `uint32` 세 인덱스입니다.
- 세션 시작·명령·응답·진행·거리·종료 결과는 모두 작업 전용 디렉터리에서 완성된 파일만 원자 교체합니다. 누락·중복·역순·부분 JSON과 식별자 불일치는 세션 오류로 종료합니다.
- 요청 내 파일 경로는 요청 디렉터리 하위 상대경로입니다. 상위 이동, 절대경로, 연결/재분석 지점과 파일 역할 간 경로 충돌을 거부합니다.
- 입력 파일은 선언한 개수와 정확한 길이가 같아야 하며 유한 좌표·인덱스 범위·퇴화 점군과 삼각형을 검사합니다.
- 입력 좌표를 코어 `PointCoordinateType=float32`로 명시적으로 변환합니다. 정규화 원점과 단위는 상위 조정자의 책임입니다.

등록 `parameters`는 `adjust_scale`, `min_scale`, `max_scale`, `overlap_ratio`, `filter_out_farthest_points`, `max_iterations`, `sampling_limit`, `max_threads`를 요구합니다. 추가 배율 범위는 양수이며 항등 배율 1을 포함해야 합니다. 겹침 비율은 `[0.1,1]`이며 최소 세 점을 남겨야 합니다. 데이터 점과 점 모델은 표본 제한 이하여야 합니다. 메시 모델의 전체 꼭짓점과 면은 유지합니다. 내부의 임의 재표본화는 허용하지 않습니다. 상위 조정자는 후보 선별 뒤 50,000점 표본의 유효거리 내부 비율을 계산하여 매 등록에 전달하고, `filter_out_farthest_points=true`로 공개 코어 필터를 켭니다.

상위 조정자의 논리 연산 계약은 `PREPARING` 거리 명령 1~2048회 뒤 기준 평가, `RIGID` 겹침 추정 거리·등록·독립 평가, `SCALE_REFINE` 겹침 추정 거리·등록·독립 평가를 요구합니다. 두 메시의 기준·독립 평가는 양방향입니다. 등록 전 겹침 추정과 준비 거리에는 정확 평가 캐시를 사용하지 않습니다. 모든 명령의 식별자·순번과 지오메트리 적재·수명·집계 검증을 유지합니다.

빌드는 oneTBB `2022.0.0` 정확한 버전을 필수로 켜고 QtConcurrent·CGAL을 끕니다. `max_threads`는 `1..1024`의 정수로 검증하며 Windows 프로세서 그룹 전체의 활성 논리 프로세서 수로 제한합니다. 확정한 같은 상한을 등록, C2C·C2M 거리와 MSVC PPL 병렬 정렬에 적용합니다.

## 결과와 중단

논리 연산 성공 상태는 `completed`, 정상 세션 종료는 `closed`, 취소는 `cancelled`, 오류는 `error`입니다. 프로세스 종료 코드는 정상 0, 취소 2, 오류 1입니다. v4 계산 결과는 순번별 응답 파일에 기록하고 전체 세션 결과는 `native/session-result.json`에 기록하며 Python은 stdout을 통신이나 결과 경로로 사용하지 않습니다. 호환 기준선인 v2 `--request`만 단발 결과를 stdout에 반환합니다.

공통 결과는 세션·작업·입력 식별자, 순번, 코어·패치·스레드 식별자와 실행 시간을 포함합니다. 명령별 `timings`는 binary read, float32 변환·검증, mesh load, octree, mesh grid, kernel, serialization을 구분하고, `operation_statistics`는 geometry load/cache hit와 가속구조 생성 횟수를 기록합니다. close 결과는 세션 누계, 최고 작업 집합, 캐시 추정 크기와 geometry별 적재·캐시 적중 통계를 포함합니다. 등록 `matrix_4x4`는 행 우선 `data→model`, 즉 `p'=sRp+T`이며 거리 파일은 입력 순서의 N개 `float64`입니다.

같은 `geometry_id`와 완전히 같은 descriptor는 이미 변환·검증한 `PointCloud`와 선택적 `SimpleMesh`를 재사용합니다. 같은 ID의 다른 descriptor는 거부합니다. `cache_scope=SESSION`인 기본 A/P 모델만 close까지 유지합니다. `cache_scope=COMMAND`인 평가 질의와 등록용 변형 점군은 성공 응답을 원자 기록한 직후 해제하며, 해제한 ID의 재사용은 `GEOMETRY_LIFETIME_EXPIRED`로 거부합니다. 오류·취소는 세션을 즉시 종료하고 레지스트리 소멸자가 남은 객체를 회수합니다. `SimpleMesh`와 참조 `PointCloud`는 같은 캐시 항목이 함께 소유하고, 원본 float64 읽기 버퍼는 float32 변환 직후 해제합니다. 명령은 한 번에 하나만 실행하므로 계산 순서는 기존 Python 조정자가 정한 순서를 그대로 따릅니다.

close 통계의 `cache_resident_estimated_bytes`는 종료 직전 남은 SESSION 항목의 `estimated_bytes` 합계이며, `cache_peak_estimated_bytes`는 명령 전용 객체를 포함한 세션 최고값입니다. geometry별 적재·적중 합계와 실제 물리 명령별 여섯 counter 합계는 Python 계약에서 세션 합계와 교차 검증합니다. 후보 평가의 정확 캐시는 Python이 원시 float64 표본·행렬·평가 계약 바이트가 완전히 같을 때만 사용합니다. 네이티브 응답에는 논리 연산과 실제 물리 명령을 분리해 감사할 수 있는 원본 순번과 평가 키가 남습니다.

Python 조정자는 물리 명령 요청 파일을 원자 기록하기 직전부터 응답과 거리·행렬 검증을 끝낸 직후까지 host 단조 시계로 `roundtrip_seconds`를 측정합니다. `protocol_overhead_seconds`는 이 값에서 네이티브 `elapsed_seconds`를 뺀 비음수 값입니다. 정확 캐시 논리 기록은 원본 물리 측정값을 복사하지만 세션 합계에는 실제 물리 명령을 한 번만 포함합니다. 최종 결과는 물리 왕복·오버헤드 합계, 왕복 대비 비율과 전체 solver 시간 대비 비율을 기록하며 공통 계약과 Blender 복원 경계가 모두 재계산합니다.

`--check-environment`는 하드웨어 논리 프로세서 수를 요청·실제 상한으로 사용해 합성 등록을 실행하고, 실제 로드한 `CCCoreLib.dll`과 `tbb12.dll` 경로 및 두 멀티스레드 지원 플래그를 반환합니다.

코어 공개 콜백은 여러 TBB 스레드가 공유하는 atomic latch를 사용합니다. `GetTickCount64()` 기준 최대 25ms마다 CAS 승자 한 스레드만 `GetFileAttributesW`로 취소 파일을 조회하고, 발견한 취소는 세션 종료까지 유지합니다. 파일 없음·경로 없음 이외의 조회 오류는 `INPUT_IO_ERROR`로 실패합니다. 커널 뒤 성공 응답 직전에는 간격을 무시한 강제 조회를 진행 파일 공개 전후에 수행해 마지막 취소 경쟁도 닫습니다. 코어가 콜백을 확인하지 않는 한 번의 계산 구간에서는 중단까지 시간이 걸릴 수 있으므로 상위 조정자가 2초 유예 후 프로세스 종료를 관리합니다. 진행·최종 JSON 및 거리 배열은 같은 디렉터리의 임시 파일을 원자 교체해 확정합니다.

## 채택한 최적화 경계

역방향 표면 평가는 후보별 전체 이동 메시를 만들지 않습니다. 원본 기준 평가점을 합성 후보의 역행렬로 변환해 SESSION 수명의 원본 이동 메시를 조회하고, CCCoreLib가 제곱근까지 계산한 거리에 후보의 양의 균일 배율을 곱합니다. 기존 후보별 메시 materialize 경로와 배율 0.9·1.0·1.1에서 원시 거리 허용오차 `2e-6` 및 최종 후보·품질 동등성을 확인했습니다.

공개 API의 비교 점군 옥트리 사전 생성은 채택하지 않았습니다. 점군과 메시가 공유하는 union cubic bbox로 두 옥트리를 만든 안전 후보가 거리 바이트는 동일했지만 5회 평균 `0.991572초`로 control `0.924524초`보다 7.25% 느렸습니다. 정확 거리 계산에 필요한 조회점 옥트리는 코어가 매 호출 구성합니다.

0.3.58부터 준비 단계의 연속 거리 명령은 불변 메시·결합 큐브 경계·실제 단계·셀 크기가 같으면 `GridAndMeshIntersection`을 재사용합니다. 세션에 한 개만 보관하고 조건 변경·등록 시작·COMMAND 메시 해제 시 배열까지 반환합니다. 포인터 배열 메모리 예산을 유지하며 원본 면·표본 수와 정확 거리 계산을 바꾸지 않습니다. 실제 생성·적중 횟수와 생성 시간, 상주 메모리를 기존 v4 통계에 기록합니다. 상세 구현은 `mesh-grid-reuse-v1` 패치와 [재빌드 안내](REBUILD.md)에 있습니다.

0.3.59는 자동 검색 단계 선택을 동일 대상의 첫 거리 명령에서만 수행합니다. 선택용 전체 대상·조회점 옥트리는 즉시 해제하고 실제 격자 단계 값 하나만 보관합니다. 후속 조회의 밀도 변화로 자동 단계를 다시 선택하지 않으므로 선택용 옥트리 생성과 격자 단계 변경에 따른 재생성을 제거합니다. 메모리 예산은 계속 매 호출 검사하며, 예산으로 낮아진 단계는 해당 대상의 연속 명령에서 유지합니다. 대상 변경·등록 시작·COMMAND 대상 해제 시 단계도 초기화합니다. 전체 옥트리를 보관하는 캐시가 아니므로 `octree_cache_hits`는 늘리지 않으며, 생략된 선택용 옥트리 생성은 `octree_build_count`에 합산하지 않습니다.

## 스케일 경계 수정

v4의 `farthest-mesh-correspondence-v1`은 최원거리점 제거 뒤에도 메시 최근접점의 삼각형 번호 스칼라 필드를 생성·복사하여 후속 부분 겹침 필터의 잘못된 메모리 접근을 막습니다. 기존 두 패치와 별도 파일·적용 전후 해시로 관리하며, 최종 코어 식별자와 재빌드 원문은 안전 패치를 포함해야 합니다. 독립 워커는 기존 Windows 오류 모드를 보존하면서 치명적 오류 대화상자를 억제하고 실패 종료 코드를 계속 보고합니다.

채택 원문과 수정 원인은 [재빌드 안내](REBUILD.md)에 기록했습니다. 배율 일관성 수정은 `src/RegistrationTools.cpp`만 바꾸는 `bounded-scale-v1`로 유지합니다. v4 세션은 명령을 순차 실행하며, 별도 `multithread-state-race-v1` 패치는 각 명령 내부 TBB 태스크가 공유하는 옥트리 성공 플래그와 C2M 성공·오류 상태만 원자화합니다. 두 패치는 각각 원문·패치·적용 결과 SHA-256을 잠금 파일로 검증하며, 결과 행렬의 배율을 사후에 잘라 맞추는 보정은 수행하지 않습니다.

Windows에서 읽기 핸들이 파일 교체를 잠시 막을 때 시스템 오류 5·32·33에만 20ms 간격, 최대 240ms의 원자 교체 재시도를 수행합니다. 한도를 넘으면 해당 시스템 번호를 `native_code`로 보존한 `OUTPUT_IO_ERROR`를 반환합니다. v4 진행 파일 기록 실패는 명령 응답 또는 `session-result.json`의 오류로 남고, v2 단발 요청은 같은 오류를 stdout 결과에도 반환합니다.
