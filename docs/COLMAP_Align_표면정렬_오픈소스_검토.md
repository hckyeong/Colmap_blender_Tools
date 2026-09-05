# COLMAP Align 표면 정렬 오픈소스 검토

- 조사일: 2026-09-03
- 대상: `codex/align-test-env` 실험 애드온의 연결 OBJ 표면 정밀 정렬
- 범위: 공식 문서·공개 소스·라이선스 조사. 설치·성능 비교·기능 구현은 수행하지 않았다.
- 기존 흐름: 메시 마커 초기 정렬 이후 Small GICP에는 소유 COLMAP 희소 점군이 입력된다.
- 원하는 결과: 연결 메시 표면을 정밀 정렬 기준으로 사용하고, 얻은 하나의 변환을 소유 COLMAP 점군·카메라·메시에 함께 적용한다.

## 1. 표면 정렬의 구분

메시 표면을 이용한 정렬도 반복 계산에서는 점 또는 표본을 사용한다. 다음 두 경로를 구분해야 한다.

1. 양쪽 메시에서 표본을 추출하고 표본끼리 ICP/GICP를 수행한다. 원래 희소 타이포인트보다 표면을 풍부하게 표현하지만, 대상은 유한한 표본 점 집합이다.
2. 이동 메시의 정점·표면 표본에서 기준 메시의 삼각형 면 위 최근접점을 찾는다. 기준 메시의 면 내부도 대응 위치가 될 수 있다. 이 문서의 직접 표면 대응 후보는 이 경로를 제공한다.

VTK는 한 표면의 정점을 다른 표면 위 최근접점에 반복 대응시키는 방식을 명시한다. CloudCompare와 trimesh에서도 대상 메시 면을 실제 검색하는 경로를 확인했다. [VTK ICP](https://vtk.org/doc/nightly/html/classvtkIterativeClosestPointTransform.html), [CloudCompare 등록 코드](https://github.com/CloudCompare/CloudCompare/blob/master/qCC/ccRegistrationTools.cpp), [trimesh ICP 코드](https://github.com/mikedh/trimesh/blob/main/trimesh/registration.py)

## 2. 우선 검토 후보

| 후보 | 확인한 표면 처리 | 변환·주요 설정 | 적용 판단 |
| --- | --- | --- | --- |
| CloudCompare / CCCoreLib | 이동 메시에서 표면 표본을 생성하고 기준 메시를 면 검색 대상으로 전달한다. | 변환 행렬, 겹침 비율, 균일 스케일 보정 | 부분 겹침이 있는 RC 컴포넌트의 비교 기준으로 우선 검토 |
| VTK | `vtkIterativeClosestPointTransform`이 기준 표면의 최근접점과 대응한다. | 4×4 행렬, 강체·Similarity 모드, 반복 수·표본 수 | Python 외부 작업 프로세스에 연결할 라이브러리 후보 |
| trimesh | `registration.icp(a, b_mesh)`에서 `b.nearest.on_surface(a)` 호출을 확인했다. | 초기 4×4 행렬, 결과 4×4 행렬, 스케일·반사 허용 여부 | 작은 비교 실험과 구현 기준을 확인하기 좋은 후보 |

위 우선순위는 현재 요구에 대한 기술적 판단이다. 속도나 정렬 정확도의 실측 순위가 아니다.

### CloudCompare / CCCoreLib

현재 등록 코드에서는 이동 대상이 메시이면 표면에서 표본을 생성한다. 기준 대상이 메시이면 `modelMesh`를 유지한 채 `ICPRegistrationTools::Register`에 전달한다. CCCoreLib API도 점군-메시 정렬에서 메시를 고정 기준으로 사용하는 계약을 명시한다. [등록 소스](https://github.com/CloudCompare/CloudCompare/blob/master/qCC/ccRegistrationTools.cpp), [CCCoreLib ICP API](https://cloudcompare.org/doc/CCLib/html/class_c_c_core_lib_1_1_i_c_p_registration_tools.html)

겹침 비율과 스케일 보정 옵션이 있어 서로 일부만 겹치는 컴포넌트의 비교에 적합한 후보로 판단한다. 결과 변환 행렬을 다른 데이터에 적용하는 절차도 공식 문서에 있다. [ICP 설정](https://cloudcompare.org/doc/wiki/index.php/ICP), [변환 행렬 활용](https://cloudcompare.org/doc/wiki/index.php/Alignment_and_Registration)

CloudCompare 애플리케이션은 GPL v2 이상이고, CCCoreLib의 저장소 라이선스는 GNU Library GPL v2다. 프로그램 실행 연계와 라이브러리 직접 통합은 서로 다른 배포 방식으로 검토한다. [CloudCompare 라이선스](https://github.com/CloudCompare/CloudCompare/blob/master/license.txt), [CCCoreLib 라이선스](https://github.com/CloudCompare/CCCoreLib/blob/master/LICENSE.txt)

### VTK

공식 ICP는 원본 표면 정점과 대상 표면의 최근접점을 대응시키며 `vtkCellLocator`를 사용한다. Python 예제가 제공된다. [ICP API](https://vtk.org/doc/nightly/html/classvtkIterativeClosestPointTransform.html), [Python 예제](https://examples.vtk.org/site/PythonicAPI/PolyData/AlignTwoPolyDatas/)

내부 `vtkLandmarkTransform`은 회전·이동만 허용하는 강체 모드와 균일 스케일까지 허용하는 Similarity 모드를 제공한다. 기본값에 의존하지 않고 모드를 명시해야 한다. 기준 ICP에는 표면 표본 밀도와 부분 겹침 처리를 별도로 검토해야 하므로 기본값만으로 높은 정확도를 보장할 수 없다. [변환 모드](https://vtk.org/doc/nightly/html/classvtkLandmarkTransform.html)

라이선스는 BSD 3조건 계열이다. [VTK 저작권·라이선스](https://github.com/Kitware/VTK/blob/master/Copyright.txt)

### trimesh

기존 3점 정렬의 초기 행렬을 `registration.icp`에 넘기고, 이동 메시에서 만든 표본과 기준 메시를 입력하는 경로가 적합하다. `mesh_other`도 메시 간 정렬을 제공하지만 주축 기반 초기 탐색을 포함하므로 현재 마커 초기값을 쓰는 경로와 구분한다. 초기 정렬이 대략 맞아야 한다는 조건도 문서에 명시되어 있다. [등록 API](https://trimesh.org/trimesh.registration.html)

COLMAP 적용에서는 반사를 허용하지 않도록 `reflection=False`를 명시한다. 스케일은 기존 3점 정렬에서 결정한 값을 고정할지, 제한된 범위에서 추가 보정할지 비교한다. 기본 ICP 반복은 최근접점과 Procrustes 계산이므로 부분 겹침·이상점 대응은 추가 평가 대상이다.

라이선스는 MIT다. [라이선스](https://github.com/mikedh/trimesh/blob/main/LICENSE.md)

## 3. 함께 확인한 후보

- **PyMeshLab**: 공식 문서에 `compute_matrix_by_icp_between_meshes`와 표본 수·거리·법선 분포 설정이 있다. 다만 문서의 설명은 점 집합 ICP이며, 이번 조사에서는 현재 배포본의 삼각면 대응 경로와 Windows 런타임을 실제 검증하지 않았다. 우선 후보와 같은 수준으로 표면 처리 방식을 확정하지 않는다. [필터 문서](https://pymeshlab.readthedocs.io/en/latest/filter_list.html#compute-matrix-by-icp-between-meshes), [GPL v3 라이선스](https://github.com/cnr-isti-vclab/PyMeshLab/blob/main/LICENSE)
- **MeshLib**: 메시 두 개를 받아 ICP 변환을 반환하는 공식 Python/C++ 예제가 있다. 그러나 현재 저장소는 비상업용 무료·상업용 별도 라이선스를 명시하므로, 앞선 오픈소스 후보와 구분한다. [메시 ICP 예제](https://meshlib.io/documentation/ExampleMeshICP.html), [현재 라이선스 안내](https://github.com/MeshInspector/MeshLib#license)
- **Open3D / 기존 Small GICP의 표면 표본 경로**: 메시에서 표본을 만든 후 점군 등록에 사용할 수 있는 별도 비교안이다. GICP API의 입력은 점군이므로 기준 삼각형 면을 직접 검색하는 기능과 같다고 설명하지 않는다. [Open3D GICP 입력](https://www.open3d.org/html/python_api/open3d.pipelines.registration.registration_generalized_icp.html)

## 4. 현재 프로젝트에 대한 권고

**CloudCompare로 표면 정렬 결과를 먼저 비교하고, 자동화 후보로 VTK 또는 CCCoreLib를 평가하는 순서를 권고한다.** trimesh는 같은 초기값·같은 데이터에서 기준 동작을 빠르게 비교할 후보로 둔다.

COLMAP에 동일한 행렬을 적용하려면 회전·이동, 필요 시 양의 균일 스케일을 가진 변환을 사용한다. 메시를 부분적으로 늘이거나 휘는 비강체 정렬은 하나의 Sim(3) 행렬로 카메라와 점군에 적용할 수 없으므로 이번 목표에서 제외한다.

후속 실험을 진행한다면 기존 메시 3점 초기값을 공통으로 사용하고 다음을 비교한다.

1. 현재 희소 점군 Small GICP
2. 메시 표면 표본 Small GICP
3. 기준 삼각형 면을 직접 검색하는 표면 ICP

평가는 학습에 사용한 대응점 RMS 하나로 결정하지 않는다. 겹치는 표면의 별도 표본으로 양방향 표면 거리, 상위 오차 구간, 국소 부위의 어긋남과 실행 시간을 비교한다. 삼각형 밀도 차이·뚫린 부분·겹치지 않는 영역과 스케일 자유도가 결과에 미치는 영향도 함께 본다.

현재 단계에서는 실제 RC 메시로 후보 간 정확도나 개선율을 측정하지 않았다.
