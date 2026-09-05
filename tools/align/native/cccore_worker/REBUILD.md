# 재빌드와 변경 원문

2026-09-04 기준 Windows x64, Visual Studio 2022 C++ 도구와 해당 설치본의 CMake로 빌드했습니다. 준비 스크립트가 고정 oneTBB `2022.0.0` 정확한 버전을 검증해 사용하므로 별도 Qt·CGAL 설치는 필요하지 않습니다.

## 고정 원문

- [CCCoreLib 원본 커밋](https://github.com/CloudCompare/CCCoreLib/tree/dc8c7d80f4ef8fd6a2c302c8d5f9c35099ba8432): `dc8c7d80f4ef8fd6a2c302c8d5f9c35099ba8432`, LGPL-2.0-or-later
- [nanoflann 원본 커밋](https://github.com/jlblancoc/nanoflann/tree/9a653cb243db6a09c94f833b28732b62f033e2b5): `9a653cb243db6a09c94f833b28732b62f033e2b5`, BSD-2-Clause
- [nlohmann/json 원본 커밋](https://github.com/nlohmann/json/tree/9cca280a4d0ccf0c08f47a99aa71d1b0e52f8d03): 3.11.3, MIT
- [oneTBB 공식 릴리스](https://github.com/uxlfoundation/oneTBB/releases/tag/v2022.0.0): 2022.0.0, Apache-2.0

oneTBB Windows x64 ZIP의 SHA-256은 `660f3555de26bee8cd4daef1105d5d9e93e695b2fcfa9c57db3aa38c6afe5675`입니다. 배포하는 `tbb12.dll`과 `oneTBB-Apache-2.0.txt`도 잠금 파일의 개별 해시로 다시 검증합니다.

`dependencies.lock.json`에 다운로드 경로와 SHA-256을 고정했습니다. 코어는 공유 DLL로 빌드하며 같은 ABI로 재빌드한 DLL로 교체할 수 있습니다. 워커 소스는 프로젝트의 GPL-3.0-or-later이며 GUI `qCC` 코드는 포함하지 않습니다.

## 검증된 최소 코어 변경

원본 `RegistrationTools.cpp`는 제한하지 않은 단계 변환을 내부 점군에 적용한 뒤 다음 반복에서 누적 반환 배율만 제한했습니다. 따라서 제한 밖 정답에 도달한 내부 점군 RMS와 반환 행렬의 실제 RMS가 달랐습니다.

실제 비대칭 8점에 비영 원점, Z 회전 0.012라디안, 이동 `(0.015,-0.012,0.018)`을 적용해 재현한 값은 다음과 같습니다.

| 정답 배율 | 허용 범위 | 원본 반환 RMS | 반환 행렬 실제 RMS |
| --- | --- | --- | --- |
| 1.15 | 0.9~1.1 | 0.000000154 | 0.220878810 |
| 0.85 | 0.9~1.1 | 0.000000142 | 0.220879055 |
| 1.16 | 1/1.1~1/0.9 | 0.000000279 | 0.215970456 |
| 0.86 | 1/1.1~1/0.9 | 0.000000232 | 0.216863139 |

`bounded-scale-v1`은 점군을 바꾸기 전에 누적 배율을 제한해 단계 배율을 계산하고 `T=Gmodel−sRGdata`로 대응점 중심을 유지합니다. 누적 변환은 실제 적용한 단계 변환으로만 갱신합니다. 수정 후 위 네 사례에서 실제 행렬 RMS·반환 RMS와 변환 중심이 허용오차 `3e-6` 이내로 일치합니다. 범위 안 유사변환·강체·삼각면 모델 회귀도 함께 통과했습니다.

- 원문 LF 정규화 SHA-256: `dfbc07ef31c7256c3361e156b7558ec727f23487184cabd2105899c9a2fb72cf`
- 패치 SHA-256: `66eb7b07c45d111530c3c0639eb0bcf57db2b262c7bf330a695b1e3c6fec042a`
- 수정 원문 SHA-256: `329eef87ce8160ff07b0e2f91f38f164951f0511294bf870b3fd0b33b6623dae`

### 멀티스레드 공유 상태 수정

고정 코어의 TBB 탐색문은 버전이 두 번째 구성요소로 해석되는 인자 순서였고, 단일 명령 내부 TBB 태스크가 공유하는 옥트리 성공 플래그와 C2M 성공·오류 필드에는 동기화되지 않은 읽기·쓰기가 있었습니다. `multithread-state-race-v1`은 `CMakeLists.txt`에서 oneTBB `2022.0.0` 정확한 버전을 필수로 찾고 `include/DgmOctree.h`, `src/DgmOctree.cpp`, `src/DistanceComputationTools.cpp`의 해당 필드만 원자화합니다. v3 워커는 한 프로세스에서 명령을 순차 처리하므로 서로 다른 명령 사이의 전역 C2M 래퍼 경쟁은 실행 경로에 없습니다.

- 패치 SHA-256: `6c4d52fad0721d676761893ddb0eb558a57edf4636bd0d32695ab8bbfc584eb8`
- `CMakeLists.txt` 원문/수정 SHA-256: `96a39725ef0f486a785829a6e736c84683eabdbd309b788e19b33f28e5d593e9` / `0dec383b0c7c12af414bb8faba02f5cd0bdc1d34b353dd06b5004711ac6204ab`
- `include/DgmOctree.h` 원문/수정 SHA-256: `e22fbb2f00f965863d92f50658df9ad6e8acdffc8a1dc8ae086cc7919c6f335d` / `c31678e8f42d387fd17974b5a7129d9e3ba912101de35e12c187a062cc609117`
- `src/DgmOctree.cpp` 원문/수정 SHA-256: `520dcd3f0705de3a188bf3850f02bd10515f642a37bff9d58f5b768ba037cb50` / `7077f4ccd9d4c768568ca7d57775ff3503592b3afcd27d827f9def9f70a8c137`
- `src/DistanceComputationTools.cpp` 원문/수정 SHA-256: `6d1bf3774fff53a981a7f8a19f47a37a98963b42d3e81bf4b3587e5c424963bb` / `1fe77038451dfed19dcd3a772d95519c6e729b6c80c817e949487a1c566af7dc`

패치 파일과 변경된 코어 파일을 포함한 전체 CCCoreLib 원문, nanoflann 원문, JSON 헤더, 고정 oneTBB 공식 ZIP과 워커 소스는 함께 배포한 `cccore_rebuild_sources.zip`에 들어 있습니다. 각 원문 라이선스와 수정 표시를 유지했습니다. 최종 바이너리·원문 묶음 해시는 `runtime_manifest.json`에서 확인할 수 있습니다.

## 저장소에서 재빌드

PowerShell 7에서 다음 명령을 실행합니다. 출력은 지정한 격리 환경의 `cache/`와 `runtime/cccore/` 내부에만 생성합니다.

```powershell
.\build_tools\prepare_cccore_runtime.ps1 -EnvironmentRoot '<격리환경절대경로>'
& '<격리환경절대경로>\runtime\python\python.exe' -X utf8 -I .\tests\align\native\test_cccore_worker.py
```

## 배포된 원문만으로 재빌드

원문 ZIP을 별도 작업 폴더에 풀고, 해당 폴더에서 실행합니다. ZIP의 코어는 두 최소 패치가 적용된 상태이며 `tbb/`에는 검증된 공식 Windows 패키지가 있습니다. 네트워크 다운로드가 필요하지 않습니다.

```powershell
$cmake = 'C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
Expand-Archive -LiteralPath .\tbb\oneapi-tbb-2022.0.0-win.zip -DestinationPath .\tbb
& $cmake -S .\native -B .\build -G 'Visual Studio 17 2022' -A x64 `
  "-DCCCORE_SOURCE=$PWD\cccore-src" "-DJSON_INCLUDE_DIR=$PWD\json" `
  "-DTBB_DIR=$PWD\tbb\oneapi-tbb-2022.0.0\lib\cmake\tbb" `
  -DCORE_COMMIT=dc8c7d80f4ef8fd6a2c302c8d5f9c35099ba8432 `
  -DCORE_PATCH=bounded-scale-v1+multithread-state-race-v1
& $cmake --build .\build --config Release --parallel 8
Copy-Item -LiteralPath .\tbb\oneapi-tbb-2022.0.0\redist\intel64\vc14\tbb12.dll -Destination .\build\bin\tbb12.dll
& .\build\bin\colmap_cccore_worker.exe --check-environment
```

`build/bin`에 워커, `CCCoreLib.dll`, `tbb12.dll`이 준비됩니다. 배포 런타임에서 Microsoft C++ 재배포 DLL 네 파일과 oneTBB 라이선스·제삼자 고지를 함께 유지합니다. Windows의 Universal CRT는 운영체제 제공 구성요소를 사용합니다.

준비 스크립트는 매 실행마다 새 빌드·원문·런타임 스테이징을 만들고 허용 파일 목록, v3 Manifest 연결, 자체 검사와 단일 세션·캐시 네이티브 회귀를 모두 확인한 뒤 런타임 디렉터리를 교체합니다. 실패하면 기존 런타임을 유지하거나 교체 직전 백업으로 되돌리므로 이전 파일이 원문 ZIP이나 최종 배포에 섞이지 않습니다.

## 삼각면 거리의 옥트리 단계 선택

독립 거리 API의 삼각면 경로는 `octreeLevel=0`을 자동 보정하지 않아 실데이터 약 3만 점과 6만 5천 면을 단일 셀에서 비교했습니다. 기존 실험의 전체 계산은 약 264초였으며 거리 평가가 대부분을 차지했습니다. 워커는 동일 고정 코어의 `RegistrationTools.cpp`가 사용하는 공개 `DgmOctree::findBestLevelForComparisonWithOctree`로 점군과 메시 꼭짓점의 옥트리 단계 값을 선택합니다. 거리 계산 자체는 공식 `computeCloud2MeshDistances`를 그대로 사용합니다. 임의 고정 단계나 거리 근사는 적용하지 않았습니다.

회귀는 삼각면 내부·외부의 기존 해석적 정답을 유지하고, 64800개 삼각면과 30000개 점의 해석적 거리 및 15초 처리 한도를 검사합니다. 이 변경은 워커의 공개 API 설정이며 코어 원문 수정은 위에 고지한 두 최소 패치뿐입니다.

## v3 세션·수명·취소 계약

`--session` 작업자는 request/ready, 단조 증가 command request/response, progress, cancel, close 파일만 사용한다. 세션·작업·입력 지문과 상대 경로를 매 단계 확인하며 v2 `--request`와 한 프로세스 안에서 혼용하지 않는다. 기본 A/P PointCloud·SimpleMesh는 `cache_scope=SESSION`, 변형 평가·등록 PointCloud는 `cache_scope=COMMAND`로 등록한다. 성공 응답 기록 뒤 COMMAND 항목을 해제하고, close 통계의 상주 바이트는 남은 SESSION 항목의 추정 바이트 합계다.

취소 확인은 모든 CCCore 콜백에서 파일 시스템을 조회하지 않는다. 원자 latch, `GetTickCount64()` 25ms 제한과 CAS 승자 한 스레드만 `GetFileAttributesW`를 호출한다. `ERROR_FILE_NOT_FOUND`와 `ERROR_PATH_NOT_FOUND`만 미요청으로 처리하고 다른 오류는 실패로 닫는다. terminal progress 기록 전후에는 제한을 건너뛴 강제 확인을 수행한다. 결과·응답·요청의 내구 원자 기록과 2초 강제 종료 계약은 그대로 유지한다.

후보별 전체 변형 이동 메시 대신 inverse query와 원본 메시를 사용하는 경로, COMMAND 해제 후 ID 재사용 거부, terminal 취소 경계, 병렬 취소 race와 정확 평가 캐시의 물리·논리 연산 분리를 네이티브·Python 회귀에서 검증한다. 공개 옥트리 사전 구축 후보는 거리 바이트가 같아도 반복 측정에서 control보다 7.25% 느려 소스와 런타임에 포함하지 않는다.
