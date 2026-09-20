# 제삼자 소프트웨어 고지

이 런타임은 다음 원문과 라이선스를 포함합니다.

| 구성요소 | 채택 버전 또는 커밋 | 라이선스 파일 |
| --- | --- | --- |
| CCCoreLib 공유 라이브러리 | dc8c7d80f4ef8fd6a2c302c8d5f9c35099ba8432 + bounded-scale-v1 + multithread-state-race-v1 + farthest-mesh-correspondence-v1 + mesh-grid-memory-budget-v1 + mesh-grid-reuse-v1 + mesh-grid-fixed-domain-v1 + icp-mesh-grid-reuse-v1 | licenses/CCCoreLib-LGPL-2.0.txt |
| CCCoreLib 빌드 구성 | 같은 커밋 | licenses/CCCoreLib-build-MIT.txt |
| nanoflann | 1.7.1, 9a653cb243db6a09c94f833b28732b62f033e2b5 | licenses/nanoflann-BSD.txt |
| nlohmann/json | 3.11.3 | licenses/json-MIT.txt |
| oneTBB | 2022.0.0 | licenses/oneTBB-Apache-2.0.txt, licenses/oneTBB-third-party-programs.txt |
| COLMAP 독립 워커 | 함께 제공한 원문 | licenses/COLMAP-Tools-GPL.txt |

CCCoreLib는 LGPL-2.0-or-later로 제공됩니다. `bounded-scale-v1`에서 등록 단계의 배율 경계와 반환 변환 일관성을 수정했습니다. `multithread-state-race-v1`에서 TBB 탐색 인자 순서와 정확한 버전을 고정하고 단일 워커 호출 내부 TBB 태스크가 공유하는 옥트리와 C2M 성공·오류 상태를 원자화했습니다. 변경 원문, 서로 분리된 패치, 원본 식별자와 재빌드 절차를 `cccore_rebuild_sources.zip` 및 `REBUILD.md`로 함께 제공합니다. 공유 DLL을 수정·재빌드·교체할 수 있으며, 해당 라이브러리 수정의 디버깅에 필요한 분석을 제한하는 추가 조항은 없습니다.

oneTBB는 Apache License 2.0으로 제공됩니다. 공식 Windows x64 패키지에서 `tbb12.dll`, 라이선스 원문과 제삼자 프로그램 고지를 수정 없이 배포하며, 같은 공식 ZIP을 오프라인 재빌드 묶음에 포함합니다.

Microsoft Visual C++ 런타임 `msvcp140.dll`, `vcruntime140.dll`, `vcruntime140_1.dll`, `concrt140.dll`은 설치된 Visual Studio 2022의 재배포 디렉터리에서 수정 없이 복사합니다. 이 파일들은 Microsoft 소프트웨어 사용 조건에 따르며 오픈소스 라이선스로 재라이선스하지 않습니다. [Visual Studio 2022 재배포 가능 파일 안내](https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution)와 [제품 사용 조건](https://visualstudio.microsoft.com/license-terms/)을 참조하세요.

`farthest-mesh-correspondence-v1`은 메시 등록의 최원거리점 필터가 대응점과 최근접 삼각형 식별자를 함께 복사하도록 수정합니다. 기존 배율·동시성 패치는 변경하지 않으며, 별도 안전 패치 원문과 적용 전후 SHA-256을 잠금 파일 및 재빌드 원문 묶음에 보존합니다. 거리 제한이나 등록 알고리즘의 수치 기준을 추가하지 않습니다.

이 패키지는 CloudCompare GUI 실행 파일이나 GUI `qCC` 소스를 포함하지 않습니다.

`mesh-grid-memory-budget-v1`은 정확한 점–메시 거리 계산의 격자 단계를 실제 경계와 포인터 배열 메모리 예산에 맞게 제한합니다. Windows 가용 물리·커밋 메모리와 고정 최대 예산을 함께 사용하며, 원본 형상·표본·거리 근사를 변경하지 않습니다. 준비·등록·평가의 공통 코어 경로에 적용하고 별도 패치 및 적용 전후 원문 해시를 재빌드 묶음에 제공합니다.

`mesh-grid-reuse-v1`은 불변 메시·경계·단계·셀 크기가 같은 독립 거리 호출에서 완성된 격자를 재사용하는 별도 API를 추가합니다. 기존 거리 함수와 메모리 예산을 유지하며, 캐시 메모리 추정 함수와 별도 패치·최종 원문 해시를 재빌드 묶음에 제공합니다.

`mesh-grid-fixed-domain-v1`은 준비 단계의 전체 형상 범위를 명시하여 검사점 묶음이 달라도 동일 좌표계의 격자를 재사용하는 별도 API를 추가합니다. 범위 유효성과 모든 검사점의 포함 여부를 확인하며 기존 정확 거리 함수와 격자 메모리 제한은 유지합니다. `Grid3D.h`의 교차 후보 검사에서 평면 부호 0을 중립으로 처리하여 셀 경계에 닿은 삼각형 누락도 수정합니다. 적용 전후 원문 해시와 별도 패치를 재빌드 묶음에 제공합니다.

`icp-mesh-grid-reuse-v1`은 메시를 기준으로 하는 ICP 등록에 별도 실행 문맥을 추가합니다. 등록 내부에서 격자 한 개를 유지하고 이동한 검사점의 옥트리·최근접점은 다시 계산합니다. 범위를 벗어나면 물리 셀 크기를 유지하며 포함 범위를 확대하고 격자를 다시 만듭니다. 기존 메모리 예산과 취소·배율·필터 동작을 유지하며, 한 셀 축의 공통 원점과 병렬 거리 계산의 스칼라 오프셋 초기화를 보정합니다. 실제 반복 횟수와 내부 구간 시간을 기록하며 기존 등록 API는 유지합니다. 다섯 파일의 적용 전후 원문 해시와 패치를 재빌드 묶음에 제공합니다.

`mesh-bvh-search-v1`은 기존 공개 등록 API를 보존하면서 정확 최근접 표면 검색을 주입하는 별도 등록 경로를 추가합니다. 수정된 CCCoreLib 두 파일과 패치·적용 전후 해시는 기존 LGPL 변경 원문 묶음에 제공합니다. 워커의 `mesh_bvh.h`와 `mesh_bvh.cpp`는 C++17 자체 구현으로 워커 원문 및 기존 COLMAP Tools 라이선스에 포함되며 새 제삼자 의존성은 추가하지 않습니다.
