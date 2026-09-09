# Align CC 정식 승격 0.3.37

- 작성일: 2026-09-05
- 상태: 정식 승격 완료
- main 출발점: `16695ae25191eb93730e1bc87721503c3bd82d83`
- 실험 이관 기준: `e9082f74b7e0ec726f1c01323e642a111c673d06`
- 기능 이관 커밋: `0c4f5e9`
- 정식 실행 환경·프로필·CPU 설정 커밋: `352ee14`
- 설치·세션·성능 검증 이관 커밋: `9ecd157`
- 작업 브랜치: `codex/align-main-integration`
- 검증 결과 루트: `E:\DEV\DEV_RND\colmap_align_main_integration\outputs`

## 변경 내용

정식 `colmap_tools`의 기본 정렬 엔진을 CC v3로 전환했다. 실험 `test_env` 래퍼·실행기·사용자 설정을 제외하고 `tools/align`, 관련 검사·CC 빌드 자산·설계 문서를 선별해 이관했다. Modifier·Projection Painting과 공용 Shell 생명주기는 기존 main 동작을 유지한다.

- 레이어별 점군·표면 선택, 각 형상의 축 설정, 연결 OBJ·독립 OBJ 불러오기.
- A끼리/P끼리 형상 종류를 고정하는 마커, 기존 3점 정렬과 표면 피킹.
- 네 가지 CC 입력 조합, 강체 정렬 후 최대 ±10% 균일 배율 보정.
- 단일 네이티브 세션과 형상 재사용, Root CPU 환경설정, 전용 환경 검사.
- 미리보기·승인·버리기·Undo/Redo·취소 및 실패 후 재시작.
- OBJ 원본 면·UV 내장, 삼각형 프록시 정렬, 원본 토폴로지를 보존하는 내보내기.
- 스키마 8 저장과 내장 OBJ·마커·확정 행렬 복원, 파일 시작 시 Align 자동 재연결.

버전은 Root와 Extension Manifest 모두 `0.3.37`이다. 기본 ZIP은 CC 전용 CPython·NumPy·워커·DLL·라이선스·재빌드 원문을 포함한다. 별도 실험 환경 접근 없이 설치 패키지 내부 경로로 실행한다. 명시적 SmallGicp 비교 빌드와 선택형 소스를 포함하는 Full 빌드는 유지한다.

## 검증과 배포 식별자

정식 ZIP은 `build/colmap_tools-0.3.37.zip`, 크기는 **47,943,982바이트**, SHA-256은 `41905a6350b6ca4fe61698c57117be8815db73a6afa0f2055f2f77aac07ab32b`다. `outputs/installed-final/verification-report.json`에 같은 해시와 전체 GUI 검사 통과가 기록되어 있다. 실패한 이전 시도는 별도 출력 폴더에 보존하고 최종 통과 보고서와 혼합하지 않는다.

| 검사 | 결과 |
|---|---|
| Architecture / Coexistence | 12개 / 11개 통과 |
| Modifier Core / Projection Core | 216개 / 40개 통과 |
| 통합 등록·실패 정리·재등록·해제 | 통과 |
| Align Blender 소스 회귀 | 필수 35개 스크립트 모두 통과 |
| CC Python 솔버 | 41개 통과 |
| CC 네이티브 세션·오류·취소·수치 | 35개 통과 |
| 정식 설치본 실제 CC | 네 가지 형상 조합, 승인·Undo/Redo·원본 불변성 통과 |
| OBJ 왕복 | 원본 면, 실제 면 코너 UV·seam·텍스처 복사·변환 정점 통과 |
| 작업공간·Camera Marker 교대 GUI | 세 도구 공존·표시·카메라 격리 통과 |
| 새 Blender 프로세스 복원 GUI | Align 자동 연결, 일반 작업공간 유지, 내장 쿼드·UV·마커·행렬·CPU 설정 복원 통과 |

소스 검사는 `outputs/source-checks/`의 앞 6종과 `outputs/source-final/`의 뒤 29종을 합친다. 재개 지점 앞의 이미 통과한 검사를 중복 실행하지 않았다. 실험판의 “모든 프로필 기본 CC” 검사와 정식판의 “원본 OBJ UV 표 항목 수” 검사는 각각 명시적 구엔진 프로필과 Blender 면 코너 UV 계약에 맞춰 갱신했다. 비교 빌드에서도 Root 환경설정 하나만 사용하며, Full의 실제 CC와 SmallGicp의 실제 합성 연산·OBJ 왕복을 별도 설치 검사한다.

Full과 SmallGicp도 설치·실제 정렬·OBJ 왕복을 통과했다. 보고서는 각각 `outputs/installed-full-final/verification-report.json`, `outputs/installed-small/verification-report.json`이다. 비교 ZIP 해시는 Full `be88b01d33dc90ebea14a17c1aaec38f3b7b490eda8a86d2c54dca967690cbc3`, SmallGicp `801ff5973a589cfc1d503186eb09d34807df9ff125d7fdfa7fd56bedc5c36d95`다.

네이티브 계산 소스·패치는 승격 중 변경하지 않았다. 고정 워커 SHA-256은 `45aed37a17f23ca2ea8f1edd2aa32603fd3a286a734b6fdc201d7b1db55b63c9`이다. 재빌드 원문 ZIP SHA-256은 `a76ccff15dd4a5edb1aadfdb74ed3ca47964ec162bbc420cfcbdac8a6747d1ac`, Runtime Manifest SHA-256은 `2982bc7ca6d019b85cf4c1735c402a4800ef99d77a01e6bd7c8c269666200386`이다.

## 실데이터 성능·메모리

Blender 5.2.0 LTS, Windows 11, 논리 CPU 32개·요청/적용 31스레드에서 측정했다. RAM은 136,533,553,152바이트, 페이지 파일은 8,192MB이며 측정 후 사용량과 부팅 이후 최고 사용량 모두 0MB였다. 기준 메시 1,781,383정점·3,552,808삼각면, 이동 메시 78,130정점·155,674삼각면을 사용했다.

입력은 이전 run05의 읽기 전용 사본이며 SHA-256은 `2b07982399dffcd6f7dc45a74336d271b15d19c6e6a7405f18fd4c9d6a0a4adc`다. 기존 시작 행렬과 1.03배·Z 회전 0.01라디안·이동 `(0.03, -0.02, 0.01)`의 오정렬 조건, 샘플 50,000개·평가 30,000개·반복 상한 100회·overlap 0.9·배율 ±10%를 그대로 재현했다. 각 상태는 새 Blender 프로세스에서 워밍업 1회와 측정 3회를 수행했다. 복원 시간은 전체 정렬 시간과 분리했다.

| 입력 상태 | 측정 3회(초) | 평균(초) | 기존 v3 평균 대비 | 이전 v2 대비 |
|---|---|---:|---:|---:|
| 정렬됨 | 5.968883 / 6.234720 / 6.221816 | **6.141806** | +2.42% | 14.73배 빠름 |
| 오정렬 | 102.681652 / 102.462306 / 102.373823 | **102.505927** | −3.33% | 4.90배 빠름 |

6회 모두 기존 v3의 형상 지문·설정·최종 후보·품질 상태·행렬·delta와 일치했다. 행렬 최대 차이 및 RMS·평균 RMS·가까운 90% RMS·중앙값·95백분위 거리 차이는 0이다. 이미 정렬된 입력의 `REVIEW`, 오정렬 입력의 `PASS`도 그대로 유지한다. 관측 시간의 작은 증감을 엔진 개선이나 회귀로 단정하지 않는다. 계산 코드·바이너리가 동일하고 수치·작업 경계 차이가 없어 추가 대조 실행은 수행하지 않았다.

모두 네이티브 프로세스 1개, A/P 메시 적재 각 1회다. 정렬 상태는 논리 8회·물리 4회·정확 캐시 4회, 오정렬 상태는 논리 8회·물리 8회·정확 캐시 0회다.

작업집합 표본 합계 최고값은 정렬 상태 919,678,976바이트, 오정렬 상태 749,187,072바이트다. 이번 0.1초 표본은 이전 0.05초보다 짧은 피크를 놓칠 수 있으므로 오정렬 메모리가 줄었다고 해석하지 않는다. 같은 Windows API의 프로세스별 생애 최고 작업집합 합계는 1,137,438,720 / 1,188,544,512바이트로 기존 대비 +0.13% / +0.33%다. 이 합계는 동시 최고 사용량이 아니며, 짧은 피크를 포함하는 별도 비교 지표로만 사용한다. 프로세스별 private·peak commit과 Blender 메모리는 원본 보고서에 별도로 보존했다. 관측 증가량은 기존 +15% 제한 이내다.

Windows PDH는 정렬 전 3초 기준 구간과 정렬 중 약 1초 구간별로 수집했다. 기준 CPU 부하는 정렬 0~2.99%, 오정렬 0~4.09%였다. 다른 Blender 검사·빌드는 측정 종료 뒤 순차 실행했다. 페이지 쓰기·Pages Output·페이지 파일 점유의 최대값은 모두 0이고 최소 가용 RAM은 107,709,624,320바이트였다. 디스크 페이지 읽기는 최대 약 60.64회/초로 실제 존재했다. 이 읽기는 DLL·실행 파일·매핑 파일의 하드 폴트도 포함하므로 특정 워커의 스왑 읽기량으로 해석하지 않는다. 이 환경에서 스왑 악화 증거가 없다는 결론이며 모든 컴퓨터·더 큰 입력에 일반화하지 않는다. [Microsoft 카운터 해석 안내](https://learn.microsoft.com/en-us/troubleshoot/windows-client/performance/how-to-determine-the-appropriate-page-file-size-for-64-bit-versions-of-windows)

프로토콜 왕복 부가 비용은 정렬 상태 0.151757~0.220671초(3.2926~4.8035%), 오정렬 상태 0.278556~0.402785초(0.2766~0.3997%)다. 기존 1% 목표는 짧은 입력에서 여전히 실패한다. [ADR-0011](https://github.com/hckyeong/Colmap_blender_Tools/blob/fd2d9cf57bb022505ea3b021a3bc2ee9f7f41df6/docs/decisions/0011-cccore-production-runtime.md)에 따라 비율은 최적화 목표로 유지하고 전체 시간·정확성·메모리·기능을 근거로 정식 채택했다. 이전 `EXPERIMENT_ONLY` 결과를 통과로 고쳐 쓰지 않는다.

원시 결과·페이징 표본·집계는 `outputs/benchmark-installed-01/`에 있다. 측정 ZIP 해시는 `ecc83f723ec6212631af58f7944c72bdd399e38f204a5f1aacb76be87069851e`다. 최종 ZIP과의 바이트 비교에서 바뀐 파일은 사용자 안내 `README.md`, 프로필별 설명·비교판 입력 제한을 연결한 `operators/marker_alignment.py`, `ui/panels.py` 세 개뿐이다. 정렬 계산·복원·입출력·Python·네이티브 바이너리는 동일하며, 최종 ZIP의 변경된 UI와 설치·저장 경로는 전체 격리 GUI 검사로 검증했다. 연결 근거는 `final-package-comparison.json`이다.

## 적용과 복귀

이 기록을 포함한 검증된 통합 브랜치를 main에 반영하고 `build/colmap_tools-0.3.37.zip`을 정식 산출물로 제공한다. 최종 Git 커밋·ZIP·런타임·설치 기록의 연결 정보는 main의 Git 제외 산출물 `build/align-main-promotion.json`에 기록한다. 이전 `0.3.36` 산출물과 실험 환경·브랜치는 유지한다. 원격 push와 실험 폴더 삭제는 수행하지 않는다. 사용자 Blender 설정·단축키를 실험 환경의 값으로 덮어쓰지 않는다.

사용자 결정에 따라 기존 테스트 `.blend`의 백업·일괄 변환·구버전 내보내기는 수행하지 않는다. 원본 파일을 자동 삭제하지 않으며, 새 정식판 저장·재열기는 독립 검증 파일로 확인한다.
