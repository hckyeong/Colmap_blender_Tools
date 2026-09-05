# COLMAP Align OBJ 내장 원본 토폴로지·정렬 프록시 계획서

## 1. 문서 정보

- 대상 도구: `COLMAP Align Test Env`
- 대상 브랜치: `codex/align-test-env`
- 대상 기능: OBJ 가져오기, 표면 표시·피킹, CCCoreLib 정렬, OBJ 내보내기, 프로젝트 저장·복원
- 최초 작성일: 2026-09-04
- 최종 수정일: 2026-09-04
- 구현 상태: 핵심 구현과 격리 설치본 Background·GUI 자동 회귀 완료, 사용자 실데이터 최종 QC 대기
- 목적: OBJ의 원본 삼각형·Quad·N-gon 토폴로지를 `.blend` 내부에 보존하고, 파생 삼각형 프록시만 표시·피킹·정렬에 사용한다.

이 문서는 OBJ 원본 토폴로지와 프로젝트 영속성에 관한 최신 계획이다. 기존 `COLMAP_Align_OBJ_UV_보존_구조변경_계획서.md`와 충돌하는 경우 이 문서의 OBJ 저장·복원·Export 계약을 우선한다. 기존 UV·재질·텍스처 기능은 이번 변경으로 제거하지 않는다.

## 2. 확정 결정

1. 이번 변경에서는 OBJ만 `.blend`에 내장한다. COLMAP 원본 데이터의 저장·복원 구조는 변경하지 않는다.
2. Blender 5.2 기본 OBJ Importer가 만든 삼각화 전 Mesh를 원본 토폴로지의 단일 권위로 사용한다.
3. 원본 Mesh는 Object나 Collection에 연결하지 않은 비공개 `bpy.data.meshes` Data Block으로 유지한다.
4. 비연결 Mesh에는 `use_fake_user=True`와 소유권 UUID를 지정해 `.blend` 저장·재실행 후에도 보존한다.
5. `Mesh.calc_loop_triangles()`는 원본 Polygon을 바꾸지 않고 표시·피킹·CCCoreLib 입력용 삼각형 프록시를 만든다.
6. 정렬은 프록시로 계산하며 원본 정점·Polygon·UV를 직접 변경하지 않는다. 결과는 기존 공통 Sim(3) 행렬로만 보관한다.
7. Export는 내장 원본 Mesh를 읽고 정점 좌표에 행렬을 적용하면서 원본 Polygon Corner 순서로 기록한다.
8. 정상 Import가 끝난 뒤 외부 OBJ는 출처 정보일 뿐이다. 프로젝트 복원과 Export에서 다시 읽거나 존재 여부·SHA-256을 검사하지 않는다.
9. 원본 OBJ 수정 금지, 원본 OBJ 유실, 삼각형 대체 Export 안내는 새 내장 프로젝트에서 제거한다.
10. 레이어 아이콘, A/P 역할, 단일 형상 표시, 3점 마커 정렬, CCCoreLib 미리보기·승인·취소 UX는 유지한다.

Blender 5.2에서 비연결 Mesh 한 개에 Quad 한 면을 저장하고 `use_fake_user=True`를 지정한 뒤 `.blend`를 다시 열었을 때 정점 4개, Polygon 1개, Corner 4개와 비연결 상태가 모두 유지되는 것을 선행 확인했다.

### 2.1 구현 결과

- Blender 5.2 Native OBJ Import를 `forward_axis=Y`, `up_axis=Z`와 Identity Object Matrix 검증으로 고정했다.
- 독립 OBJ와 COLMAP 연결 OBJ 모두 UUID가 권위인 비연결 Fake User Mesh로 확정하고, `calc_loop_triangles()` 결과만 Runtime 프록시로 사용한다.
- 프로젝트 schema v8은 내장 UUID·schema·이름 hint·revision을 저장하며 새 프로젝트 복원은 외부 OBJ 경로와 SHA-256을 읽지 않는다.
- 구버전 프로젝트는 가져오기 전후 SHA-256을 모두 확인한 뒤 한 번 내장 형식으로 이전한다.
- 레이어 삭제·복원 rollback·새 프로젝트는 소유 Mesh와 Native Import가 만든 zero-user Material·Image만 정리한다.
- Data Layers 행에는 UUID나 Data Block 이름 없이 `OBJ 내장됨`, 원본 Polygon 수, 프록시 Triangle 수를 표시한다.
- Blender 5.2 Background 회귀에서 혼합 Triangle·Quad·오목 N-gon, UV·재질, `.blend` 재열기, 외부 OBJ 삭제 후 복원, companion 저장·복원, custom Undo·Redo, marker picking·정렬, CCCoreLib 형상 입력을 확인했다.

```text
외부 OBJ
   │ 최초 Import에서만 읽음
   ▼
Blender 기본 OBJ Importer
   │
   ├─ 내장 원본 Mesh ──────────────► .blend 저장·복원
   │   Polygon / Loop / UV / Material
   │          │
   │          └────────────────────► 원본 토폴로지 OBJ Export
   │                                  정점에 정렬 행렬만 적용
   │
   └─ calc_loop_triangles()
              │
              ├─ Viewport Surface
              ├─ Surface Marker Picking
              └─ CCCoreLib Surface Input
```

## 3. 변경 전 구조와 원인

변경 전 `TriangleMeshModelData`는 정점과 `(T, 3)` 삼각면 배열을 Runtime 권위로 사용했다. OBJ Reader도 Face Corner가 정확히 세 개인 입력만 허용했다. 프로젝트 파일에는 대형 메시를 넣지 않고 Source Path와 Fingerprint만 저장해 Blender 재실행 후 외부 OBJ를 다시 읽어 Runtime을 복원했다.

이 구조에서는 다음 문제가 생긴다.

| 영역 | 현재 동작 | 문제 |
| --- | --- | --- |
| OBJ Import | 삼각면만 읽음 | Quad·N-gon을 원본 면으로 보존할 수 없음 |
| Runtime | 삼각형 배열만 유지 | 두 삼각형이 원래 Quad였는지 판별할 수 없음 |
| 임시 Blender Mesh | 배열 추출 후 제거 | Blender가 보유했던 원본 Polygon 정보가 사라짐 |
| 프로젝트 복원 | 외부 OBJ 재파싱 | 원본 파일의 존재·동일성에 의존 |
| OBJ Export | Runtime 삼각면 기록 | 결과가 항상 삼각형으로 출력됨 |

삼각형 캐시만으로는 원래 Polygon 경계를 일반적으로 복원할 수 없다. 이번 변경은 삼각형을 다시 N-gon으로 추론하지 않고, 삼각화 전 Blender Mesh를 그대로 보존해 문제를 없앤다.

## 4. 목표와 범위

### 4.1 필수 목표

- Corner가 3개 이상인 OBJ Polygon을 Blender 기본 Importer로 가져온다.
- 삼각형·Quad·N-gon과 Polygon별 Corner 순서를 내장 Mesh에 보존한다.
- UV Seam과 면별 Material 인덱스를 Blender Mesh 표현 범위에서 보존한다.
- 원본 Mesh는 Scene, View Layer, Collection과 일반 Outliner 조작 대상에 연결하지 않는다.
- 표시·피킹·CCCoreLib에는 유효한 삼각형 프록시만 제공한다.
- 정렬 결과는 원본 Mesh에 Bake하지 않고 기존 공통 행렬로 유지한다.
- 외부 OBJ를 삭제하거나 수정한 뒤에도 저장된 `.blend`에서 원본 면 구조로 Export할 수 있어야 한다.
- 프로젝트 저장·재실행 뒤 내장 Mesh에서 Runtime 프록시를 복원한다.
- 인위적인 Polygon 개수 제한을 추가하지 않는다.
- Import·복원·Export 실패 시 기존 레이어와 기존 출력 결과를 유지한다.

### 4.2 이번 범위에서 제외

- COLMAP cameras, images, points3D의 `.blend` 내장
- 원본 OBJ 텍스트 바이트, 주석, 줄 순서, 공백의 동일 복원
- OBJ Line, Point, Curve 요소 지원
- Quad Remesh 또는 토폴로지 편집
- 삼각형에서 Quad·N-gon을 추론하는 역삼각화
- CCCoreLib가 Quad·N-gon을 직접 처리하도록 변경
- MTL 파일과 Texture 이미지 자체를 `.blend`에 Pack하는 기능
- 정점 수나 면 연결을 바꾸는 비강체 정렬

외부 MTL·Texture를 사용하는 기존 기능은 별도 계약을 유지한다. 이 변경에서 제거하는 외부 의존성은 OBJ 기하와 토폴로지에 관한 의존성이다.

## 5. 데이터 소유권

### 5.1 내장 원본 Mesh

원본 Geometry의 권위는 `bpy.data.meshes`에 보관된 비연결 Mesh다. 별도의 원본 Polygon NumPy 사본을 장기 보관하지 않는다.

Mesh에는 다음 소유권 정보를 Custom Property로 기록한다.

```text
owner             = "COLMAP_ALIGN_EMBEDDED_OBJ"
schema_version    = 1
asset_uuid        = 안정적인 UUID
model_id          = Align Data Layer의 모델 ID
source_name       = 최초 파일명
source_path       = 출처 표시용 절대 경로
geometry_revision = 내부 관리 Revision
```

- `asset_uuid`가 영속 식별자의 권위이며 Data Block 이름은 검색용 힌트로만 사용한다.
- Data Block 이름에는 충돌을 피하기 위한 전용 접두사와 UUID를 사용한다.
- `use_fake_user=True`를 항상 유지한다.
- Object를 남기지 않으므로 사용자가 뷰포트에서 선택·변형·렌더링할 수 없다.
- 원본 좌표는 OBJ Source 좌표계로 유지한다. RC·Custom 축 변환은 기존 Display Transform에서만 적용한다.
- 원본 Mesh 정점에 정렬 결과를 직접 Bake하지 않는다.

### 5.2 Runtime 정렬 프록시

기존 렌더러·피킹·CCCoreLib 연결의 영향 범위를 제한하기 위해 `TriangleMeshModelData`는 정렬 프록시 역할로 유지한다.

```python
vertices_float64: np.ndarray                # 내장 Mesh에서 추출한 원본 정점 좌표
triangles_uint32: np.ndarray                # loop_triangles의 삼각형 정점 인덱스
triangle_polygon_indices_uint32: np.ndarray # 원본 Polygon 추적
triangle_loop_indices_uint32: np.ndarray    # 원본 Corner 추적
embedded_asset_uuid: str                    # 내장 원본 Mesh 연결
geometry_revision: int                      # Snapshot stale 판정
```

- 장기 보존하는 Polygon·Loop·UV·Material의 권위는 내장 Mesh 하나뿐이다.
- 프록시는 원본 Polygon 배열과 UV 배열을 복제하지 않는다.
- 기존 `float64` 정점 배열은 현재 렌더러와 Solver 호환을 위해 첫 구현에서 유지한다.
- Python Triangle 객체 목록을 장기 보관하지 않고 NumPy 연속 배열로 즉시 확정한다.
- CC Worker용 좌표·면·표본과 공간 탐색 구조는 작업 중에만 생성하고 완료·취소·실패 시 해제한다.

### 5.3 Source Metadata

- 최초 OBJ 경로와 파일명은 출처 표시, 사용자가 명시적으로 재Import할 때, 동일 경로 덮어쓰기 방지용 문자열 비교에만 사용한다.
- 정상 내장 프로젝트의 복원·Export는 Source Path를 `stat`, `open`, 재파싱 또는 SHA-256 계산하지 않는다.
- 원본 OBJ가 사라지거나 바뀌어도 내장 Geometry 상태는 `STALE`로 바뀌지 않는다.
- 내부 일관성은 `asset_uuid`, Schema, 소유권, Model ID, Geometry Revision과 Mesh의 기본 구조 검증으로 판정한다.

## 6. OBJ Import 트랜잭션

### 6.1 Blender 기본 Importer 호출

- 전용 어댑터에서 `bpy.ops.wm.obj_import`를 호출한다.
- 호출 전 Scene 선택, 활성 Object, 모드와 활성 Collection을 기록한다.
- Import 전후 Data Block 차이로 이번 호출이 생성한 Object·Mesh·Material·Image만 식별한다.
- `use_split_objects=False`, `use_split_groups=False`를 사용해 한 OBJ를 한 Align Asset으로 정규화한다.
- 생성 Mesh가 여러 개인 예외 입력은 모든 Object Matrix를 반영해 한 Asset으로 결합하되 Polygon Corner와 재질 연결을 보존한다.
- Blender Importer 축 옵션과 생성 Object Matrix는 비대칭 Fixture로 검증해 원본 OBJ 좌표를 내장 Mesh에 저장한다.
- 기존 RC·Custom Display Axis를 Import 좌표에 중복 적용하지 않는다.

### 6.2 Staging과 Commit

Import는 다음 순서를 원자적으로 수행한다.

1. 기존 Blender 상태와 기존 Align Layer Snapshot을 기록한다.
2. 전용 임시 Collection에서 OBJ를 가져온다.
3. 생성 Mesh의 정점·Polygon·Loop·UV·Material 구조를 검증한다.
4. 원본 Polygon을 변경하지 않고 `calc_loop_triangles()`를 계산한다.
5. Runtime 프록시와 GPU 준비 후보를 만든다.
6. 모든 검증이 성공하면 Mesh에 소유권·UUID·Schema·Fake User를 지정한다.
7. 생성 Object와 임시 Collection만 제거하고 원본 Mesh Data Block은 비연결 상태로 유지한다.
8. 후보 Runtime과 내장 Mesh 참조를 Data Layer에 함께 Commit한다.
9. 기존 선택·활성 Object·모드·Collection을 복원한다.

실패하거나 취소하면 새로 만든 Object·Mesh와 이 Import만 소유한 부속 Data Block을 제거한다. 사용자가 이미 보유하던 Data Block과 기존 Align Layer는 변경하지 않는다.

### 6.3 Import 진행 표시

Blender 기본 OBJ Importer는 동기 연산이므로 호출 내부의 세부 퍼센트와 취소를 현재 Timer가 직접 제어할 수 없다.

```text
파일 확인 → Blender 가져오기 → 원본 검증 → 삼각형 프록시 생성 → GPU 준비 → 내장 등록
```

`Blender 가져오기` 단계는 확정되지 않은 퍼센트를 표시하지 않고 처리 중 상태만 표시한다. 대형 OBJ에서 UI 정지 시간이 허용 범위를 넘으면 원본·프록시 데이터 계약은 유지한 채 별도 Blender Worker Process를 검토한다.

## 7. 삼각형 캐시와 정렬 프록시

### 7.1 생성 계약

- `Mesh.calc_loop_triangles()`만 호출하고 원본 Mesh에 Triangulate Modifier나 파괴적 BMesh 연산을 적용하지 않는다.
- 오목한 N-gon은 Blender가 계산한 Tessellation을 사용한다.
- 각 삼각형에 원본 Polygon과 Loop 인덱스를 연결한다.
- 프록시 정점 좌표에는 기존 Display Axis와 Align 행렬을 계산 시점에 합성한다.
- 면적 0, 반복 Corner, 범위 밖 인덱스 또는 비유한 정점은 모델명과 Polygon 번호를 포함해 Import를 실패시킨다.

### 7.2 사용처

| 사용처 | 입력 |
| --- | --- |
| Viewport Surface | 프록시 정점 + 삼각면 |
| 메시 마커 피킹 | 프록시 정점 + 삼각면 |
| 마커 대표 삼각형 | 프록시 삼각면 |
| CCCoreLib 기준 표면 | 프록시 정점 + 삼각면 |
| CCCoreLib 이동 표면 | 프록시 기반 면적 표본 |
| OBJ Export | 내장 원본 Mesh Polygon·Loop |

레이어의 점군·표면 아이콘으로 선택한 입력 종류와 표면–표면, 표면–점군, 점군–표면, 점군–점군 자동 선택 규칙은 변경하지 않는다.

## 8. 정렬 행렬 계약

- 3점 마커 정렬과 CCCoreLib 결과는 기존처럼 P 레이어의 공통 Sim(3) 행렬에 적용한다.
- ±10% 균일 스케일 제한과 기존 Preview·승인·취소·Undo·Redo 흐름을 유지한다.
- 내장 원본 Mesh의 정점, Polygon, Loop, UV와 Material 연결은 정렬 전후 동일해야 한다.
- 표시 축과 정렬 행렬을 내장 정점에 미리 Bake하지 않는다.
- Export 좌표는 `유효 행렬 × 내장 원본 정점`으로 계산한다.
- Preview 이후 `asset_uuid` 또는 `geometry_revision`이 바뀌면 승인을 거부한다.

## 9. OBJ Export

### 9.1 Snapshot

Export 시작 시 내장 Mesh에서 다음 불변 Snapshot을 만든다.

- 정점 좌표
- Polygon별 Loop 시작·개수
- Loop별 정점 인덱스
- UV Corner 좌표
- Polygon별 Material 인덱스
- 내장 Asset UUID와 Geometry Revision
- Display Axis와 Alignment 행렬

Snapshot 이후에는 내장 Mesh 자체를 수정하지 않는다. Writer는 Snapshot을 순차 처리하며 후보 폴더 검증이 끝난 뒤에만 최종 `aligned_obj`로 승격한다.

### 9.2 면 기록

- 정점에는 유효 Display Axis와 Alignment 합성 행렬을 적용한다.
- UV 좌표는 변경하지 않는다.
- 각 Polygon의 Loop 범위를 OBJ `f` 한 줄로 기록한다.
- 삼각형은 삼각형, Quad는 Quad, N-gon은 같은 Corner 수의 N-gon으로 출력한다.
- UV가 있으면 같은 Corner의 정점·UV 연결을 함께 기록한다.
- 기존 면별 Material과 MTL·Texture Export 계약을 유지한다.

유효 행렬 선형부의 행렬식이 음수이면 Polygon의 첫 Corner를 고정하고 나머지 Corner 순서를 반전한다. 정점 Corner와 UV Corner에 같은 순열을 적용한다.

### 9.3 외부 원본 의존성 제거

새 내장 프로젝트의 Export에서는 다음 동작을 하지 않는다.

- 외부 OBJ 존재 여부 확인
- 외부 OBJ 수정 시각·크기·SHA-256 확인
- 외부 OBJ 재파싱
- 원본 유실 경고
- 원본 유실 시 삼각형 프록시 대체 Export

내장 Mesh가 누락되거나 Schema·소유권·구조 검증에 실패하면 내부 프로젝트 데이터 손상으로 보고 후보 Export를 중단한다. N-gon을 조용히 삼각형으로 바꾸는 대체 동작은 두지 않는다.

외부 Texture를 복사하는 기존 기능이 활성화된 경우에는 Texture 파일에 관한 기존 존재·동일성 검사를 유지한다. 이는 내장 OBJ Geometry 검사와 분리한다.

### 9.4 왕복 검증

후보 OBJ를 새 가변 Corner Reader 또는 Blender 기본 Importer로 다시 읽어 다음 항목을 확인한다.

- 정점 수와 변환된 좌표
- Polygon 수와 Polygon별 Corner 수
- Polygon별 정점 연결과 Winding
- UV Corner 좌표와 Seam
- 면별 Material 연결
- 출력 Polygon Tessellation과 정렬 프록시의 기하학적 일치

검증 실패 시 후보만 제거하고 기존 출력 결과는 유지한다.

## 10. 프로젝트 저장과 복원

### 10.1 새 저장 Schema

프로젝트 저장 레코드에 다음 필드를 추가한다.

```text
embedded_obj_asset_uuid
embedded_obj_schema_version
embedded_obj_data_name_hint
embedded_obj_geometry_revision
```

Source Path와 원래 파일명은 출처 정보로 계속 저장하지만 정상 복원의 입력으로 사용하지 않는다. 대형 원본 Geometry를 JSON이나 Text Data Block에 복제하지 않는다. 실제 Mesh Data Block 자체가 `.blend`에 저장된다.

### 10.2 파일 재실행 복원

1. 저장 Payload를 읽는다.
2. `bpy.data.meshes`에서 소유권과 `asset_uuid`가 일치하는 비연결 Mesh를 찾는다.
3. Schema, Model ID, Fake User, 정점·Polygon·Loop 구조를 검증한다.
4. 내장 Mesh에서 삼각형 프록시와 GPU 상태를 다시 만든다.
5. 저장된 Display Axis, Alignment, Marker와 역할 상태를 연결한다.
6. 전체 Staging이 성공한 뒤 Runtime을 교체한다.

외부 OBJ 경로가 없거나 파일 내용이 달라도 복원 결과에는 영향을 주지 않는다.

### 10.3 기존 프로젝트 호환

- 내장 Asset UUID가 없는 기존 프로젝트는 기존 Source Path 또는 사용자가 Relink한 OBJ를 한 번 읽는다.
- 성공적으로 복원하면 원본 Mesh를 즉시 내장하고 새 Schema로 저장 가능한 상태로 전환한다.
- 기존 외부 OBJ도 찾을 수 없는 프로젝트는 원본 N-gon을 만들 수 없으므로 기존 Relink 흐름을 유지한다.
- 한 번 내장 전환이 완료된 프로젝트에는 이후 외부 OBJ Relink를 요구하지 않는다.
- 구버전 삼각형 프로젝트는 소스에 삼각형만 있어도 같은 내장 경로로 이전한다.

## 11. Data Block 생명주기

| 이벤트 | 내장 OBJ 처리 |
| --- | --- |
| Import 성공 | Staged Mesh를 소유권과 Fake User가 있는 비연결 Mesh로 Commit |
| Import 실패·취소 | 이번 Import가 만든 Data Block만 제거 |
| Layer 삭제 | 해당 Layer가 소유한 Asset UUID의 Mesh만 제거 |
| 새 Align 프로젝트 | 이전 프로젝트 소유 Mesh를 정리한 뒤 새 상태 시작 |
| `.blend` 저장 | Fake User에 의해 Mesh 저장 |
| `.blend` 재실행 | UUID로 Mesh를 찾아 Runtime 프록시 재생성 |
| Add-on 비활성화 | Runtime 참조만 해제하고 저장된 내장 Mesh는 유지 |
| Add-on 재활성화 | 저장 Payload와 UUID로 다시 연결 |

일반 Orphan 정리에서 제거되지 않도록 Fake User를 사용한다. 소유권이 없는 비연결 Mesh나 다른 도구의 Mesh는 정리하지 않는다. 같은 UUID의 Mesh가 둘 이상이면 임의 선택하지 않고 복원 오류로 처리한다.

## 12. UI와 사용자 흐름

레이어 구조와 버튼 위치는 유지한다.

1. 사용자가 기존 `OBJ 메시 추가` 또는 COLMAP 형제 OBJ 자동 연결을 실행한다.
2. Import 완료 후 레이어에는 기존처럼 표면 아이콘 하나가 표시된다.
3. 상태 문구에 원본 Polygon 수와 정렬 프록시 Triangle 수를 구분해 표시한다.
4. 마커 3점 정렬과 CCCoreLib 정밀 정렬을 기존 UI로 실행한다.
5. Export는 내장 원본 Mesh에서 Quad·N-gon을 보존해 출력한다.

```text
정점 80,000개 · 원본 면 79,200개 · 정렬용 삼각면 158,400개 · OBJ 내장됨
```

- `원본 OBJ를 수정하지 마십시오` 안내를 표시하지 않는다.
- `원본 OBJ가 없습니다`와 삼각형 대체 Export 안내를 표시하지 않는다.
- Source Path를 보여주는 기존 위치가 있다면 `최초 가져온 경로`로 명시한다.
- 내장 Data Block 이름이나 UUID는 일반 사용자 UI에 노출하지 않는다.
- 내장 Mesh가 손상된 경우에만 `내장 OBJ 데이터가 누락되었거나 손상되었습니다`라고 표시한다.

## 13. 성능과 메모리

### 13.1 메모리 원칙

- Blender Importer가 만든 원본 Mesh를 제거하지 않고 그대로 권위 데이터로 전환한다.
- 원본 Polygon·Loop·UV를 별도의 장기 NumPy 배열로 다시 복제하지 않는다.
- 비파괴 삼각형 인덱스 캐시의 증가량은 대략 `Triangle 수 × 12~16바이트`를 기준으로 측정한다.
- 예를 들어 1,000만 Triangle이면 순수 삼각형 캐시는 약 120~160MB가 예상된다.
- 기존 Runtime 호환용 정점 배열과 GPU Buffer의 추가 사용량은 삼각형 캐시와 분리해 보고한다.
- CC 입력·표본·공간 탐색 구조는 정렬 작업이 끝나면 즉시 해제한다.
- 대형 Python List나 Triangle별 Python 객체를 영속 상태로 남기지 않는다.

### 13.2 시간 특성

- 최초 Import 비용은 Blender 기본 Importer를 사용하는 두 방식에서 동일하다.
- 내장 방식은 반복 Export와 프로젝트 Runtime 복원에서 외부 OBJ 텍스트 재파싱을 제거한다.
- `.blend` 파일 크기와 저장·열기 시간은 내장 Mesh 크기만큼 증가한다.
- 대형 OBJ의 최초 Import, `.blend` 저장, `.blend` 재실행, 첫 프록시 생성, 반복 Export를 각각 측정한다.
- 파일 크기만으로 메모리를 추정하지 않고 정점 수, Polygon 수, Loop 수와 Tessellation Triangle 수를 함께 기록한다.

### 13.3 측정 기준

최소 다음 Fixture를 비교한다.

- 기존 600,001 Triangle OBJ
- 30만 Quad OBJ: 원본 30만 Polygon, 프록시 약 60만 Triangle
- 오목한 N-gon 혼합 OBJ
- 실제 Quad Remesher 출력 대형 OBJ

각 Fixture에서 Import 전, Native Import 직후, 프록시 생성 후, CC 정렬 중 최대, CC 종료 후, `.blend` 재실행 후와 Export 중 최대 메모리를 기록한다.

## 14. 오류와 원자성

다음 상황에서는 Import 후보와 이 후보가 만든 Data Block만 제거하고 기존 상태를 유지한다.

- Blender 기본 Importer 실패
- 생성 Mesh 식별 실패 또는 예상하지 않은 빈 Geometry
- 비유한 정점, 잘못된 Polygon·Loop·UV·Material 연결
- Tessellation 실패 또는 퇴화 Polygon
- Runtime 프록시·GPU 준비 실패
- 사용자 취소

다음 상황에서는 복원 Staging을 폐기하고 기존 Runtime을 Commit하지 않는다.

- 내장 Asset UUID 누락·중복
- 지원하지 않는 내장 Schema
- 저장 Model ID와 Mesh 소유권 불일치
- 손상된 정점·Polygon·Loop 구조
- 프록시 재생성 실패

다음 상황에서는 Export 후보만 폐기하고 기존 출력 결과를 유지한다.

- Export Snapshot 구조 오류
- 정점 기록 중 취소
- Geometry Revision 변경
- Polygon·UV·Material 왕복 검증 실패

## 15. 수정 대상

| 경로 | 계획된 변경 |
| --- | --- |
| `tools/align/mesh_io/embedded_obj_store.py` | 내장 Mesh 소유권, UUID 검색·검증·Commit·정리 경계 추가 |
| `tools/align/mesh_io/obj_native_import.py` | Blender 기본 OBJ Importer Staging과 원본 좌표·Data Block 정리 어댑터 추가 |
| `tools/align/mesh_io/types.py` | 프록시와 내장 Asset 참조·Revision 계약 추가 |
| `tools/align/mesh_io/obj_reader.py` | Export 왕복 검증용 가변 Corner Polygon Reader로 확장 |
| `tools/align/mesh_importer.py` | Native Import, 프록시 생성과 원자 Commit 연결 |
| `tools/align/model_repository.py` | Layer별 내장 Asset UUID 소유권과 삭제·Clear 처리 추가 |
| `tools/align/viewport/renderer.py` | 내장 원본이 아닌 삼각형 프록시만 사용하도록 계약 고정 |
| `tools/align/viewport/picking.py` | 프록시 Triangle과 원본 Polygon·Loop 매핑 검증 |
| `tools/align/alignment/icp_geometry.py` | CCCoreLib 입력을 프록시에서만 생성하고 작업 후 해제 |
| `tools/align/mesh_io/obj_writer.py` | 내장 Mesh Snapshot, 가변 Corner 출력, 반사 Winding과 왕복 검증 |
| `tools/align/operators/export_obj.py` | 외부 OBJ 검사 제거, 내장 Snapshot과 원자 Export 연결 |
| `tools/align/operators/import_mesh.py` | 내장 완료 상태와 원본·프록시 면 수 표시 |
| `tools/align/operators/model_group.py` | Layer 삭제 시 소유 내장 Mesh 정리 연결 |
| `tools/align/project_persistence/types.py` | Embedded Asset 영속 필드 추가 |
| `tools/align/project_persistence/codec.py` | 새 Schema 직렬화·검증과 구버전 읽기 추가 |
| `tools/align/project_persistence/runtime.py` | UUID 기반 복원과 기존 Source 기반 일회성 이전 구현 |
| `tools/align/session.py` | 비활성화·새 프로젝트·파일 전환 시 Data Block 수명 구분 |
| `tools/align/ui/panels.py` | `OBJ 내장됨`과 내부 손상 상태 표시 |
| `tests/align/blender` | 내장 수명, N-gon, 정렬, Export, 복원과 실패 원자성 회귀 추가 |
| `test_env/verify_blender.py` | 설치본 실제 `.blend` 저장·재실행 검증 추가 |
| `docs/TESTING.md` | 새 회귀 명령과 성능 측정 기준 추가 |

COLMAP Reader·저장 Payload와 다른 도구의 Runtime은 수정하지 않는다. Texture 파일 Pack 기능도 추가하지 않는다.

## 16. 구현 순서

### 단계 1. 현행 동작과 Blender 저장 계약 고정

- 기존 삼각형 OBJ Import·표시·마커·CCCoreLib·Export 회귀를 고정한다.
- 비연결 Mesh, Fake User, Quad·N-gon, UV, Material Index가 `.blend` 저장·재실행 후 유지되는지 자동 테스트로 고정한다.
- 비대칭 좌표 Fixture로 Blender Importer와 기존 Display Axis의 관계를 고정한다.

완료 조건: 장면 Object 없이 원본 Polygon Mesh가 저장되며 좌표축이 중복 변환되지 않는다.

### 단계 2. 내장 OBJ 저장소와 원자 Import

- `embedded_obj_store`의 소유권·UUID·Schema·검색·정리 API를 구현한다.
- Blender 기본 Importer를 Staging으로 호출하고 성공한 Mesh만 내장 상태로 Commit한다.
- 실패·취소·Layer 삭제에서 자기 소유 Data Block만 정리한다.

완료 조건: OBJ Import 후 Scene Object는 늘지 않고 소유권이 있는 비연결 Mesh 하나가 남는다.

### 단계 3. 삼각형 프록시와 기존 정렬 UX 연결

- `calc_loop_triangles()` 결과로 Runtime 프록시를 생성한다.
- 표시·피킹·마커·네 가지 CCCoreLib 입력 조합을 프록시에 연결한다.
- 정렬 승인·취소·Undo·Redo가 내장 Mesh를 변경하지 않는지 검증한다.

완료 조건: Quad 한 면이 원본 Polygon 한 면과 정렬 프록시 Triangle 두 면으로 표현되고 기존 UX로 정렬된다.

### 단계 4. 내장 원본 기반 Polygon Export

- 내장 Mesh에서 불변 Export Snapshot을 만든다.
- 가변 Corner Polygon, UV, Material과 반사 Winding을 기록한다.
- 외부 OBJ 존재·Fingerprint·재파싱 및 삼각형 대체 경로를 제거한다.
- 후보 출력 왕복 검증과 원자 승격을 유지한다.

완료 조건: 외부 OBJ를 삭제한 뒤에도 Quad·N-gon 결과를 같은 면 차수로 Export한다.

### 단계 5. 프로젝트 저장·복원과 기존 프로젝트 이전

- 새 Embedded Asset Schema를 저장한다.
- `.blend` 재실행 시 UUID로 내장 Mesh를 찾아 프록시를 재생성한다.
- 기존 Source 기반 프로젝트를 한 번 Import한 뒤 내장 구조로 이전한다.
- Add-on 비활성화와 재활성화에서 내장 Mesh가 유지되는지 검사한다.

완료 조건: 외부 OBJ 없이 `.blend`만 이동해도 프로젝트 복원과 Export가 성공한다.

### 단계 6. 성능·메모리와 설치본 검증

- 실제 대형 Triangle·Quad·N-gon OBJ의 단계별 시간과 최대 메모리를 측정한다.
- CC 종료 후 작업용 Buffer가 누적되지 않는지 확인한다.
- 격리된 테스트 Blender에 빌드·설치하고 실제 Quad Remesher OBJ로 전체 흐름을 검증한다.

완료 조건: 반복 Import·정렬·저장·재실행·Export에서 Data Block과 메모리가 누적되지 않고 기존 회귀가 통과한다.

## 17. 필수 테스트

### 17.1 Import와 내장

- Triangle, Quad, 볼록·오목 N-gon 혼합
- UV 없음, UV Seam, 복수 Material
- 양수·음수 OBJ 인덱스
- 복수 Object·Group 입력
- Import 후 Scene·Collection에 생성 Object가 남지 않음
- 비연결 Mesh의 Fake User와 소유권 UUID 확인
- 실패·취소 반복 후 Mesh·Material·Image 누적 없음

### 17.2 정렬

- Quad를 구성하는 서로 다른 프록시 Triangle에서 마커 피킹
- 3점 정렬 후 원본 Polygon·UV·Material 불변
- 표면–표면, 표면–점군, 점군–표면, 점군–점군 입력 선택
- CCCoreLib ±10% 스케일 보정 후 원본 토폴로지 불변
- Preview 취소·승인·Undo·Redo 후 내장 Mesh 불변

### 17.3 저장과 복원

- 저장 후 Blender 재실행에서 Object 0개, 내장 Mesh 1개 유지
- 외부 OBJ를 이동·수정·삭제한 상태에서 복원 성공
- Data Block 이름 변경 후 UUID 복원 성공
- UUID 중복·소유권 오류·Schema 오류 시 원자 실패
- Add-on 비활성화·재활성화 후 복원
- Layer 삭제 시 해당 UUID Mesh만 제거
- 구버전 Source 기반 프로젝트의 일회성 내장 이전

### 17.4 Export

- 외부 OBJ가 없는 상태에서 Triangle→Triangle 출력
- 외부 OBJ가 없는 상태에서 Quad→Quad 출력
- 외부 OBJ가 없는 상태에서 N-gon→동일 Corner 수 N-gon 출력
- 혼합 Polygon 순서와 차수 보존
- 정렬 Translation·Rotation·Uniform Scale 좌표 검증
- Reflection에서 Polygon·UV Corner 동시 반전
- UV Seam과 면별 Material 연결 보존
- 실패·취소 시 기존 출력 유지
- Export 경로에서 외부 OBJ `open`, `stat`, SHA-256이 호출되지 않음

### 17.5 성능과 메모리

- Triangle 수별 캐시 증가량 기록
- Native Import 종료 후 불필요한 임시 Object·Mesh 제거 확인
- CC 정렬 종료 후 작업용 좌표·표본·공간 Index 해제 확인
- 반복 Export에서 외부 OBJ 재파싱이 발생하지 않음
- `.blend` 저장·재실행 시간과 파일 크기 기록

## 18. 완료 기준

1. OBJ의 Triangle·Quad·N-gon이 내장 원본 Mesh에 같은 Polygon 차수로 저장된다.
2. 내장 원본 Mesh는 Scene에 노출되지 않으며 `.blend` 저장·재실행 후 유지된다.
3. 표시·피킹·3점 정렬·CCCoreLib는 삼각형 프록시만 사용한다.
4. 정렬 과정은 내장 원본 Mesh를 변경하지 않고 공통 행렬만 갱신한다.
5. 외부 OBJ를 삭제하거나 수정해도 새 프로젝트 복원과 Export가 성공한다.
6. Quad와 N-gon은 정렬 결과 OBJ에서도 원래 Polygon 차수를 유지한다.
7. 정상 경로에서 외부 OBJ 재읽기·존재 검사·SHA-256·수정 금지 안내가 없다.
8. 기존 삼각형 OBJ, 레이어 UI, 마커, CCCoreLib Preview·승인·취소·Undo·Redo 동작이 유지된다.
9. Import·복원·Export 실패 시 기존 Runtime과 기존 출력이 보존된다.
10. Layer 삭제·새 프로젝트·실패 반복에서 소유 Data Block과 작업 Buffer가 누적되지 않는다.
11. 기존 Source 기반 프로젝트는 원본을 한 번 읽을 수 있을 때 내장 방식으로 이전된다.
12. COLMAP 저장·복원과 다른 도구의 데이터 소유권은 변경되지 않는다.

## 19. 구현 전 측정 항목

- 실제 Quad Remesher OBJ 한 개를 토폴로지·성능 기준 Fixture로 지정한다.
- Blender 기본 Importer가 그 파일에서 생성하는 Mesh·Object·Material·Image 수를 기록한다.
- 원본 OBJ 좌표와 내장 Mesh 좌표의 축·Object Matrix 관계를 실측한다.
- 정점·Polygon·Loop·Triangle 수와 각 단계의 메모리를 기록한다.
- `.blend` 저장 압축 설정에 따른 저장·재실행 시간은 측정하되 첫 구현의 데이터 계약을 설정 옵션에 의존시키지 않는다.

성능 측정 결과가 허용 범위를 넘더라도 원본 N-gon 권위를 외부 파일로 되돌리는 임시 우회는 넣지 않는다. Import 실행 방식이나 Runtime 프록시의 수명만 같은 데이터 계약 안에서 개선한다.

## 20. 구현 검증 결과

- 검증일: 2026-09-04
- 검증 브랜치: `codex/align-test-env`
- Blender: 5.2.0 LTS
- 격리 설치본 `verify --gui`: 설치·재등록, 소스 회귀, 실제 작업공간 GUI를 포함한 37개 항목 통과
- 외부 OBJ 삭제 후 ACTIVE Text 저장, `.blend` 재실행, UUID 복원과 Quad·N-gon Export E2E 통과
- Python AST 221개와 `git diff --check` 통과
- 자동 검증 보고서: `E:\DEV\DEV_RND\colmap_align_test_env\outputs\verification-report.json`
- 사용자 실데이터 Quad Remesher OBJ의 처리 시간·최대 메모리·RC 재검증은 최종 QC로 남긴다.
