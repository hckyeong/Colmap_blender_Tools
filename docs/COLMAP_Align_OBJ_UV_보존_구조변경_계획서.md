# COLMAP Align OBJ UV 보존 구조변경 계획서

## 1. 문서 정보

- 대상 도구: `COLMAP Align`
- 대상 기능: OBJ Import, Runtime Mesh Model, 정렬 결과 OBJ Export
- 기준 버전: `COLMAP Tools 0.1.15`
- 구현 목표 버전: `0.1.16`
- 구현 상태: `0.1.16` 구현·자동 회귀 완료
- 작성 목적: OBJ를 정렬한 뒤에도 원본 UV 좌표와 Face별 UV 연결을 유지한다.

## 2. 결정 요약

현재 OBJ Import는 정점 `v`와 삼각면 `f`의 정점 인덱스만 읽는다. `vt`, `vn`, `usemtl`, `mtllib`은 무시하며 Face Token의 `/` 뒤 내용도 버린다. 따라서 UV는 이동 중에 삭제되는 것이 아니라 Import 시점부터 Runtime에 들어오지 않는다. Export 역시 `v`와 `f`만 기록하므로 정렬 결과 OBJ에는 UV가 없다.

이번 변경은 다음 구조로 진행한다.

1. OBJ Reader가 `vt`와 Face Corner의 UV 인덱스를 읽는다.
2. UV를 정점 속성이 아니라 **삼각면 코너별 별도 인덱스**로 Runtime Model에 보관한다.
3. Align은 3D 정점에만 행렬을 적용하고 UV 값은 변경하지 않는다.
4. OBJ Export는 `vt`와 `f v/vt`를 다시 기록한다.
5. 반사 변환으로 Face Winding을 뒤집을 때 정점과 UV Corner 순서를 함께 뒤집는다.
6. Export 결과를 다시 읽어 UV 값과 Face별 UV 인덱스까지 검증한 뒤에만 최종 폴더로 승격한다.

```text
원본 OBJ
├─ v --------------------------┐
├─ vt ---------------------┐   │
└─ f v/vt[/vn] --------┐   │   │
                       ▼   ▼   ▼
                ObjParseResult
                       │
                       ▼
             TriangleMeshModelData
              ├─ 3D 정점 ── Align 행렬 적용
              ├─ 삼각면 정점 인덱스
              ├─ UV 좌표 ─── 값 유지
              └─ 삼각면 UV 인덱스 ── Corner 순서 유지
                       │
                       ▼
               정렬 결과 OBJ Export
                 v + vt + f v/vt
```

## 3. 현재 문제와 원인

| 현재 위치 | 현재 동작 | UV 손실 지점 |
| --- | --- | --- |
| `tools/align/mesh_io/obj_reader.py` | `v`, `f`만 누적 | `vt` 레코드를 무시함 |
| `obj_reader._face_index()` | Face Token을 `/`에서 자르고 정점 인덱스만 읽음 | `v/vt`, `v/vt/vn`의 `vt` 인덱스를 버림 |
| `ObjParseResult` | 정점·삼각면만 보관 | UV 배열과 Corner 인덱스가 없음 |
| `TriangleMeshModelData` | 정점·삼각면·표시 인덱스만 보관 | Import 이후 UV를 유지할 Runtime 필드가 없음 |
| `ObjExportModelSnapshot` | 정점과 삼각면만 고정 | Export Job에 UV가 전달되지 않음 |
| `IncrementalObjWriter` | `v`, `f i j k`만 기록 | `vt`, `f v/vt`를 출력하지 않음 |
| OBJ 왕복 검증 | 정점·Face·Bounds만 비교 | UV 누락을 성공으로 판정함 |

## 4. 목표

### 4.1 필수 목표

- OBJ의 UV 좌표 레코드 순서를 보존한다.
- 각 삼각면의 세 Corner가 참조하는 UV 인덱스를 보존한다.
- 하나의 3D 정점이 여러 UV를 갖는 UV Seam을 보존한다.
- Align의 이동·회전·균일 스케일은 3D 정점에만 적용한다.
- 정렬 결과 OBJ에 `vt`와 `f v/vt`를 기록한다.
- UV가 없는 기존 OBJ도 지금과 동일하게 Import·Align·Export할 수 있어야 한다.
- 원본 OBJ 파일은 절대 수정하지 않는다.
- 실패·취소 시 기존 Runtime과 기존 `aligned_obj` 결과를 유지한다.

### 4.2 이번 범위에서 제외

- MTL 파일 복사와 `mtllib`, `usemtl` 보존
- Texture Image 자동 복사 또는 경로 재작성
- OBJ Normal `vn` 보존
- Align Viewport의 Texture Shading
- Quad·N-gon 지원
- Blender Mesh Object 생성 또는 UV Editor 연동

UV만 보존해도 Blender에서 기존 Material을 다시 연결할 수 있다. 다만 MTL과 Texture 자동 연결은 별도 후속 작업이며, 이번 변경이 재질까지 보존한다고 표시하면 안 된다.

## 5. 핵심 데이터 계약

UV는 3D 정점별 값이 아니라 Face Corner별 연결이다. 같은 정점이 UV Seam 양쪽에서 서로 다른 `vt`를 참조할 수 있으므로 `vertex_count == uv_count`를 가정하거나 UV 기준으로 정점을 강제 분리하지 않는다.

### 5.1 `ObjParseResult`

다음 필드를 추가한다.

```python
texcoords_float64: np.ndarray                 # (T, 3)
texcoord_component_counts_uint8: np.ndarray  # (T,), 값 1~3
triangle_texcoord_indices_int32: np.ndarray  # (F, 3), UV 없음은 -1
```

- `texcoords_float64`는 `vt u`, `vt u v`, `vt u v w`를 동일한 연속 배열에 보관한다.
- 생략된 성분은 배열에서 `0.0`으로 채우고 실제 출력 성분 수는 `texcoord_component_counts_uint8`로 보존한다.
- `triangle_texcoord_indices_int32`는 0 기반 인덱스다.
- UV가 전혀 없는 OBJ는 `(0, 3)`, `(0,)`, Face 전체 `-1`로 표현한다.
- 한 Face는 세 Corner 모두 UV가 있거나 세 Corner 모두 없어야 한다. 한 Face 안에서 일부 Corner만 UV가 있는 입력은 명시적으로 거부한다.
- UV가 있는 Face와 없는 Face가 한 OBJ에 함께 있는 것은 허용한다.

### 5.2 `TriangleMeshModelData`

동일한 세 UV 필드를 추가하고 다음 불변 조건을 검증한다.

- UV 배열은 `float64`, 연속 배열, 유한값, 읽기 전용이다.
- UV 성분 수는 1~3이며 UV 레코드 수와 일치한다.
- Face UV 배열은 `int32`, `(face_count, 3)`이다.
- `-1` 이외의 UV 인덱스는 `texcoord_count`보다 작아야 한다.
- Face 한 행은 모두 `-1` 또는 모두 유효한 인덱스여야 한다.
- 기존 호출부 호환을 위해 UV 필드는 빈 배열 기본값을 제공한다.

다음 읽기 전용 속성을 제공한다.

```python
texture_coordinate_count: int
textured_face_count: int
has_texture_coordinates: bool
has_complete_texture_coordinates: bool
```

`has_complete_texture_coordinates`는 모든 Face에 UV가 있을 때만 `True`다.

### 5.3 메모리 제한

- 초기 구현은 최대 600,000 Face와 1,800,000개 UV 좌표를 하드 제한으로 사용했다.
- 2026-09-04 변경부터 Face와 UV 좌표의 인위적인 개수 상한을 제거하고 파일 전체를 읽는다.
- UV 좌표, 성분 수, Face UV 인덱스는 NumPy 연속 배열로 확정한 뒤 Python 누적 목록을 즉시 해제한다.
- Display Sampling과 ICP 입력에는 UV 배열을 복사하지 않는다.

## 6. OBJ Reader 변경

### 6.1 `vt` 레코드

- `vt`는 숫자 성분 1~3개만 허용한다.
- 모든 값은 유한한 `float64`여야 한다.
- 0개 또는 4개 이상 성분은 오류로 처리한다.
- 레코드 순서를 그대로 유지한다.
- UV 제한을 넘으면 부분 결과를 등록하지 않고 Import를 실패시킨다.

### 6.2 Face Token

다음 입력을 구분한다.

| Token | 정점 | UV | Normal |
| --- | --- | --- | --- |
| `v` | 사용 | 없음 | 없음 |
| `v/vt` | 사용 | 사용 | 없음 |
| `v//vn` | 사용 | 없음 | 현재처럼 보존하지 않음 |
| `v/vt/vn` | 사용 | 사용 | 현재처럼 보존하지 않음 |

- 정점 인덱스와 UV 인덱스를 별도 기준으로 해석한다.
- OBJ의 양수·음수 UV 인덱스를 모두 지원한다.
- `0` 인덱스, 범위 밖 인덱스, 비정수 인덱스는 명확한 오류로 처리한다.
- Streaming Parser 계약을 유지하기 위해 Face가 참조하는 정점과 UV는 해당 Face보다 먼저 정의되어 있어야 한다.
- UV 인덱스가 없는 `v//vn`은 UV 없음으로 기록한다.
- Normal 값 자체는 이번 변경에서 Runtime에 보존하지 않는다.

### 6.3 Import 진행률

`MeshImportProgress`에 `texture_coordinate_count`를 추가하고 Import 완료 메시지에 다음 정보를 표시한다.

```text
정점 120,000개 · 삼각면 200,000개 · UV 145,312개 · UV Face 200,000개
```

UV가 없으면 `UV 없음`으로 표시한다.

## 7. Align 처리 계약

### 7.1 정렬 행렬 적용

- Marker Align과 ICP는 기존처럼 정점 위치만 사용한다.
- Display Axis와 최종 Alignment 행렬은 `vertices_float64`에만 적용한다.
- `texcoords_float64` 값과 `triangle_texcoord_indices_int32`는 Marker Preview, ICP Preview, 승인, Undo·Redo 동안 변경하지 않는다.
- UV 배열은 ICP Worker 입력과 GPU Point Sampling 입력에서 제외한다.

### 7.2 반사 행렬

Display Axis와 Alignment 합성 행렬의 선형부 행렬식이 음수면 기존처럼 Face Winding을 반전한다.

```text
정점 Corner: [0, 1, 2] → [0, 2, 1]
UV Corner:   [a, b, c] → [a, c, b]
```

정점 인덱스만 뒤집고 UV 인덱스를 그대로 두면 Texture가 뒤틀리므로 두 배열에 정확히 같은 순열을 적용한다.

## 8. OBJ Export 변경

### 8.1 Export Snapshot

`ObjExportModelSnapshot`에 다음 필드를 추가한다.

```python
texture_coordinate_count: int
textured_face_count: int
texcoords_float64: np.ndarray
texcoord_component_counts_uint8: np.ndarray
triangle_texcoord_indices_int32: np.ndarray
```

Snapshot 생성 시 Runtime Model의 읽기 전용 UV 배열을 고정하고 Source Fingerprint, Geometry Revision, UV Count를 Export 입력 지문에 포함한다.

### 8.2 증분 Writer 단계

기존 단계 사이에 UV 기록 단계를 추가한다.

```text
SNAPSHOT_INPUTS
→ WRITING_VERTICES
→ WRITING_TEXCOORDS
→ WRITING_FACES
→ VALIDATING
→ REPORT
→ FINALIZING
```

- `v`는 정렬 행렬이 적용된 Project World 좌표로 기록한다.
- `vt`는 정렬 행렬을 적용하지 않고 원본 값과 원본 성분 수로 기록한다.
- UV가 있는 Face는 `f v/vt v/vt v/vt`로 기록한다.
- UV가 없는 Face는 기존처럼 `f v v v`로 기록한다.
- `vn`을 보존하지 않으므로 `v/vt/vn` 입력도 출력에서는 `v/vt`가 된다.
- 17자리 유효 숫자 포맷을 사용해 `float64` UV가 재파싱 시 같은 값으로 복원되게 한다.

### 8.3 Export 진행률과 보고서

`ObjExportProgress`에 UV 기록 수를 추가한다.

```python
written_texture_coordinate_count: int
total_texture_coordinate_count: int
```

`obj_export_report.json`은 Schema 2로 올리고 모델별로 다음을 기록한다.

```json
{
  "texture_coordinate_count": 145312,
  "textured_face_count": 200000,
  "uv_coverage": "FULL"
}
```

`uv_coverage` 값은 `NONE`, `PARTIAL`, `FULL` 중 하나다.

## 9. 왕복 검증 강화

출력 후보 OBJ를 새 Reader로 다시 읽은 뒤 다음 조건을 모두 검사한다.

- 정점 수와 좌표가 Project World 기대값과 일치한다.
- Face 정점 인덱스와 Reflection Winding이 일치한다.
- UV 레코드 수가 Snapshot과 일치한다.
- UV 성분 수와 값이 Snapshot과 일치한다.
- Face별 UV 인덱스가 기대값과 일치한다.
- Reflection이면 UV Corner 인덱스도 정점과 같은 순서로 반전되어 있다.
- UV Seam을 가진 동일 정점의 서로 다른 UV 참조가 유지된다.
- 검증 실패 시 후보 폴더를 삭제하고 기존 `aligned_obj`는 유지한다.

최종 자동 검증에서는 출력 OBJ를 Blender 5.2 기본 OBJ Importer로 불러와 Mesh UV Layer의 Face Corner 좌표도 비교한다. Blender의 UV Layer 역시 Face Corner 단위이므로 내부 인덱스 계약과 직접 대응한다.

## 10. 프로젝트 저장과 재로드

현재 Align 프로젝트 파일은 대형 메시 배열을 JSON에 복제하지 않고 Source Path, Source Fingerprint와 Alignment를 저장한다. 따라서 프로젝트 저장 Schema는 변경하지 않는다.

- 새 프로젝트: OBJ 원본 경로와 지문을 저장하고 재로드 시 `vt`를 다시 파싱한다.
- 기존 프로젝트: 같은 원본 OBJ가 존재하고 지문이 일치하면 새 Reader가 UV를 자동 복구한다.
- 원본 OBJ가 변경됐으면 기존과 같이 Stale 오류를 내고 임의로 다른 UV를 연결하지 않는다.
- Runtime 복원 후 UV Count와 Face UV 인덱스도 Source 재검증 대상에 포함한다.

## 11. 오류와 원자성

다음 상황은 Import를 실패시키고 기존 Data Layer를 유지한다.

- 잘못된 `vt` 성분 수
- NaN 또는 Infinity UV
- UV 인덱스 0
- 범위 밖 또는 비정수 UV 인덱스
- 한 Face 안의 부분 UV
- UV 레코드 제한 초과
- Import 중 원본 파일 변경

다음 상황은 Export 후보만 폐기하고 기존 출력은 유지한다.

- UV Snapshot 형상 또는 인덱스 손상
- UV 기록 중 취소
- 출력 OBJ UV 왕복 검증 실패
- 최종 Stale 검사에서 Source, Geometry, Alignment 또는 UV Count 변경 감지

## 12. 수정 대상

| 파일 | 작업 |
| --- | --- |
| `tools/align/constants.py` | UV 수 제한과 Export UV 단계 상수 추가 |
| `tools/align/mesh_io/obj_reader.py` | `vt`, Face UV 인덱스, 음수 인덱스 파싱과 검증 추가 |
| `tools/align/mesh_io/types.py` | Runtime Mesh UV 배열과 불변 조건 추가 |
| `tools/align/mesh_importer.py` | Parser UV 결과를 두 Staged Model 생성 경로에 전달하고 상태 문구 확장 |
| `tools/align/mesh_io/obj_writer.py` | UV Snapshot, 증분 `vt`, `f v/vt`, Reflection Corner 반전과 왕복 검증 추가 |
| `tools/align/operators/export_obj.py` | UV Snapshot 전달, UV 기록 단계, 진행률·보고서·Stale 검사 확장 |
| `tools/align/ui/panels.py` | Import·Export UV Count와 진행률 표시 |
| `tests/align/blender/test_mesh_extension.py` | Reader, Runtime, Seam, 오류와 프로젝트 재로드 회귀 추가 |
| `tests/align/blender/test_obj_export.py` | UV Export, Reflection, 원자성, 보고서와 왕복 회귀 추가 |
| `tests/align/blender/test_installed_mesh_surface.py` | 설치본의 UV Import·Export 계약 추가 |
| `docs/TESTING.md` | UV 보존 검증 명령과 승인 기준 추가 |
| `docs/migration/IMPLEMENTATION_STATUS.md` | 구현 완료 후 Build 상태 갱신 |

GPU Renderer, Marker Picking, ICP Solver, Small GICP Worker와 공용 Workspace 계층은 UV를 소비하지 않으므로 수정 대상이 아니다.

## 13. 테스트 계획

### 13.1 Reader 단위 테스트

- `vt u`, `vt u v`, `vt u v w`
- `f v/vt`, `f v/vt/vn`, `f v//vn`, `f v`
- 양수·음수 UV 인덱스
- 하나의 정점이 서로 다른 UV를 참조하는 Seam
- UV가 있는 Face와 없는 Face의 혼합
- UV 인덱스 0, 범위 초과, 비정수
- Face 내부 부분 UV 거부
- NaN·Infinity와 UV 상한 거부

### 13.2 Runtime 테스트

- UV 배열 dtype, 형상, 연속성, 읽기 전용 검증
- 기존 UV 없는 Constructor 호환
- Import 취소·실패 시 Repository 무변경
- Display Sampling과 ICP 입력이 UV 배열을 복사하지 않는지 확인
- Marker Align, ICP Preview, 승인과 Undo·Redo 전후 UV Fingerprint 동일
- 프로젝트 저장·재로드 후 UV Fingerprint 동일

### 13.3 Export 테스트

- Identity, Translation, Rotation, Uniform Scale에서 UV 값 불변
- Reflection에서 정점·UV Corner 동시 반전
- UV Seam 왕복 유지
- UV 없는 OBJ의 기존 `f v v v` 호환
- Partial UV Face 집합 보존
- 기존 600,000 Face 경계를 넘는 형상과 UV의 증분 처리
- 취소·실패·덮어쓰기 원자성
- Report Schema 2의 UV Count와 Coverage

### 13.4 Blender 소비자 검증

- Blender 5.2 기본 OBJ Importer로 정렬 결과 OBJ를 다시 Import한다.
- 생성된 Mesh에 UV Layer가 존재하는지 확인한다.
- Mesh UV Layer의 Face Corner 좌표가 원본 UV와 일치하는지 확인한다.
- UV Seam 개수와 Corner 연결이 유지되는지 확인한다.

### 13.5 전체 회귀

- Architecture 11개
- Coexistence 10개
- Modifier Core 155개
- Projection Paint Core 40개
- Blender 통합 등록·해제
- Align Mesh Import·Surface Picking·Marker Align·ICP·OBJ Export
- Workspace 전체 GUI와 Camera Marker Handoff
- 격리 설치본 Native Runtime 검사
- 실제 `user_default` 설치본 UV Fixture Import·Align·Export

## 14. 구현 순서

### 단계 1. 실패 재현과 계약 테스트

- UV Seam이 있는 삼각형 OBJ Fixture를 추가한다.
- 현재 Reader가 `vt`를 버리고 Export에 `vt`가 없음을 실패 테스트로 고정한다.
- UV 없는 기존 Fixture의 동작을 호환 기준으로 고정한다.

완료 조건: 현재 결함과 기존 호환 조건이 자동 테스트로 동시에 재현된다.

### 단계 2. Reader와 Runtime Model

- `vt`와 Face UV 인덱스 Parser를 구현한다.
- `ObjParseResult`, `TriangleMeshModelData`, Import Job에 UV 배열을 연결한다.
- UV 입력 오류와 원자성 회귀를 통과시킨다.

완료 조건: Import 직후 Runtime에서 원본 UV 값과 Corner 인덱스를 조회할 수 있다.

### 단계 3. 정렬 불변성

- Marker·ICP 전후 UV Fingerprint 테스트를 추가한다.
- Reflection Corner 순열 도우미를 정점·UV가 공유하도록 구성한다.
- Undo·Redo와 프로젝트 재로드를 검증한다.

완료 조건: 모든 정렬 상태 전이에서 UV 배열이 바뀌지 않는다.

### 단계 4. UV Export와 왕복 검증

- Export Snapshot, UV 기록 단계, Face Token과 Report Schema 2를 구현한다.
- Reader 왕복 검증에 UV 비교를 추가한다.
- Blender 기본 OBJ Importer 소비자 검증을 추가한다.

완료 조건: 정렬 결과 OBJ를 Blender가 UV Layer가 있는 Mesh로 읽는다.

### 단계 5. 패키징과 실제 설치

- `0.1.16`으로 버전을 올린다.
- `SmallGicp` Production ZIP을 빌드한다.
- 격리 설치본과 전체 Workspace GUI 회귀를 실행한다.
- 실제 `user_default`에 설치한다.
- 실제 설치본으로 UV Fixture Import·Align·Export를 실행한다.

완료 조건: Source, 격리 설치본과 실제 설치본에서 같은 UV Fingerprint를 얻는다.

## 15. 완료 기준

1. UV Seam이 있는 OBJ를 Import하면 원본 `vt` 값과 Face Corner UV 인덱스가 Runtime에 존재한다.
2. OBJ를 이동·회전·균일 스케일해도 UV 값과 UV 연결은 변경되지 않는다.
3. 반사 변환에서는 정점과 UV Corner 순서가 함께 반전된다.
4. 정렬 결과 OBJ에 `vt`와 `f v/vt`가 기록된다.
5. Blender 5.2로 결과 OBJ를 다시 Import하면 UV Layer가 생성되고 Corner UV가 원본과 일치한다.
6. UV가 없는 OBJ는 기존처럼 정상 처리된다.
7. `0.1.16` 완료 시점에는 MTL과 Texture가 범위 밖이었으나, `0.1.17` 후속 변경으로 diffuse `map_Kd` 텍스처 표시·복사를 별도 계약으로 지원한다.
8. 취소·실패 시 기존 Runtime과 기존 Export 결과가 보존된다.
9. 프로젝트 저장·재로드와 Align Undo·Redo 뒤에도 UV가 유지된다.
10. 전체 자동 회귀, 격리 설치본과 실제 설치본 검증을 통과한다.

## 16. 참고 기준

- Blender 4.5 LTS OBJ Manual은 OBJ Export의 `UV Coordinates`가 활성 UV Layer 좌표를 기록한다고 설명한다: https://docs.blender.org/manual/en/4.5/files/import_export/obj.html
- Blender 5.2 Python API의 `MeshUVLoopLayer.uv`는 UV를 Face Corner 좌표로 정의한다: https://docs.blender.org/api/current/bpy.types.MeshUVLoopLayer.html

이번 데이터 모델이 UV를 정점 하나당 하나로 저장하지 않고 Face Corner별 인덱스로 보존하는 이유도 이 계약과 같다.

## 17. 0.1.17 후속 확장: diffuse 텍스처 표시와 원본 복사

이 절은 3.2절의 기존 제외 범위 중 MTL·Texture·Viewport Texture Shading 항목을 `0.1.17`에서 확장한 현행 계약이다.

- Import는 OBJ의 `mtllib`, 면별 `usemtl`, MTL의 `Kd`와 `map_Kd`를 읽는다.
- UV와 실제 이미지 파일이 모두 있는 면만 텍스처 GPU 배치를 만들며, `COLMAP RGB`가 켜진 경우 원본 diffuse 이미지를 표시한다.
- 텍스처 면은 정점별 UV가 아니라 기존 face-corner UV 인덱스로 펼쳐 seam을 유지한다.
- `COLMAP RGB`가 꺼지거나 텍스처 조건이 부족한 면은 기존 Reference·Moving 역할 색상으로 표시한다.
- Export는 원본 이미지를 변환·재인코딩하지 않고 모델별 하위 폴더에 바이트 그대로 복사한다.
- 원본 MTL은 출력 경로와 충돌 가능성 때문에 그대로 복사하지 않는다. 재질 이름과 `Kd`를 보존한 새 MTL을 생성하고 `map_Kd`를 복사본 상대 경로로 기록한다.
- 출력 OBJ·MTL·복사 이미지는 같은 후보 폴더에서 검증되며, 원본 상태와 복사 SHA-256이 일치할 때만 최종 `aligned_obj`로 승격한다.
- 보고서는 Schema 3으로 올리고 모델별 재질 수, 고유 diffuse 텍스처 수, 복사 상대 경로와 SHA-256을 기록한다.

Normal Map, Roughness, Metallic, 투명도 Map과 비표준 MTL 확장은 계속 지원 범위 밖이다.
