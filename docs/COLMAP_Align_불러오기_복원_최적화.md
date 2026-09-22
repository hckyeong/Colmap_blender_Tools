# Align 불러오기·복원 최적화

## 범위와 근거

- 대상은 1단계 OBJ 불러오기·내장 Mesh 복원이다. 정렬 알고리듬·UI·원본 OBJ는 변경하지 않는다.
- `Taiwan_armadillo_scanHighpoly.obj`는 921,654,682바이트, 정점 10,173,478개, 삼각형 20,128,488개이다.
- 변경 전 소스의 Blender 5.2 GUI 별도 프로세스 측정: 불러오기 127.776초, 복원 171.823초. 복원 시간은 `.blend` 열기 제외이다.
- 면마다 `np.unique`를 실행하는 전체 형상 검증이 불러오기에서 2회(98.844초), 복원에서 3회(144.757초) 발생했다. GPU 준비에서도 같은 정점 법선을 두 번 계산했다.

## 구현과 보존 계약

1. 면 크기별로 묶어 반복 정점을 검사한다. 삼각형은 세 쌍 비교, 나머지는 각 행을 정렬해 인접 중복을 검사한다. 일반 묶음은 최대 262,144코너이며 단일 거대 N-gon은 해당 면 크기를 사용한다.
2. 좌표·UV는 Blender float32 버퍼로 읽은 뒤 기존 float64 계약으로 변환한다. 정수 배열의 추출 경로는 유지한다.
3. 불러오기에서는 식별자 지정과 프록시 생성을 같은 동기 호출로 묶어 전체 검증을 한 번 수행한다. 검증한 좌표를 즉시 프록시에 사용한다.
4. 복원 사전 점검은 식별자·소유권을 검사한다. 프록시 생성 경계에서 식별자를 다시 확인하고 전체 형상을 검사한다. 여러 타이머 호출 사이에서 검증 결과를 캐시하거나 신뢰하지 않는다.
5. GPU 배치 준비에서 이미 계산한 법선을 면 배열 생성에 전달한다. 기존 단독 호출도 동작한다.

원본 Polygon·Loop·UV·재질·면 순서·좌표계를 유지한다. `validate_embedded_obj_mesh`와 `commit_embedded_obj_mesh`의 공개 동작은 유지한다. 기존 UUID·리비전·Fake User·비연결 조건, 비유한 좌표·인덱스 범위·연속 Loop·반복 정점 검사를 유지한다. 실패 시 기존 staging 경로에서 정리하고 공개 프로젝트 상태를 확정하지 않는다. 내장 복원은 외부 OBJ에 접근하지 않으며 구버전 출처 해시 검증은 유지한다.

## 검증

Blender 5.2.0 LTS에서 다음 11개 테스트 스크립트가 통과했다. 실행 형식은 `blender.exe --background --factory-startup --python-exit-code 1 --python tests/align/blender/<스크립트>`이다.

- `test_embedded_obj_optimization.py`, `test_mesh_normal_reuse.py`
- `test_embedded_obj.py`, `test_mesh_extension.py`
- `test_companion_import.py`, `test_companion_persistence.py`, `test_companion_display.py`
- `test_project_restore.py`, `test_project_restore_roundtrip.py`
- `test_obj_export.py`, `test_mesh_surface_picking.py`

새 검증은 혼합 면·묶음 경계·비인접 반복 정점·거대 N-gon, 좌표·UV의 정확한 값 보존, 가져오기/복원당 형상 검증 한 번, 사전 점검 이후 비유한 좌표·리비전 변경 시 부분 복원 거부를 확인했다. 법선은 무텍스처·혼합·전체 텍스처 경로에서 기존 결과와 배열 단위로 동일하며, 배치당 한 번 계산한다. 새 스크립트 두 개는 정식 소스 회귀 목록에도 등록했다.

## 실제 OBJ 비교 결과

| 항목 | 변경 전 | 변경 후 | 속도 향상 |
|---|---:|---:|---:|
| 불러오기, GPU 배치 준비 포함 | 127.776초 | 29.216초 | 4.37배 |
| 내장 복원, GPU 배치 준비 포함 | 171.823초 | 25.421초 | 6.76배 |
| 불러오기 전체 형상 검증 합계 | 98.844초, 2회 | 4.667초, 1회 | 21.18배 |
| 복원 전체 형상 검증 합계 | 144.757초, 3회 | 4.453초, 1회 | 32.51배 |
| 불러오기 GPU 준비 단계 | 7.494초 | 3.698초 | 2.03배 |

변경 후 면 내부 반복 정점 검사 자체는 불러오기 0.358초, 복원 0.356초였다. 가져온 배열과 새 프로세스에서 복원한 배열의 SHA-256이 모두 일치했다. 변경 전후 정점·면·UV·표시 표본 개수도 일치하며 원본 OBJ의 크기와 수정 시각은 유지됐다.

2026-09-18, Ryzen 9 7950X3D·RTX 4090·RAM 약 128GB·NumPy 2.3.4에서 동일 파일을 각 조건 1회 측정했다. 별도 숨김 GUI Blender에서 기존 작업 단계를 직접 진행하고 실제 GPU 배치와 256×256 면 그리기를 확인했다. 표의 시간은 프로세스 시작, 타이머 대기, 첫 면 그리기, `.blend` 열기/저장 및 검증용 해시 계산을 제외한다. 파일 캐시를 비우지 않았으므로 콜드 디스크 성능이나 통계적 평균을 뜻하지 않는다. 현재 소스 기준이며 설치 ZIP 배포·설치본 변경은 포함하지 않는다.

상세 로컬 산출물은 `build/align-import-review-20260918/`와 `build/align-import-optimized-20260918/`의 `import.json`, `restore.json` 및 최적화 폴더의 `comparison.json`에 보관한다. 측정용 임시 `.blend`는 삭제하며 원본 데이터와 측정 로그는 Git에 포함하지 않는다.

남은 비용은 불러오기 기준 OBJ 읽기 7.31초, 형상 검사·배열 추출·프록시 생성 17.18초, 표시 표본 1.01초, GPU 준비 3.70초이다. 각 준비 단계 내부는 여전히 동기 실행이므로 UI 응답성을 위한 별도 분할·워커 적용까지 해결한 것은 아니다.

## 0.3.51 빌드·설치

2026-09-18 사용자 요청으로 `build/colmap_tools-0.3.51.zip`을 정식 CCCoreLib 프로필로 빌드하고 Blender 5.2의 `user_default` 저장소에 설치했다. 기존 0.3.50 ZIP과 비교하면 버전·빌드 프로필 외 제품 코드 변경은 이 문서의 Align 네 파일뿐이며, 기존 Modifier·패널 수정은 이전 ZIP에도 이미 포함되어 있었다.

- 정식 빌드의 구조·공존·Core·Blender 등록, 네이티브 정렬 및 ZIP 무결성 검사가 통과했다.
- `test_installed_extension.ps1`의 격리 설치·등록·네 가지 입력 조합 정렬·OBJ/UV 입출력 검사가 통과했다.
- 별도 Blender 프로세스에서 실제 설치본 0.3.51 활성화와 1,170개 배포 파일의 해시 일치를 확인했다. 사용자 환경설정 파일의 SHA-256은 설치 전후 동일했다.
- ZIP SHA-256: `dc9d6c40e10c95d1f47e4cb11c48dd5ea278d2d709028162d182e81d6c894e05`.
- 로컬 검증 기록: `build/verification/align-optimization-0.3.51/`. 실제 대용량 성능 수치는 위 소스 측정 결과이며 설치 후 대용량 재계측이나 사용자 직접 조작 검수 결과를 뜻하지 않는다.

Blender 버퍼 구현 근거: [공식 RNA foreach 구현](https://github.com/blender/blender/blob/main/source/blender/python/intern/bpy_rna.cc).
