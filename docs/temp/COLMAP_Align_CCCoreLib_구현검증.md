# CCCoreLib 엔진 교체 구현·검증 기록

- 일자: 2026-09-03
- 상태: 실험 구현·설치·검증 완료
- 브랜치: `codex/align-test-env`
- 대상: `E:\DEV\DEV_RND\colmap_align_test_env\source`
- 관련: [계획서](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/COLMAP_Align_CCCoreLib_%EC%97%94%EC%A7%84%EA%B5%90%EC%B2%B4_%EA%B3%84%ED%9A%8D%EC%84%9C.md), [배포 결정](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/decisions/0010-cccore-align-test-runtime.md)

## 반영된 동작

레이어의 점군·표면 아이콘이 CC 정밀 정렬의 실제 입력을 선택한다. A 전체의 형상 종류는 같아야 하며 숨김 A도 포함한다. P는 하나다. P 점군/A 메시, P 메시/A 점군의 역방향 처리까지 자동 선택한다. 연결 표면은 면적 표본과 기준 삼각면의 최근접점을 사용한다.

3점 초기 정렬과 현재 위치에서 직접 Align을 유지한다. CC 강체 정렬 뒤 시작 C 대비 ±10%의 균일 스케일 보정을 수행한다. 미리보기·버리기·승인·A 편입·마커 정리·Undo/Redo는 기존 경계를 사용한다. 정렬 결과는 모델 공통 C에 적용되어 COLMAP 점·카메라·연결 메시가 함께 이동한다.

설치 패키지는 CC 전용 Python 조정자와 실행 파일·공유 DLL을 포함한다. Small GICP 전용 solver/worker, Python 패키지와 소유 DLL은 제외한다. 이전 엔진은 개발 소스와 비교용 런타임에 보존하며 자동 대체 실행하지 않는다. main과 일반 Blender 사용자 설정은 수정하지 않는다.

## 구현 위치

- `tools/align/alignment/icp_geometry.py`: 선택 형상·연결 메시·독립 축·복수 A·입력 지문.
- `tools/align/alignment/icp_cccore_contract.py`: 외부 파일 계약과 원자 입출력.
- `tools/align/alignment/icp_cccore_solver.py`: 정규화, 네 조합, 강체/배율 보정, 독립 거리·겹침 평가.
- `tools/align/alignment/icp_cccore_worker.py`: CPython 조정자 진입점.
- `tools/align/alignment/icp_cccore_runtime.py`: 기존 미리보기·승인·취소 연결.
- `tools/align/native/cccore_worker`: CC 공유 라이브러리를 호출하는 실행 파일 원문·고정 의존성·패치.
- `build_tools/prepare_cccore_runtime.ps1`: 검증된 코어 준비와 재빌드.
- `tools/align/project_persistence/codec.py`, `alignment/types.py`: 스키마 7 출처와 이전 스키마 복원.
- `test_env`: 실험 패키징·설치·실제 설치본 네 조합 및 GUI 검증.

기능 소스는 기존 경로를 유지한다. 통합 시 정렬 기능과 테스트, CC 런타임 준비·라이선스 자산을 함께 반영하고 통합 애드온 빌드 스크립트에 CC 패키징을 연결해야 한다. 실험 사용자 설정과 `test_env` 실행기 전체를 main에 그대로 반영하는 방식은 사용하지 않는다.

## 확인하여 수정한 문제

1. 코어 원본은 배율 한도를 적용한 반환 행렬과 내부 평가 RMS가 달랐다. 실제 점군을 움직이기 전에 누적 배율을 제한하고 이동을 다시 계산하는 최소 패치를 적용했다. 정·역방향의 경계 밖 정답으로 검증했다.
2. Windows 진행 파일의 읽기·원자 교체가 충돌했다. 읽기 공유 플래그와 확인된 일시적 오류의 제한 재시도로 처리한다. 장기 점유는 명시적 오류이며 완성 전 파일을 공개하지 않는다. [Windows 파일 공유 규약](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
3. 원본 형상의 전체 지문을 매 진행 확인마다 계산하던 경로를 분리했다. 진행 중에는 리비전·출처·축·C를 확인하고 완료와 승인 시 원본 내용 전체를 검증한다. 세 경계를 실제 작업으로 확인했다.
4. 승인 후 형상 출처와 전후 평가가 사라지지 않도록 스키마 7 감사 정보에 보존한다. 코어·실행 파일·DLL·패치 해시, 입력 형상별 출처·축·초기 C, 독립 거리·겹침 기준·검토 사유가 저장된다.
5. 삼각면 거리 평가가 분할 단계 0으로 실행되어 전체 면을 과도하게 검사하던 병목을 확인했다. 코어의 `findBestLevelForComparisonWithOctree`로 수준을 결정하고 데이터 옥트리를 재사용한다. 64,800면·30,000점의 해석적 정답 거리 검사가 2.287초에 통과했다.

## 검증 근거

`outputs`는 실험 루트 `E:\DEV\DEV_RND\colmap_align_test_env` 아래를 뜻한다.

- 네이티브 코어: 16개 검사 통과, `runtime/cccore/native_test_report.json`.
- 제공 원문만 사용하는 오프라인 재빌드: `runtime/cccore/offline_rebuild_report.json`.
- CPython 조정자: 네 조합, 큰 좌표, 정·역방향 ±10%, 대응 부족, 취소·강제 종료의 자식 정리, Windows 공유 잠금 20개 검사. 최종 코어 재실행 20개 통과(31.862초), `outputs/cccore-solver-verification.json`.
- 설치본: `outputs/installed-cccore-alignment.json`. 설치 폴더의 Python·작업자 실제 실행 인자를 확인하고 네 조합의 비항등 C·독립 축·A 불변·승인·Undo/Redo를 검사한다.
- 소스 회귀: `test_env/manage.py`의 34개 검사 파일. 연결 메시·마커·축·COLMAP 입출력·저장 스키마 1~7 포함.
- GUI: 실제 A/P 작업공간, Industry Compatible 키맵, 레이어별 표시 전환, 축 창, 연결 메시 마커 클릭, 3점 정렬, 저장 출처.
- 최종 배포: `outputs/build-report.json`, `outputs/verification-report.json`.

## 실데이터 기록

기존 `cloudcompare_surface_20260903_run01/before.blend`의 동일 확정 C를 초기값으로 사용한다. Small GICP 희소 점군 입력과 CC 연결 표면 입력을 각각 실행하고, 최적화와 다른 난수 시드의 면적 표본으로 양방향 삼각면 거리를 평가한다. 따라서 엔진과 입력 형상을 함께 바꾼 실제 사용 흐름의 비교다.

결과는 `outputs/cccore_engine_20260903_run01`에 따로 저장한다. 원본과 이전 비교 결과는 덮어쓰지 않는다. 실제 COLMAP 내보내기·재로드의 점/카메라 투영 관계와 연결 메시 공통 변환을 확인한다. 실행 시간은 다른 검증 프로세스와 겹치지 않는 측정을 우선 기록한다.

RC 프로그램 자체의 재구성은 실행하지 않는다. 이번 수치 검증은 내보내는 COLMAP·카메라·메시의 변환 일관성까지이며, RC가 내부적으로 카메라를 다시 최적화한 결과는 별도 실측이다.


## 실데이터 최종 결과

같은 시작 C, 같은 독립 평가 표본에서 측정했다. 거리는 원본 프로젝트 단위다.

| 항목 | 시작 위치 | Small GICP 희소 점군 | CCCoreLib 연결 표면 |
| --- | ---: | ---: | ---: |
| 대칭 90% 절사 RMS | 0.03929969 | 0.03929970 | 0.03562594 |
| 시작 대비 개선 | — | -0.0000% | 9.3480% |
| 추가 균일 배율 | 1.0 | 1.000000000 | 1.003553043 |
| 시작 API부터 미리보기까지 | — | 16.06초 | 56.20초 |
| 엔진 판정 | — | REVIEW | PASS |
| 관측 작업 프로세스 메모리 합계 최고값 | — | 684.13 MiB | 86.20 MiB |

메모리는 Windows 작업 집합을 0.1초마다 관측한 값이다. Blender 전체 메모리가 아니며 짧게 존재한 자식이나 표본 사이 최고값을 놓칠 수 있다. 시간은 해당 데이터·환경의 단회 측정이다. 두 엔진의 입력 형상과 내부 정책은 다르며 Small GICP의 기본 스케일 한도는 ±5%, CC는 ±10%다. 자세한 조건은 `measurement_context.json`에 기록했다.

Small GICP는 초기 RMSE 개선이 없어 검토 상태였으며 상호 대응 비율 약 0.145로 스케일 보정이 생략됐다. CC 표면 평가는 공간 분할 개선 전후 반환 D와 독립 평가가 동일했다. 이전 264.27초 실행은 기능 확인 중 다른 검증 부하가 일부 겹쳤으므로 최종 56.20초와의 차이를 순수한 알고리즘 속도 개선율로 해석하지 않는다.

### 저장본과 실제 내보내기 왕복

최종 CC 결과를 별도 Blender 프로세스에서 재열고, 각 모델의 COLMAP 5개 파일과 연결 OBJ를 실제 내보내기·재로드했다.

- COLMAP 점 709,048개 전체의 재로드 좌표와 593개 카메라 자세를 확인했다. 카메라별 최대 256점씩 총 151,808개 투영 쌍을 검사했다.
- 점 좌표 재로드 최대 오차: 0.
- 카메라 좌표 관계 최대 오차: `8.53e-14`.
- 정규화 영상 투영 최대 오차: `4.18e-10`.
- 연결 메시의 공통 D 적용 최대 오차: `1.07e-14`.
- 입력 보존본 지문 유지, 원본 및 이전 결과 덮어쓰기 없음.

결과 파일은 실험 루트 기준 다음과 같다.

- `outputs/cccore_engine_20260903_run01/comparison.json`: 입력 지문·모든 시도·전후 평가·시간·메모리.
- `outputs/cccore_engine_20260903_run01/before.blend`: 정렬 전 사본.
- `outputs/cccore_engine_20260903_run01/cccore_attempt03/after.blend`: 최종 CC 승인 결과.
- `outputs/cccore_engine_20260903_run01/cccore_attempt03/reopened_and_export_verified.json`: 별도 재열기·실제 내보내기 왕복 증거.
- `outputs/cccore_engine_20260903_run01/cccore_attempt03/colmap_roundtrip`: 검증에 사용한 COLMAP·OBJ 내보내기 사본.

최종 코어 작업자 SHA-256은 `58474b390ae3e96ed100734114d8abe9d5bdf33b4d64a82d26cfdfe1def551e2`다. 코어 고정 커밋과 `bounded-scale-v1` 패치는 유지하며, 삼각면 거리의 공간 분할 선택은 작업자에 반영했다.


## 최종 설치 확인

- 실험 패키지: `dist/colmap_align_test_env-0.1.0.zip`.
- 패키지 SHA-256: `48b5cf11d41883e2455028f3759f60962a93a6456e5ea6f069116180a89cdd6f`.
- 소스 지문: `c038db55d796b2fe07efb60b1c5cb342c2c5cb9f51adff40c04b81d06dd2f183`.
- `verification-report.json`: 36개 검증 항목 모두 통과(설치본 네 조합·등록 검사 1항목, 소스 회귀 파일 34개, 실제 GUI 1항목).
- 설치된 CC 실행 파일은 최종 실데이터 검증에 사용한 작업자 SHA-256과 일치한다.
- 현재 소스·빌드 보고서·설치 기록·검증 보고서의 지문 일치를 확인했다.
- `main` 작업트리는 변경하지 않았으며 Git 커밋·통합을 수행하지 않았다.

기존 PowerShell 실행기를 사용하면 현재 설치본으로 실험 Blender를 열 수 있다.

```powershell
& 'E:\DEV\DEV_RND\colmap_align_test_env\Start-Test-Env.ps1'
```

## CPU 멀티스레드 v2 추가 검증

2026-09-04에 oneTBB 2022.0.0을 포함한 `cccore-native-v2` 런타임과 Add-on Preferences의 자동·수동 CPU 상한을 구현했다. 최종 런타임은 Manifest 19개와 실제 파일 19개가 일치하고, 네이티브 통합 9개·워커 20개·Blender UI 9개·Blender 런타임 6개 등 총 44개 검사를 실패·오류·건너뜀 없이 통과했다.

`C:\Users\gksru\Desktop\Untitled.blend`의 동일 v2 바이너리에서 1스레드는 96.0219초, 자동 31스레드는 90.4438초였다. `1.0617배` 속도와 5.81% 시간 단축이며 결과 행렬·RMS·품질은 완전히 같았다. 동시 작업 집합 피크는 4.8281MiB 늘었다. 계획한 1.25배 기준에는 못 미쳐 main 채택은 보류한다. 상세 결과와 병목 분석은 [CPU 멀티스레드 계획서](COLMAP_Align_CCCoreLib_CPU_멀티스레드_적용_계획서.md)와 `outputs/untitled_thread_benchmark_20260904_run01/comparison.json`에 있다.

최종 v2 워커 SHA-256은 `03d489eafcb397639dc2730368d254cc39f12a68808e60568f9da0d993198b7e`다. 이 절보다 앞선 v1 설치 해시와 성능 수치는 당시 배포 기록으로 보존한다.

## 단일 세션 워커 v3 추가 검증

2026-09-05에 Align 작업 하나가 `colmap_cccore_worker.exe` 프로세스 하나만 사용하는 `cccore-native-v3` 파일 세션을 구현했다. Python은 표본 생성, 후보 행렬 조합, 독립 평가와 최종 후보 선택을 계속 담당한다. 점군–점군 6개, 점군–메시와 메시–점군 각 5개, 메시–메시 8개 논리 연산의 순서와 기존 품질 정책을 유지한다.

세션은 시작 요청·준비 완료, 단조 증가 명령 요청·응답·진행률, 취소와 닫기 파일을 원자적으로 교환한다. 세션·작업·입력 지문·명령 순번·경로를 매 단계 확인하며 부분 JSON, 중복·누락·역순, 경로 이탈과 같은 geometry ID의 설명 변경을 오류로 종료한다. 기본 A/P PointCloud·SimpleMesh는 `SESSION` 범위로 유지하고 평가·등록 변형 PointCloud는 `COMMAND` 범위에서 성공 응답 기록 직후 해제한다. 실패·취소는 세션 종료와 레지스트리 소멸로 회수한다.

역방향 표면 평가는 기준 표본에 합성 후보의 역행렬을 적용해 원본 이동 메시를 조회하고, CCCoreLib 거리 산출 뒤 후보의 양의 균일 배율을 복원한다. 기존 float64 변환 후 float32 적재 순서와 양방향 원시 거리·후보·품질 동등성을 확인해 후보별 전체 변형 메시를 제거했다. 정확 후보 캐시는 원시 float64 표본·행렬·평가 계약의 bit-exact 키만 허용한다. 항등 메시–메시 작업은 논리 8개, 물리 4개, 적중 4개였고 비트가 다른 후보를 만드는 네 형상 작업은 적중 0이었다.

공개 API에서 점군과 모델 옥트리를 같은 결합 정육면체 경계로 미리 만든 후보는 거리 바이트가 control과 같았지만 5회 평균 `0.99157244초`로 control `0.92452442초`보다 7.25% 느렸다. 검증된 control 경로로 복귀했으며, 정확 후보 캐시 뒤 완전한 키가 반복되는 물리 명령도 없어 GridAndMeshIntersection 장기 캐시는 채택하지 않았다.

초기 358초 실데이터 실행의 약 4.5배 회귀는 Python progress 폴링이 아니라 `Progress::isCancelRequested()`가 모든 CCCore 콜백에서 취소 경로를 조회한 것이 원인이었다. 동일 입력 A/B에서 progress 미조회 `17.141초`, 10ms 조회 `17.500초`, 변경 감지 50ms 조회 `17.078초`로 폴링 영향이 없음을 확인했다. 원자 latch, `GetTickCount64()` 25ms 제한과 CAS 단일 조회자로 수정하고 terminal 기록 전후에는 강제 조회한다. 파일·경로 없음 이외의 오류는 실패로 닫으며 진행 파일의 내구 원자 기록은 유지한다. Python 응답 대기기는 프로세스 종료 직후 검증된 세션 종료 파일을 재조회해, 종료 파일 공개와 `poll()` 사이의 경쟁에서도 취소를 환경 오류로 오분류하지 않는다.

최종 Release 설치본 검증 결과는 다음과 같다.

- 네이티브 워커: 35개 통과. 단일 PID, SESSION 적재·적중, COMMAND 해제와 만료 ID 거부, v2 단발 요청 수치 동등성, 2초 이내 취소, 병렬 race, terminal 경계 취소, 부분 JSON, 순번·식별자·경로 오류를 포함한다.
- Python 솔버: 실제 설치 워커를 사용한 41개 전부 통과(29.056초). 네 형상 모드의 6·5·5·8 순서, inverse query 수치, 정확 캐시 적중·미적중, 프로토콜 시간 계약, 충돌·취소와 부모 강제 종료 시 부분 결과·fallback 금지를 포함한다. response 취소 종료 관찰 경쟁의 실제 자식 취소 20회 반복, 결정적 start·ready 직후·response·close 경계, 실제 start·close 각 4회 반복과 정상 close 16회 반복도 통과했다.
- Blender: UI 9개, 실제 런타임·승인·Undo/Redo·손상 통계 거부 7개, 저장·축 의존성·v3 전체 감사 필드 encode→decode→restore와 `cccore-icp-v1/v2` 실제 snapshot 호환을 포함한 9개를 통과했다.
- 런타임 Manifest: 선언한 19개 파일과 실제 파일·해시가 일치하고 자체 검사의 DLL은 `CCCoreLib.dll`·`tbb12.dll` 상대 식별자로만 기록한다. 배포 ZIP과 중첩 재빌드 ZIP에서 현재 개발 루트·사용자 홈 경로가 없고 호스트 shebang이 든 불필요한 `f2py.exe`도 포함하지 않는다.
- 최종 v3 워커 SHA-256: `45aed37a17f23ca2ea8f1edd2aa32603fd3a286a734b6fdc201d7b1db55b63c9`.
- 재빌드 원문 ZIP SHA-256: `a76ccff15dd4a5edb1aadfdb74ed3ca47964ec162bbc420cfcbdac8a6747d1ac`.
- 런타임 Manifest SHA-256: `2982bc7ca6d019b85cf4c1735c402a4800ef99d77a01e6bd7c8c269666200386`.
- 최종 v3 설치 ZIP `dist/colmap_align_test_env-0.1.0.zip`은 `47,306,706바이트`, SHA-256 `8fd47686398f988792e608aa8152a5ea4500ba5dd220e3481b2dd864a736b68f`다.
- 설치 ZIP의 소스 지문은 `1d99d68b18a1ab40bd4cc583f5af1a235a569a6b0a9a7deedd76109bcf367aed`이며 빌드 보고서 SHA-256은 `7398e34916341e4a76e2837320994266e2ad567fc77452b3d5bd6194fa544b5e`, 검증 보고서 SHA-256은 `96b1d059f7cce48fa3534e0121e0c38b946420734d96381e5e687efdfee29629`다. 검증 보고서의 설치·회귀·실제 GUI 항목은 `37/37` 통과했다.

최종 설치본의 백그라운드 팩토리 시작(`--background --factory-startup`) 실데이터 측정은 `outputs/untitled_session_v3_final_20260905_run05/summary.json`과 `report.md`에 보존했다. aligned 자동 31스레드 평균은 `5.9969730초`로 v2 자동 `90.4438108초` 대비 `15.081577배`, 1스레드 평균 `10.2432664초` 대비 `1.708073배` 빨랐다. aligned v3 측정 3회 안에서 최종 행렬·RMS·후보·품질은 bit-identical이었으며, PID는 1개, 논리 8개·물리 4개·정확 캐시 적중 4개였다. 의도적 오정렬 자동 평균은 `106.0332545초`, 논리 8개·물리 8개·적중 0이며 등록 두 호출 합계 `100.3655825초`가 전체의 94.6548%였다.

같은 geometry 지문·파라미터·perturbation·시작 행렬의 v2 오정렬 측정 3회는 `502.2879059초`, `507.1089660초`, `497.0522273초`, 평균 `502.1496997초`였다. v3 평균은 `4.735776배` 빠르고 시간을 `78.8841%` 줄여 성능 기준을 통과했으며, Align당 PID도 v2 8개에서 v3 1개로 줄었다. 최종 행렬·delta 최대 차이는 0이고 품질 `PASS`와 최종 후보 `scale_refine`도 같았다. trimmed RMS 차이 `3.7746634e-9`는 기존 float 코어 허용오차 안이므로 수치 동등으로 판정하되 v2/v3 전체 통계가 bit-identical하다고 기록하지 않는다.

aligned 최대 작업 집합 `918,560,768바이트`는 같은 입력 v2보다 2.8513%, 오정렬 `918,458,368바이트`는 같은 입력 v2보다 3.4366% 높아 둘 다 +15% 기준을 통과했다. Paging counter는 수집하지 않아 Paging 없음은 미판정이다. 물리 명령별 host 단조 왕복 계측에서 전체 solver 시간 대비 프로토콜 오버헤드는 aligned 자동 3.3668~5.0927%, 1스레드 1.2896~2.2335%로 1% 기준을 실패했고, 오정렬 자동은 0.2675~0.2977%로 통과했다. 전체 12회의 최대값은 5.092686%다. 따라서 프로토콜 완료 기준 실패와 Paging 미검증을 함께 보존하고 전체 통합 판정은 `EXPERIMENT_ONLY`로 유지한다.

6단계는 등록 내부 가속 구조 재사용 성능 패치를 적용하지 않는 `NO_INTERNAL_PATCH`다. 오정렬 등록이 지배적이지만 공개 시간은 커널 내부 가속 구조 생성과 거리·반복 계산을 분리하지 못해 내부 구조만 병목이라는 근거가 없다. 추측성 등록 성능 패치는 적용하지 않았으며, 이미 채택한 `multithread-state-race-v1` 동시성 패치는 유지한다. run03은 7/12 완료 뒤 정상 close가 응답 파일→`session-result=closed`→exit 0 순으로 기록되는 사이 Python이 이전 응답 부재 상태와 exit 0을 함께 관찰해 실패했다. 종료 상태 검증 뒤 응답 경로를 재확인하도록 수정했고 정확 순서 단위 회귀와 실제 정상 close 16회를 통과했다. run04는 이전 `AB33A7B7...` 워커 측정으로 보존하며, 상대 Manifest·호스트 경로 제거 뒤 최종 `45AED37A...` 워커의 run05가 이를 대체한다. 원본 `C:\Users\gksru\Desktop\Untitled.blend`는 12회 전후 크기 `162,229,476바이트`, mtime UTC `2026-09-04T14:28:48.8775735Z`, SHA-256 `2B07982399DFFCD6F7DC45A74336D271B15D19C6E6A7405F18FD4C9D6A0A4ADC`로 같았다. 초기 358초 실행은 취소 경로 조회 수정 전 이상치이며 Python polling 원인 가설을 폐기한 진단 자료로만 보존한다.
