# COLMAP Align 실험 환경의 main 승격 검토

- 검토일: 2026-09-05
- 상태: 통합 준비 가능, 현재 상태 그대로 정식 배포는 보류
- 범위: 로컬 Git 이력, 정식/실험 패키징·등록 경계, 저장 형식, 기존 검증 산출물 대조
- 이번 검토에서는 제품 코드 수정, 브랜치 생성, 병합, 설치 및 벤치마크 재실행을 하지 않았다.
- 후속 사용자 결정: 현재 `.blend`는 보존할 필요가 없는 테스트 파일이다. 백업 필수 조건은 해제하며, 구체적인 실행 순서는 [마이그레이션 계획서](COLMAP_Align_main_마이그레이션_계획서.md)를 따른다.

## 1. 결론

실험 Align 기능을 정식 COLMAP Tools의 Align으로 승격하는 것은 가능하다. main과 실험 사이의 코드 충돌보다 **정식 CC 런타임 패키징과 통합 환경설정 연결, 최종 배포본 검증**이 남은 핵심 작업이다.

`colmap_align_test_env` 설치 폴더나 실험 애드온의 이름을 바꾸는 방식으로 승격하지 않는다. 정식 `colmap_tools` 식별자와 Root Shell을 유지하고, 그 안의 Align 구현을 실험에서 검증한 구현으로 갱신한다. Modifier와 Projection Painting의 데이터·작업공간·등록 경계를 유지한다.

## 2. 확인한 Git 상태

| 항목 | 확인 결과 |
|---|---|
| 정식 작업트리 | `E:\DEV\DEV_RND\Colmap_blender_Tools` |
| 정식 브랜치 | `main`, `16695ae25191eb93730e1bc87721503c3bd82d83` |
| 실험 작업트리 | `E:\DEV\DEV_RND\colmap_align_test_env\source` |
| 실험 브랜치 | `codex/align-test-env`, `b7c5f1ecc3a338ef4ddfec6d5104b0de6dd0fa11` |
| 분기 이후 정식 변경 | 없음. `git merge-base --is-ancestor main HEAD` 성공 |
| 커밋된 변경 | 128개 파일, 28,175줄 추가 / 848줄 삭제 |
| 정식 작업트리 미커밋 변경 | 없음 |
| 실험 미커밋 변경 | 재연결 관련 `registration.py`, `test_env/manage.py`, `docs/TESTING.md`, 신규 `test_env/verify_session_reconnect.py` |

표의 미커밋 목록은 이 검토 문서를 작성하기 전 기준이다. 원격 서버를 새로 조회하지 않았으므로 병합 직전에는 원격 main의 최신 상태를 다시 확인해야 한다.

로컬 이력상 실험은 main의 후손이므로 커밋 전체의 단순 병합은 충돌 해결 없이 가능하다. 다만 실험 전용 구성까지 모두 포함하게 되어 현재의 선별 통합 방침과 다르다. Git의 빠른 전진 병합은 내용 검증을 대신하지 않는다. [Git 공식 병합 옵션](https://git-scm.com/docs/merge-options)

실험 작업트리는 선택적 체크아웃을 사용한다. 실험 디스크에 없는 Shell·다른 도구 경로를 삭제된 것으로 판단하면 안 된다. Git 트리에서 `shell`, `shared`, `tools/modifier`, `tools/projection_paint`, Root `__init__.py`, `blender_manifest.toml`은 main 대비 변경이 없다.

## 3. 승격 전에 해결할 사항

### 3.1 정식 ZIP에 CC 실행 환경이 포함되지 않는다 — 필수 수정

근거:

- 정식 `build_tools/build_extension.ps1:6`은 `SmallGicp`와 `Full` 프로필만 허용하며 기본은 `SmallGicp`다.
- 같은 파일의 `Set-StagedBuildProfile`은 `SMALL_GICP` 또는 `FULL`만 기록한다.
- `Copy-ExtensionSource`는 복사한 기존 `tools/align/runtime`을 제거한다. 이후 `Add-SmallGicpEmbeddedRuntime`은 `runtime/python`만 채운다.
- 실험 `tools/align/distribution_profile.py`는 기본 정렬 엔진을 `CCCORELIB`로 반환한다. 정식 프로필에 해당하는 선택 목록도 CC만 노출한다.
- 실험 `tools/align/alignment/icp_cccore_runtime.py`는 `runtime/cccore/colmap_cccore_worker.exe`를 요구한다. 없으면 환경 검사 또는 작업 시작이 실패한다.

따라서 현재 기능 커밋만 옮겨 기존 정식 빌드를 실행하면, 기본 엔진과 포함된 실행 환경이 어긋난다. 기존 설치 검사는 Small GICP 실행만 검사하므로 CC 누락을 올바르게 검증하지 못한다.

필요 작업:

1. 정식 CC 배포 프로필과 패키지 필수 파일 계약을 추가한다.
2. 전용 CPython·NumPy 조정기와 CC 워커·CCCoreLib·oneTBB·C++ 런타임을 검증된 스테이징에 포함한다.
3. 현재 실험에서 포함하는 라이선스·변경 표시·재빌드 원문 ZIP·잠금 파일 및 실행 환경 해시 검증을 정식 빌드로 옮긴다.
4. `prepare_cccore_runtime.ps1`의 기본 경로는 저장소 부모를 실험 루트로 가정하므로, 정식 빌드는 저장소 내부의 명시적인 생성물 경로를 전달하거나 정식 경로 계약을 추가한다.
5. 정식 아카이브 검사·설치본 검사를 실제 CC 프로필과 최소 정렬 실행 기준으로 갱신한다. 이전 엔진의 호환 코드 정리는 필요한 배포 범위로 한정한다.

### 3.2 정식 환경설정에서 CC 설정이 누락된다 — 필수 수정

실험 `test_env/addon/__init__.py:55`는 `COLMAPALIGN_AP_preferences`를 별도 등록한다. 이 클래스에는 `cc_cpu_thread_limit`, 감지/선택 스레드 표시, CC 환경 검사 버튼이 있다.

정식 `shell/lifecycle.py`는 `shell/preferences.py`의 `COLMAPTOOLS_AP_preferences`를 등록하며, 현재 속성은 `open3d_python_path`뿐이다. Align 자체의 `properties.CLASSES`에는 실험 환경설정 클래스가 포함되지 않는다.

이 상태로 승격하면 정렬 코드가 정식 Root 설정을 찾더라도 스레드 속성이 없어 자동값으로 해석한다. 즉, 이 누락만으로 정렬이 중단되지는 않지만 **수동 스레드 설정과 CC 환경 검사 UI가 사라진다**.

Root 환경설정에 CC 설정과 검사 UI를 연결하고, 정식 애드온에 하나의 Preferences만 등록한다. 같은 식별자로 실험 Preferences를 추가 등록하지 않는다. 스레드 값 변경·저장·재실행·다음 작업 반영을 정식 설치본에서 확인한다.

### 3.3 최신 소스·ZIP·검증 보고서가 동일한 상태가 아니다 — 배포 검증 필요

확인한 산출물:

| 산출물 | 확인 결과 |
|---|---|
| 기존 `outputs/verification-report.json` | 2026-09-05 03:40, 37개 검사 묶음 통과 |
| 그 보고서의 소스 지문 | `1d99d68b18a1ab40bd4cc583f5af1a235a569a6b0a9a7deedd76109bcf367aed` |
| 최신 `outputs/build-report.json` | 2026-09-05 09:44, 소스 지문 `7663d653ffc4a842f1423cefe322e4126b914e4bb3c7e40dfbad495973a131eb` |
| 최신 빌드에 포함된 `registration.py` | 현재 소스 파일 SHA-256과 일치. 재연결 수정 포함 |
| `outputs/session-reconnect-result.json` | 소스 GUI에서 ACTIVE, 수동 작업공간 전환 0회, 재연결 약 0.221초 |

37개 통과 보고서는 최신 ZIP의 통과 증거로 재사용할 수 없다. 최신 자동 재연결 수정은 아직 커밋되지 않았고, 소스 GUI 재열기 검증은 빈 Align 작업공간을 같은 프로세스에서 저장·재열기하는 범위다.

재연결 변경을 확정한 뒤 최종 소스로 정식 ZIP을 빌드하고 그 ZIP을 설치해 검증한다. 실제 레이어·내장 OBJ·마커가 있는 파일의 새 Blender 프로세스 시작, 일반 작업공간에서 여는 경우, 다른 도구와의 세션 교대도 확인한다.

### 3.4 기존 main으로의 저장 파일 역호환은 없다 — 복귀 절차 필요

- 기존 main `tools/align/constants.py:27`: 저장 스키마 3, 읽기 지원 1~3.
- 실험 같은 파일: 저장 스키마 8, 읽기 지원 1~8.
- 기존 main의 프로젝트 codec은 지원하지 않는 스키마를 거부한다.

새 구현은 이전 형식 읽기를 지원하도록 작성되어 있다. 그러나 새 구현에서 저장한 스키마 8 파일의 Align 세션을 이전 main 애드온으로 복원할 수는 없다. 이는 `.blend` 전체 파일이 손상된다는 뜻이 아니라 Align의 저장 상태가 구버전 복원 대상이 아니라는 뜻이다.

사용자는 현재 파일이 테스트용이므로 보존할 필요가 없다고 결정했다. 따라서 해당 파일의 백업·변환·복구는 필수 단계에서 제외한다. 복귀 기준은 이전 코드와 설치 ZIP이며, Git 복귀가 저장 형식까지 되돌린다는 의미는 아니다. 새 정식판의 읽기·정렬·저장·재열기 기능은 검증용 파일로 확인한다.

### 3.5 기존 성능 채택 기준에 미완료 항목이 있다 — 근거 확정 필요

`outputs/untitled_session_v3_final_20260905_run05/report.md`에 기록된 같은 실데이터 결과:

| 조건 | v2 평균 | v3 평균 | 결과 |
|---|---:|---:|---|
| 기존 정렬 상태, 자동 31스레드 | 90.444초 | 5.997초 | 약 15.08배 |
| 같은 오정렬 상태, 자동 31스레드 | 502.150초 | 106.033초 | 약 4.74배 |
| v3 기존 정렬 상태, 단일→자동 | 10.243초 | 5.997초 | 약 1.71배 |

v2/v3 최종 행렬의 최대 차이는 0이고, 오정렬 입력의 절사 RMS 차이는 약 `3.775e-09`다. 단일 네이티브 프로세스와 세션 메시 1회 적재도 기록된 검증을 통과했다. 이는 이 데이터와 조건에 대한 근거이며 RC 재구성의 모든 국소 오차 해결을 의미하지 않는다.

그러나 보고서의 최종 판정은 `EXPERIMENT_ONLY`다. 프로토콜 오버헤드 최대 5.0927%가 기존 1% 기준에 미달했고, 페이징은 측정되지 않았다. 이는 수치 오류 발견과 구분한다. 정식 승격 전에는 페이징을 계측하고, 기존 오버헤드 기준을 충족시키거나 전체 시간과 절대 비용을 근거로 채택 기준 변경을 명시적으로 결정해야 한다. 기존 실패 판정을 통과로 바꿔 적지 않는다.

등록 내부 가속 구조 재사용 패치는 성능 향상의 필수 조건으로 새로 추가하지 않는다. 채택하지 않은 실험을 이번 승격 범위로 다시 끌어오지 않는다.

## 4. 통합 범위와 방법

기존 `test_env/README.md`는 환경 구성 커밋을 제외하고 기능 커밋을 선별 통합하도록 정하고 있다. 실제로는 `9850188`과 `b7c5f1e`에 기능과 `test_env` 변경이 함께 들어 있다. 따라서 커밋 제목만 보고 두 커밋 전체를 그대로 cherry-pick하는 방식은 기존 분리 방침을 충족하지 않는다.

권장 순서:

1. 실험의 재연결 변경을 검토·검증한 뒤 커밋하여 승격 기준을 고정한다. 기존 실험 이력을 재작성하지 않는다.
2. 최신 main에서 `codex/align-main-integration` 통합 브랜치와 전체 소스가 있는 별도 작업트리를 만든다. 정식 작업트리와 실험 실행 환경을 보존한다.
3. 고정한 실험 버전에서 `tools/align`, 관련 테스트, CC 네이티브 빌드·문서를 선별해 통합한다. 선택한 경로와 원본 커밋을 기록해 누락 여부를 대조한다. 실험 관리기에만 있는 필수 검사도 정식 테스트로 옮긴다.
4. 정식 Root 환경설정과 CC 패키징·설치 검사를 연결한다. 실험 애드온 래퍼와 `environment.json`, 실험 사용자 설정·캐시·프로젝트·결과물은 정식 배포 대상으로 가져오지 않는다.
5. 아래 정식 설치본 검증을 통과시키고 성능 채택 근거를 확정한다.
6. Root 버전과 Manifest 버전을 함께 올리고, 저장 형식과 엔진 변경을 릴리스 문서 및 ADR에 기록한다.
7. 검증한 통합 브랜치를 main에 반영한다. 실험 환경은 사용자 정리 요청 전까지 보존한다.

통합 커밋은 기능 이관 / 정식 배포·설정 연결 / 최종 검증·릴리스 기록 정도로 구분한다. 각 단계의 기준 소스와 산출물 해시를 남기되, 아직 정식 엔진이 실행되지 않는 중간 단계를 배포하지 않는다.

## 5. 정식 설치본의 완료 기준

- 정식 `colmap_tools` 이름으로 설치·등록·재등록·해제와 실패 시 등록 자원 정리가 정상이다.
- 실험 루트 경로나 시스템 Python에 의존하지 않고 패키지 내부 CC 실행 환경만으로 최소 정렬을 완료한다. 실제 네이티브/솔버 계약 및 배포 파일 해시가 일치한다.
- 점군–점군, 점군–메시, 메시–점군, 메시–메시가 기존 A/P 선택과 축 설정대로 실행된다. 마커 정렬, ±10% 배율 보정, 미리보기·승인·버리기·Undo/Redo·취소를 확인한다.
- 검증용 파일에서 COLMAP/OBJ 위치, 내장 OBJ 토폴로지와 UV, 마커, 확정 변환, 저장·재열기·내보내기를 확인한다. 현재 사용자 테스트 파일의 보존은 요구하지 않는다.
- 실제 데이터가 있는 Align 작업공간으로 새 Blender를 시작했을 때 수동 탭 전환 없이 세션이 연결된다.
- 기존 Architecture·Coexistence·Modifier/Projection Core·통합 등록 검사와 설치본 작업공간 격리·Camera Marker 교대 검사를 통과한다. 실험 검증을 위해 정식 회귀 검사를 생략하지 않는다.
- 정식 CPU 설정이 저장되고 다음 작업의 요청/실제 스레드 수에 반영된다.
- 최신 ZIP 해시와 그 설치본 검증 보고서가 일치하고, 미측정/미달 성능 항목의 처리 결정이 기록된다.

이 검토는 변경 전체의 행 단위 코드 감사나 새 정식 설치본의 실행 검증 완료를 의미하지 않는다. 현재까지 확인한 통합 경계의 문제와 실제 산출물을 바탕으로 다음 작업 범위를 확정한 것이다.

## 6. 주요 근거 위치

- `docs/architecture/PRODUCT_BOUNDARIES.md`, `docs/architecture/PACKAGE_STRUCTURE.md`
- `docs/decisions/0006-small-gicp-runtime.md`, `docs/decisions/0010-cccore-align-test-runtime.md`
- `test_env/README.md`, `test_env/addon/__init__.py`, `test_env/manage.py`
- 정식 `build_tools/build_extension.ps1`, `build_tools/test_installed_extension.ps1`, `shell/preferences.py`, `shell/lifecycle.py`
- 정식 `tests/architecture/test_repository_structure.py`, `tests/blender_background/test_installed_extension.py`
- 실험 `tools/align/distribution_profile.py`, `properties.py`, `constants.py`, `registration.py`, `alignment/icp_cccore_runtime.py`
- `E:\DEV\DEV_RND\colmap_align_test_env\outputs\build-report.json`
- `E:\DEV\DEV_RND\colmap_align_test_env\outputs\verification-report.json`
- `E:\DEV\DEV_RND\colmap_align_test_env\outputs\session-reconnect-result.json`
- `E:\DEV\DEV_RND\colmap_align_test_env\outputs\untitled_session_v3_final_20260905_run05\report.md`
