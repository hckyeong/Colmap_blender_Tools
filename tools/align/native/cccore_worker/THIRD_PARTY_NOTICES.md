# 제삼자 소프트웨어 고지

이 런타임은 다음 원문과 라이선스를 포함합니다.

| 구성요소 | 채택 버전 또는 커밋 | 라이선스 파일 |
| --- | --- | --- |
| CCCoreLib 공유 라이브러리 | dc8c7d80f4ef8fd6a2c302c8d5f9c35099ba8432 + bounded-scale-v1 + multithread-state-race-v1 + farthest-mesh-correspondence-v1 + mesh-grid-memory-budget-v1 | licenses/CCCoreLib-LGPL-2.0.txt |
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
