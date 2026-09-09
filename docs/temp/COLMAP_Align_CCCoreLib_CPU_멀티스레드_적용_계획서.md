# COLMAP Align CCCoreLib CPU 멀티스레드 적용 계획서

- 작성일: 2026-09-04
- 상태: 실험 구현·자동 검증·실데이터 벤치마크 완료, 성능 채택 기준 미달
- 대상: `codex/align-test-env` 브랜치의 독립 실험 애드온 `colmap_align_test_env`
- 소스: `E:\DEV\DEV_RND\colmap_align_test_env\source`
- 목적: CCCoreLib 정렬 결과와 현재 UX를 유지하면서 등록·거리 계산의 CPU 병렬 처리를 활성화하고, 사용자가 작업당 최대 스레드 수를 제어할 수 있게 한다.
- 관련 문서: [CCCoreLib 엔진 교체 계획서](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/COLMAP_Align_CCCoreLib_%EC%97%94%EC%A7%84%EA%B5%90%EC%B2%B4_%EA%B3%84%ED%9A%8D%EC%84%9C.md), [구현·검증 기록](COLMAP_Align_CCCoreLib_구현검증.md), [ADR-0010](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/decisions/0010-cccore-align-test-runtime.md)

## 1. 결정 요약

1. CCCoreLib의 병렬 백엔드는 **oneTBB**를 사용한다. QtConcurrent와 CGAL은 계속 끈다.
2. 기본 사용자 설정은 `CPU 스레드: 자동`으로 두고, 자동 모드는 감지한 논리 프로세서에서 하나를 남긴 수를 작업 상한으로 사용한다.
3. 사용자는 Add-on Preferences에서 `자동`, `1`, `2` 이상의 최대 스레드 수를 선택할 수 있다. `1`은 재현·비교·문제 진단을 위한 명시적 단일 스레드 모드다.
4. 설정은 Align 실행을 시작할 때 한 번 고정한다. 실행 중 설정을 바꿔도 현재 작업에는 영향을 주지 않고 다음 작업부터 적용한다.
5. TBB는 CCCoreLib의 점군 거리, 점군-삼각면 거리와 옥트리 셀 처리를 병렬화한다. Windows MSVC 병렬 정렬은 같은 상한으로 별도 제한한다.
6. 새 UI는 레이어·마커·Align 패널에 추가하지 않는다. 기기 자원 설정이므로 기존 `CC 정렬 환경 검사`가 있는 Add-on Preferences에 둔다.
7. 정렬 알고리듬, 표본 수, 겹침 비율, ±10% 스케일, 후보 평가, 미리보기·승인·취소·Undo 흐름은 변경하지 않는다.
8. 단일 스레드와 멀티스레드의 수치 동등성, 취소 안정성, 최대 메모리와 실데이터 처리 시간을 통과한 뒤에만 자동 모드를 기본으로 채택한다.

CCCoreLib는 CMake에서 TBB와 QtConcurrent를 병렬 처리 선택지로 제공하며, 현재 고정 코어의 거리 계산은 `maxThreadCount`를 TBB `task_arena`의 동시성 상한에 전달한다. [CCCoreLib CMake](https://github.com/CloudCompare/CCCoreLib/blob/master/CMakeLists.txt), [oneTBB task_arena](https://uxlfoundation.github.io/oneTBB/main/specification/source/task_scheduler/task_arena/task_arena_cls.html)

## 2. 현재 병목과 비활성 지점

현재 요청 계약에는 `max_threads`가 있지만 실제 연산은 항상 한 스레드로 고정된다.

| 위치 | 현재 상태 | 영향 |
| --- | --- | --- |
| `tools/align/alignment/icp_cccore_runtime.py` | `cc_max_threads=1` 고정 | 모든 Align 작업이 1을 요청한다. |
| `tools/align/alignment/icp_cccore_contract.py` | 양의 정수를 검증 | 값 전달 계약은 이미 있으나 자동 설정과 기기 상한이 없다. |
| `tools/align/native/cccore_worker/CMakeLists.txt` | `CCCORELIB_USE_TBB=OFF`, `CCCORELIB_USE_QT_CONCURRENT=OFF` | CCCoreLib의 점 거리 병렬 코드가 빌드되지 않는다. |
| `tools/align/native/cccore_worker/main.cpp` 등록 | 요청값을 읽은 직후 `params.maxThreadCount=1`로 덮어쓴다. | 등록 반복의 대응 거리 계산이 단일 스레드다. |
| 같은 파일의 거리 평가 | `multiThread=false`, `maxThreadCount=1` | 후보별 독립 거리 평가가 단일 스레드다. |
| 같은 파일의 `wmain` | MSVC Concurrency Scheduler 최대 동시성 1 | 옥트리 생성 중 병렬 정렬도 한 스레드다. |
| `build_tools/prepare_cccore_runtime.ps1` | TBB 비활성 빌드와 `effective_max_threads=1` 기록 | 배포 바이너리와 Manifest가 단일 스레드 계약이다. |

한 Align 작업은 형상 조합에 따라 등록 2회와 거리 평가 여러 회를 순차 실행한다. 각 네이티브 호출은 별도 프로세스이므로 호출끼리 동시에 실행하지 않으며, 이번 변경은 **한 호출 안의 거리·옥트리 계산**을 병렬화한다.

## 3. 범위

### 3.1 포함

- oneTBB Windows x64 런타임의 버전·다운로드·SHA-256·라이선스 고정
- CCCoreLib TBB 빌드 활성화와 누락 시 구성 단계 실패
- 자동·수동 최대 CPU 스레드 설정
- Python 작업 계약에서 네이티브 실행 파일까지 설정 전달
- 등록, 점군-점군 거리, 점군-메시 거리와 MSVC 병렬 정렬에 동일 상한 적용
- 멀티스레드 진행률·취소 상태의 데이터 경쟁 제거
- 요청값, 감지값, 실제 사용값, TBB 버전과 지원 여부 기록
- 단일/멀티 결과 동등성, 취소, 패키징, 성능·메모리 검증

### 3.2 제외

- GPU/CUDA 정렬
- CCCoreLib 외 엔진 추가
- 등록 2회 또는 평가 횟수 변경
- 네이티브 프로세스 상주화와 옥트리 재사용
- 메시·점군 표본 수, 품질 판정과 ±10% 스케일 정책 변경
- 레이어, 마커, 정렬 승인 UX 변경
- `main` 브랜치 통합

프로세스 상주화와 옥트리 캐시는 멀티스레드 효과를 측정한 뒤 별도 계획으로 다룬다. 이번 성능 비교에서 계산 횟수와 입력을 고정해야 스레드 병렬화의 효과를 분리할 수 있다.

## 4. 사용자 설정과 UI

### 4.1 위치

`Preferences → Add-ons → colmap_align_test_env`의 기존 CC 정렬 환경 영역에 다음 항목을 추가한다.

```text
CC 정렬 실행 환경이 애드온에 포함되어 있습니다.
CPU 스레드        [자동]
                  감지 16개 · 다음 작업에서 최대 15개 사용
[CC 정렬 환경 검사]
```

- 속성명: `cc_cpu_thread_limit`
- 표시명: `CPU 스레드`
- 저장 위치: `AddonPreferences`
- 값 범위: `0..1024`
- `0`: 자동
- `1`: 단일 스레드
- `2..1024`: 사용자가 지정한 최대 스레드 수

이 값은 기기별 실행 자원 설정이다. `.blend`의 Scene 설정이나 프로젝트 저장 스키마에는 넣지 않는다. 실제 작업에 사용한 값은 결과 출처 메타데이터에 남긴다.

### 4.2 자동값 결정

Blender 쪽에서 작업 시작 시 다음 값으로 해석한다.

```text
감지 스레드 = max(1, os.cpu_count() 또는 1)
자동 요청값 = min(1024, max(1, 감지 스레드 - 1))
수동 요청값 = max(1, min(설정값, 감지 스레드, 1024))
```

자동 모드는 Blender UI가 반응할 여유로 논리 프로세서 하나를 남긴다. 실제 네이티브 프로세스는 Windows의 활성 프로세서 수를 다시 확인하고 요청값을 한 번 더 제한한다.

```text
실제 사용값 = max(1, min(요청값, Windows 활성 논리 프로세서 수, 1024))
```

Python과 네이티브 감지값이 다르면 네이티브 값이 최종 권위가 된다. 작업은 실패시키지 않고 요청값과 실제값을 모두 결과에 기록한다.

### 4.3 실행 중 변경

- Align 시작 시 해석된 양의 정수값을 `CcCoreRuntimeParameters`에 저장한다.
- 해당 값은 Parameter Fingerprint와 Input Fingerprint에 포함한다.
- Preferences 변경은 실행 중인 Manifest와 네이티브 요청을 바꾸지 않는다.
- 완료 화면에는 상시 행을 추가하지 않는다. 상세 진단과 저장된 출처에는 `CPU 15스레드 · oneTBB`처럼 기록한다.
- 환경 검사 성공 메시지에는 병렬 지원 여부와 자동 해석값을 표시한다.

## 5. 병렬 백엔드와 배포

### 5.1 oneTBB 채택

독립 네이티브 워커는 GUI 이벤트 루프가 없으므로 QtConcurrent 대신 oneTBB를 사용한다. 공식 Windows x64 릴리스 패키지를 고정하고 실행 중 다운로드하지 않는다.

- 1차 고정 버전: oneTBB `2022.0.0`
- 공식 Windows ZIP SHA-256: `660f3555de26bee8cd4daef1105d5d9e93e695b2fcfa9c57db3aa38c6afe5675`
- 라이선스: Apache-2.0
- 고정 대상: 다운로드 URL, 버전, ZIP SHA-256, 런타임 DLL SHA-256, 라이선스 파일 SHA-256

이 버전은 고정 CCCoreLib가 요구하는 TBB 2022 계열의 최소 버전과 맞춘다. 구현 시 실제 CMake Package 탐색과 Visual Studio 2022 ABI를 확인한다. 공식 릴리스는 Windows 패키지 해시를 게시하고 있다. [oneTBB 공식 릴리스](https://github.com/uxlfoundation/oneTBB/releases/tag/v2022.0.0), [oneTBB 라이선스](https://github.com/uxlfoundation/oneTBB)

### 5.2 빌드 설정

`tools/align/native/cccore_worker/CMakeLists.txt`는 다음 계약으로 바꾼다.

```text
CCCORELIB_USE_TBB=ON
CCCORELIB_USE_QT_CONCURRENT=OFF
CCCORELIB_USE_CGAL=OFF
CCCORELIB_SHARED=ON
```

- 준비 스크립트가 검증된 TBB Package 경로를 `TBB_DIR`로 전달한다.
- `TBB::tbb` Target 또는 CCCoreLib의 `CC_CORE_LIB_USES_TBB` 정의가 없으면 CMake 단계에서 실패한다.
- TBB를 찾지 못했는데 단일 스레드 빌드로 조용히 내려가는 동작은 허용하지 않는다.
- Release 빌드 뒤 `CCCoreLib.dll`과 `colmap_cccore_worker.exe`가 TBB 런타임을 실제로 로드하는지 환경 검사로 확인한다.

### 5.3 패키지 구성

`runtime/cccore/`에 다음 파일을 포함한다.

```text
colmap_cccore_worker.exe
CCCoreLib.dll
tbb12.dll
기존 Microsoft C++ 런타임 DLL
licenses/oneTBB-Apache-2.0.txt
runtime_manifest.json
cccore_rebuild_sources.zip
```

실제 링크 결과에서 추가 TBB DLL이 확인되면 추측으로 복사하지 않고 PE 의존성을 검사해 Manifest와 패키지 목록에 명시한다. 재빌드 원문 ZIP에는 같은 TBB 패키지 또는 고정 원문과 오프라인 CMake Package 정보를 포함해 기존 오프라인 재빌드 계약을 유지한다.

## 6. 네이티브 실행 계약

### 6.1 프로토콜과 버전

병렬화는 같은 입력에서도 부동소수점 처리 순서와 성능 특성을 바꾸므로 새 실행 계약으로 식별한다.

- 알고리듬: `cccore-icp-v2`
- Python 작업 프로토콜: `cccore-job-v2`
- 네이티브 프로토콜: `cccore-native-v2`
- 프로젝트 저장 스키마: 현행 `8` 유지

기존 확정 결과는 저장된 `cccore-icp-v1` 출처와 행렬을 그대로 복원한다. 새 Align만 v2로 실행한다. 프로젝트 데이터 형태는 바뀌지 않으므로 저장 스키마를 올리지 않는다.

### 6.2 스레드 수 적용

네이티브 워커는 요청 JSON을 검증한 뒤 CCCoreLib를 처음 호출하기 전에 실제 스레드 수를 확정한다.

1. `max_threads`가 양의 정수인지 검사한다.
2. Windows 활성 논리 프로세서 수와 비교해 실제값을 제한한다.
3. 등록의 `ICPRegistrationTools::Parameters.maxThreadCount`에 실제값을 전달한다.
4. 독립 거리 평가에서 `multiThread = effective_max_threads > 1`과 `maxThreadCount = effective_max_threads`를 설정한다.
5. MSVC `Concurrency::Scheduler`의 `MaxConcurrency`도 같은 값으로 프로세스당 한 번 초기화한다.
6. 한 네이티브 프로세스는 요청 하나만 처리하고 종료하므로 작업 도중 Scheduler 상한을 바꾸지 않는다.

TBB 쪽 거리 연산은 CCCoreLib가 생성하는 지역 `task_arena(maxThreadCount)`로 제한한다. MSVC의 `parallel_sort`는 TBB와 동시에 실행되는 단계가 아니며, 같은 상한으로 제한해 예상하지 않은 CPU 독점을 막는다.

### 6.3 결과 메타데이터

환경 검사와 모든 등록·거리 결과에 다음 값을 포함한다.

```json
{
  "thread_backend": "oneTBB",
  "tbb_version": "2022.0.0",
  "requested_max_threads": 15,
  "effective_max_threads": 15,
  "hardware_thread_count": 16,
  "distance_multithread_support": true,
  "octree_multithread_support": true
}
```

Python 조정자는 각 순차 호출에서 `thread_backend`, `effective_max_threads`, 코어 커밋과 DLL 지문이 같아야 결과를 수용한다. 최종 메타데이터에는 네이티브 호출별 시간과 총 시간을 구분해 기록한다.

## 7. 진행률·취소·동시성 안전성

CCCoreLib 멀티스레드 경로에서는 여러 작업 스레드가 진행 콜백과 취소 상태를 읽을 수 있다. 다음 상태를 명시적으로 안전하게 만든다.

- `Progress::update()`의 파일 기록 간격, 최신 진행률과 이전 기록 시점은 현재 Mutex 보호를 유지한다.
- `outputFailed`와 `outputErrorCode`는 모든 읽기·쓰기를 같은 Mutex로 보호하거나 원자 타입으로 바꾼다.
- `isCancelRequested()`는 데이터 경쟁 없이 출력 실패와 취소 파일을 읽는다.
- 진행 JSON은 현재처럼 최대 100ms 간격으로 한 스레드만 원자 교체한다.
- 취소 파일은 어느 작업 스레드에서 확인해도 같은 `CANCELLED` 결과로 수렴해야 한다.
- CCCoreLib 점군-메시 병렬 구현의 공유 성공 상태와 오류 번호는 고정 소스에서 동기화 없이 읽고 쓰는 지점을 감사한다. 실제 데이터 경쟁이 확인된 필드만 최소 패치로 원자화하고 패치 ID·원문 해시·변경 고지를 갱신한다.
- 여러 Align 작업을 한 네이티브 프로세스에서 동시에 실행하지 않는다. Blender 인스턴스가 여러 개이면 각각 독립 프로세스와 TBB Arena를 사용한다.

취소 응답 시간은 멀티스레드에서도 기존처럼 파일 신호 후 정상 중단을 우선하고, 2초가 지나면 상위 Python 조정자가 자식 프로세스를 종료하는 계약을 유지한다.

## 8. 수치 결과와 재현성

표면 표본의 고정 시드와 입력 순서는 그대로 유지한다. 병렬 실행에서는 처리 순서에 따른 미세한 부동소수점 차이가 생길 수 있으므로 JSON 바이트 일치로 동등성을 판정하지 않는다.

단일 스레드와 N스레드 비교 기준:

- 합성 정답 Fixture는 기존 회전·이동·스케일 허용오차를 모두 통과한다.
- 최종 추가 배율 차이는 `1e-5` 이하로 유지한다.
- 정규화 공간에서 두 결과가 이동 표본에 만드는 위치 차이 RMS는 형상 대각선의 `1e-4` 이하로 유지한다.
- 독립 평가 `mean_rms`의 상대 차이는 `0.1%` 이하로 유지한다.
- 품질 상태 `PASS/REVIEW`와 최종 후보가 달라지면 임계값 근처의 수치 차이인지 원인을 기록하고 자동 채택하지 않는다.
- NaN, 음수 거리, 대응 수 변화, ±10% 경계 초과, 비균일 스케일과 반사는 기존처럼 실패한다.

허용오차를 넓혀 테스트를 통과시키지 않는다. 현재 허용오차 안에서 차이가 큰 데이터는 멀티스레드 채택을 중단하고 거리 단계와 후보별 결과를 비교한다.

## 9. 성능·메모리 정책

### 9.1 측정 대상

각 Fixture를 `1`, `2`, `4`, `8`, `자동` 스레드로 최소 3회 실행하고 첫 실행과 반복 실행을 구분한다.

| 입력 | 확인 목적 |
| --- | --- |
| 점군-점군 | 옥트리 생성과 최근접점 거리 병렬화 |
| 점군-표면 | 등록 중 점-삼각면 거리 병렬화 |
| 표면-점군 | 역방향 등록과 결과 역행렬 계약 |
| 표면-표면 | 등록 2회와 양방향 평가가 있는 최중요 경로 |
| 64,800 삼각면·30,000 평가점 Fixture | 기존 네이티브 성능 회귀와 비교 |
| 사용자 대형 RC OBJ/COLMAP | 실제 총 대기 시간, 피크 메모리와 UI 반응성 |

각 실행에서 다음 값을 기록한다.

- 전체 Align 벽시계 시간
- 준비, Baseline, Rigid, Rigid Evaluation, Scale Refine, Refine Evaluation 시간
- 각 네이티브 호출의 `elapsed_seconds`
- 요청·실제 스레드 수
- 네이티브 프로세스 피크 Working Set
- 성공·취소 상태와 최종 평가 수치

### 9.2 메모리 주의점

CCCoreLib의 병렬 점군-메시 거리 경로는 작업 스레드마다 삼각면 처리 Bit Array와 셀 임시 배열을 사용할 수 있다. 따라서 스레드 수 증가가 처리 시간뿐 아니라 피크 메모리에도 미치는 영향을 측정한다.

- 자동 모드의 피크 메모리는 같은 입력의 1스레드 대비 증가량과 절대값을 함께 기록한다.
- 메모리 부족이나 Paging으로 자동 모드가 느려지면 임의의 고정 상한을 바로 넣지 않는다.
- `2/4/8/자동` 측정으로 가장 안정적인 기본 상한 규칙을 결정하고, 규칙이 바뀌면 Preferences 설명과 계획서를 함께 갱신한다.
- 사용자는 언제든 `1`로 되돌려 같은 작업을 실행할 수 있어야 한다.

### 9.3 성능 채택 기준

대표 표면-표면 실데이터에서 다음 기준을 모두 만족해야 한다.

- 4스레드의 네이티브 계산 시간이 1스레드보다 최소 `1.5배` 빠르다.
- 자동 모드의 전체 Align 시간이 1스레드보다 최소 `1.25배` 빠르다.
- 대형 입력의 자동 모드 피크 메모리가 시스템 Paging을 유발하지 않는다.
- Blender UI 진행 표시와 취소가 계속 동작한다.
- 작은 입력이 1스레드보다 느려져도 절대 증가가 `0.2초` 이하면 허용한다.

성능 기준을 만족하지 못하면 TBB 설정, 병렬화된 단계와 호출별 시간을 확인한다. 검증 없이 표본 수나 평가 횟수를 줄여 목표 시간을 맞추지 않는다.

## 10. 파일별 변경 계획

| 파일 | 계획 |
| --- | --- |
| `tools/align/properties.py` | `cc_cpu_thread_limit` Add-on Preference, 자동값 해석과 UI 설명 추가 |
| `tools/align/alignment/icp_cccore_runtime.py` | 실행 시 실제 요청 스레드 고정, Parameter Fingerprint 포함, 환경 검사 표시 정보 연결 |
| `tools/align/alignment/icp_cccore_contract.py` | v2 식별자, 양의 요청값과 결과 스레드 메타데이터 검증 |
| `tools/align/alignment/icp_cccore_solver.py` | 모든 순차 네이티브 호출의 스레드 계약 일치 검사와 호출별 시간 집계 |
| `tools/align/alignment/icp_solver_backend.py` | CCCoreLib 알고리듬 버전을 v2로 갱신 |
| `tools/align/native/cccore_worker/main.cpp` | 실제값 결정, 등록·거리·PPL 상한 적용, 환경 정보와 스레드 안전한 진행·취소 |
| `tools/align/native/cccore_worker/CMakeLists.txt` | TBB 활성화, Qt 비활성 유지, TBB Target 누락 시 구성 실패 |
| `tools/align/native/cccore_worker/dependencies.lock.json` | oneTBB 버전·URL·SHA-256·라이선스 고정, 프로토콜 v2 |
| `build_tools/prepare_cccore_runtime.ps1` | TBB 준비·검증·CMake 연결·DLL/라이선스/원문 패키징·Manifest 갱신 |
| `tools/align/native/cccore_worker/README.md` | 자동·수동 스레드와 결과 필드 계약 문서화 |
| `tools/align/native/cccore_worker/REBUILD.md` | 온라인 준비와 완전 오프라인 재빌드 절차 갱신 |
| `tools/align/native/cccore_worker/THIRD_PARTY_NOTICES.md` | oneTBB 저작권·라이선스·배포 파일 추가 |
| `tests/align/native/test_cccore_worker.py` | 1/N 스레드 등록·거리 동등성, 지원 정보, 취소·오류 회귀 |
| `tests/align/test_icp_cccore_solver.py` | 요청 전달, 호출별 실제값 일치, v2 결과와 수치 동등성 검사 |
| `tests/align/blender/test_icp_cccore_ui.py` | Preferences 표시, 자동·수동 해석과 실행 중 스냅샷 검사 |
| `test_env/cccore_checks.py` | 설치본의 TBB DLL·지원 정보·네 형상 조합 확인 |
| `test_env/manage.py` | TBB 런타임·라이선스·Manifest 해시를 실험 패키지 필수 항목으로 검사 |
| `docs/TESTING.md` | 단일/멀티 비교와 성능 측정 명령 추가 |
| `docs/decisions/0010-cccore-align-test-runtime.md` | 검증 완료 뒤 연산 동시성 1 결정을 v2 정책으로 갱신 |

원본 CCCoreLib에 동시성 안전 패치가 필요하다고 확인될 경우 기존 배율 패치와 섞어 덮어쓰지 않는다. 별도 패치 파일과 Patch ID로 추적하고, 적용 순서와 각 단계의 정규화 SHA-256을 잠금 파일에 기록한다.

## 11. 구현 순서

### 1단계: 단일 스레드 기준선 고정

1. 현재 커밋의 네 형상 조합과 실제 표면-표면 입력을 1스레드로 실행한다.
2. 단계별 시간, 최종 행렬, 후보, 독립 평가, 피크 메모리를 저장한다.
3. 현행 바이너리와 Manifest SHA-256을 기준 보고서에 기록한다.

### 2단계: TBB 공급망과 빌드

1. oneTBB 공식 ZIP, 라이선스와 DLL을 잠금 파일에 추가한다.
2. 준비 스크립트에서 해시를 검사하고 격리 Cache만 사용한다.
3. CMake에서 TBB를 필수로 활성화한다.
4. 설치 경로에서 의존 DLL과 `MultiThreadSupport()`를 확인한다.
5. 오프라인 재빌드를 통과시킨다.

### 3단계: 네이티브 스레드 계약

1. 요청값과 하드웨어값으로 실제값을 결정한다.
2. 등록·거리·MSVC Scheduler에 같은 값을 적용한다.
3. 진행·취소 공유 상태를 스레드 안전하게 만든다.
4. 요청·실제값과 TBB 지원 정보를 결과에 기록한다.
5. 네이티브 프로토콜을 v2로 올리고 이전 실행 파일 혼용을 거부한다.

### 4단계: Blender 설정 연결

1. Add-on Preferences에 CPU 스레드 속성을 추가한다.
2. 자동값을 계산하고 Align 시작 시 고정한다.
3. Manifest와 지문에 실제 요청값을 포함한다.
4. 환경 검사와 결과 출처에 사용값을 표시한다.
5. 실행 중 Preferences 변경이 현재 작업을 바꾸지 않는지 확인한다.

### 5단계: 수치·취소 회귀

1. 1/2/4/8스레드에서 점군·메시 네 조합을 비교한다.
2. 배율 상하한, 역방향, 오류 입력, 진행 파일 잠금과 취소를 반복한다.
3. 병렬 결과의 행렬·평가 수치가 동등성 기준을 만족하는지 검사한다.
4. 실패 시 다른 엔진이나 단일 스레드로 자동 재실행하지 않고 명시적 오류로 종료한다.

### 6단계: 실데이터 성능 검증

1. 사용자 대형 RC 메시와 COLMAP 데이터에서 스레드 수별 3회 측정한다.
2. 호출별 병목, CPU 사용, 피크 메모리와 UI 취소 반응을 확인한다.
3. 자동 모드가 채택 기준을 만족하면 기본값을 확정한다.
4. 기준 미달이면 자동 기본값을 1로 유지하고 측정 결과를 문서에 남긴다.

### 7단계: 격리 설치본 검증

1. `test_env/manage.py verify --gui`로 스테이징·설치·등록·GUI 검사를 실행한다.
2. 설치본에 고정 TBB DLL과 라이선스가 있고 개발 Cache에 의존하지 않는지 확인한다.
3. 네 형상 조합의 Align·미리보기·승인·Undo/Redo와 OBJ 원본 토폴로지 보존을 재검증한다.
4. 결과를 실험 루트 `outputs/`에 저장하고 소스 트리는 임시 로그 없이 정리한다.

## 12. 검증 항목

### 12.1 정적·계약 검사

- TBB ON, QtConcurrent OFF, CGAL OFF
- TBB 누락 시 CMake 즉시 실패
- v1 Python과 v2 네이티브 또는 반대 조합 거부
- `0`, 음수, 논리값, 실수와 지나치게 큰 네이티브 요청 거부
- 사용자가 하드웨어보다 큰 값을 지정하면 실제값으로 제한하고 기록
- 등록·거리 모든 호출의 실제값 일치
- Parameter Fingerprint에 요청 스레드 수 포함

### 12.2 네이티브 정확도

- 점군 강체·유사변환 1/N 동등성
- 점군-메시 내부 거리 1/N 동등성
- 64,800 삼각면·30,000 점 거리 1/N 동등성
- ±10% 및 역방향 스케일 경계
- 비유한 좌표, 공선점, 잘못된 삼각면과 파일 경로 오류
- 1/N스레드 실행 중 취소
- 진행 JSON 단기·장기 공유 잠금

### 12.3 Blender·UX

- Add-on Preferences 기본값 `자동`
- 감지값이 없을 때 1로 안전 해석
- 수동 1과 N 설정 전달
- 실행 중 설정 변경 시 현재 작업 불변
- 기존 Align 패널과 레이어 행의 배치 불변
- 작업 완료 출처에 요청·실제값과 TBB 버전 기록
- 환경 검사에서 TBB DLL과 병렬 지원 확인

### 12.4 패키징

- 설치 ZIP에 필요한 TBB DLL과 라이선스 포함
- 개발 Cache 절대경로 부재
- PE 의존 DLL 전부 포함
- Runtime Manifest의 모든 바이너리 SHA-256 일치
- 재빌드 원문 ZIP으로 네트워크 없는 Release 빌드 성공
- 애드온 비활성화·재활성화와 Blender 재실행 후 동일 설정 유지

### 12.5 회귀

- 점군-점군, 점군-표면, 표면-점군, 표면-표면
- 마커 초기 정렬이 있는 경로와 현재 위치에서 직접 실행하는 경로
- 미리보기 버리기·승인·취소·강제 종료
- 승인 뒤 P의 A 편입과 Undo/Redo
- COLMAP·OBJ 축, 카메라와 Export 행렬 불변
- 내장 OBJ Triangle·Quad·N-gon 토폴로지 보존
- 외부 OBJ가 없는 `.blend` 복원과 Export

## 13. 완료 기준

1. 설치된 CC 런타임이 oneTBB 병렬 지원을 실제로 보고한다.
2. 자동 모드는 논리 프로세서 하나를 남긴 양의 스레드 상한으로 실행된다.
3. 사용자가 `1` 또는 원하는 최대값을 Add-on Preferences에서 정할 수 있다.
4. 등록과 모든 독립 거리 호출이 같은 실제 스레드 상한을 사용한다.
5. 네이티브 요청과 결과에 요청값·하드웨어값·실제값·TBB 버전이 남는다.
6. 멀티스레드 진행·취소에 데이터 경쟁, 잔류 프로세스와 손상된 결과 파일이 없다.
7. 네 형상 조합이 단일 스레드 대비 수치 동등성 기준을 통과한다.
8. 대표 표면-표면 실데이터가 정한 성능·메모리 채택 기준을 통과한다.
9. 기존 레이어·마커·Align·미리보기·승인·Undo/Redo·Export UX가 유지된다.
10. TBB DLL, 라이선스, 고정 해시와 오프라인 재빌드 원문이 실험 설치본에 포함된다.
11. 실패 시 다른 엔진으로 자동 전환하거나 결과를 조용히 단일 스레드로 재계산하지 않는다.
12. 변경은 `codex/align-test-env`에만 남고 `main`은 수정하지 않는다.

## 14. 폐기와 복귀

- Preferences에서 `CPU 스레드=1`을 선택하면 같은 v2 바이너리로 단일 스레드 진단이 가능하다.
- TBB 빌드 자체에 문제가 있으면 멀티스레드 변경 커밋을 되돌리고 기존 `cccore-icp-v1` 런타임과 Manifest를 재설치한다.
- 실험 결과가 만족스럽지 않으면 `codex/align-test-env`의 해당 커밋만 폐기하며 `main`과 사용자 원본 데이터에는 영향이 없다.
- 성능 검증 전에는 기존 v1 바이너리를 덮어쓴 결과만 남기지 않고, 기준선 보고서에 이전 해시와 측정값을 보존한다.

## 15. 구현·검증 결과

### 15.1 구현 결과

- oneTBB `2022.0.0`을 필수로 연결한 `cccore-native-v2` 런타임을 만들었다.
- Add-on Preferences에 `CPU 스레드`를 추가했고 `0=자동`, `1=단일`, `2..1024=수동 상한`으로 해석한다.
- 이 장비의 논리 프로세서 32개에서 자동 요청은 31이며, 작업 시작 시 고정되어 모든 등록·거리 호출과 결과 출처에 기록된다.
- 등록, C2C·C2M 거리와 MSVC 병렬 정렬에 같은 실제 상한을 전달한다.
- TBB 내부 공유 성공·오류 상태와 진행 출력 실패 상태를 원자화한 최소 패치를 고정했다.
- 기존 `cccore-icp-v1` 스키마 8 결과 복원은 유지하고 새 작업만 `cccore-icp-v2`로 실행한다.
- 최종 런타임은 Manifest 19개와 실제 파일 19개가 일치하며, 실행 파일·DLL·라이선스·원문 ZIP·네이티브 보고서의 선언 해시를 모두 확인했다.

최종 런타임의 주요 SHA-256은 다음과 같다.

| 파일 | SHA-256 |
| --- | --- |
| `colmap_cccore_worker.exe` | `03d489eafcb397639dc2730368d254cc39f12a68808e60568f9da0d993198b7e` |
| `CCCoreLib.dll` | `e6b335b9eabcfb44f0b6f0ecb3e69cbbe9cb2e3915f77a2d371270005473ef2a` |
| `tbb12.dll` | `6269822eb10b9de2e63812fd52b96c4d93d5ad53835760192f5c892e37d8e52d` |
| `cccore_rebuild_sources.zip` | `dd03b44d437324354cb932af4dc5dd282c7cc591aa7110b398ea2aaaa8f26227` |
| `runtime_manifest.json` | `4884d9d35f96a2e8b76b796e4ca3df005ef60140a59fd02ccafc7e4aac1b81e3` |

자동 검증은 네이티브 통합 9개, 워커 20개, Blender UI 9개, Blender 런타임 6개로 총 44개를 실행했다. 실패·오류·건너뜀은 모두 0이며, 네 형상 조합과 1/N 결과 동등성, 취소·강제 종료의 자식 회수까지 포함한다.

### 15.2 `Untitled.blend` 벤치마크

입력은 `C:\Users\gksru\Desktop\Untitled.blend`이며 162,229,460바이트, SHA-256 `3d483752369fd2db59ed1422efb43349e361724795264d5baa99b36db15ed8af`다. A는 1,781,383정점·3,552,808삼각면, P는 78,130정점·155,674삼각면의 표면-표면 조합이다. 같은 고정 소스·런타임, 초기 행렬, 50,000 등록 표본, 30,000 평가 표본과 난수 시드를 사용했다.

| 측정 | 전체 Align | 워커 | 네이티브 8회 합계 | 비고 |
| --- | ---: | ---: | ---: | --- |
| v2 1스레드 | 96.0219초 | 94.2660초 | 92.5135초 | 주 기준선 |
| v2 자동 31, 첫 실행 | 90.5263초 | 88.7820초 | 87.2753초 | CPU 피크 표본만 측정기 오류로 제외 |
| v2 자동 31, 수정 측정기 | 90.4438초 | 88.4530초 | 87.0021초 | 주 비교값 |
| 이전 v1 1스레드 | 94.2674초 | 92.5470초 | 91.0075초 | 역사적 참고값, 주 비교에서 제외 |

같은 v2의 1스레드 대비 자동 31스레드는 전체 시간을 5.5781초, 5.81% 줄여 `1.0617배` 빨랐다. 두 31스레드 전체 시간 차이는 0.0825초이고 평균은 90.4850초다.

| 네이티브 단계 | v2 1스레드 | 자동 31 평균 | 속도 |
| --- | ---: | ---: | ---: |
| Baseline 양방향 평가 | 20.4561초 | 19.8353초 | 1.0313배 |
| Rigid 등록 | 15.2302초 | 13.0868초 | 1.1638배 |
| Rigid 양방향 평가 | 20.3986초 | 20.1088초 | 1.0144배 |
| Scale 등록 | 15.5361초 | 13.4078초 | 1.1587배 |
| Scale 양방향 평가 | 20.8925초 | 20.7001초 | 1.0093배 |

수정 측정기에서 평균 등가 CPU 사용량은 1스레드 0.9766코어, 자동 31스레드 1.2850코어였다. 자동 모드는 짧은 구간에서 30.1590코어까지 사용했지만, 전체 시간의 대부분을 차지한 대형 메시 준비·옥트리 구축·교차 격자 구성과 평가 전후 작업은 직렬 구간이라 평균 병렬도와 총 단축폭이 작았다.

동시 작업 트리의 피크 Working Set은 1스레드 888,033,280바이트, 자동 31스레드 893,095,936바이트로 4.8281MiB 증가했다. 피크 Private Bytes 증가는 27.9727MiB였으며 Paging은 관측되지 않았다.

1스레드와 두 번의 자동 31스레드 실행은 최종 행렬, Baseline·최종 지표, RMS, 품질 `REVIEW`, 최종 후보 `baseline`이 모두 완전히 같았다. 행렬과 지표의 최대 절대 차이는 0이다. 이 파일은 시작 위치가 이미 최적이라 반환 변화량도 항등 행렬이었다. 모든 실행에서 Repository 행렬을 적용하거나 승인하지 않았고 원본 파일의 SHA-256·크기·mtime은 변하지 않았다.

상세 결과는 실험 루트의 `outputs/untitled_thread_benchmark_20260904_run01/comparison.json`과 각 실행의 `result.json`, `job_records/`에 있다.

### 15.3 판정

수치 안정성과 메모리 기준은 통과했다. 전체 `1.0617배`는 계획의 자동 모드 채택 기준 `1.25배`에 미달한다. 따라서 이번 결과만으로 main 통합이나 최종 자동 상한을 확정하지 않는다. 실험 애드온에서는 자동 31과 수동 1~1024 선택을 유지해 다른 실제 오정렬 데이터로 계속 비교할 수 있다.

병목은 TBB가 적용되는 셀 거리 커널보다 호출마다 반복되는 대형 메시 적재·삼각면 검증·옥트리 및 메시 교차 격자 생성에 있다. 다음 성능 작업의 구체적인 구조와 단계별 채택 기준은 [단일 세션 워커·지오메트리 캐시 구조 변경 계획서](COLMAP_Align_CCCoreLib_단일세션워커_지오메트리캐시_계획서.md)에 정리한다.
