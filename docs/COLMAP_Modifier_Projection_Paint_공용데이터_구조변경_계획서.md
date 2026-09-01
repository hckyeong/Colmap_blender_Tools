# COLMAP Modifier·Projection Painting 공용 데이터 구조변경 계획서

## 1. 문서 정보

- 작성일: 2026-08-29
- 대상 제품: `COLMAP Tools` Blender Extension
- 대상 Blender: 5.2 LTS 이상
- 기준 버전: `0.1.20`
- 목표 버전 후보: `0.2.0`
- 대상 도구: `COLMAP Modifier`, `Projection Painting`
- 독립 유지 도구: `COLMAP Align`
- 문서 상태: 구현 전 구조변경 계획

## 2. 목적

현재 `COLMAP Modifier`와 `Projection Painting`은 같은 COLMAP Camera, Image, Point3D를 각각 읽고 별도의 Blender Collection, Point Mesh와 Camera Object를 생성한다.

이 계획은 중복 Import와 중복 Scene 데이터를 제거하고 다음 구조로 변경하는 것을 목적으로 한다.

1. COLMAP Import UI는 Modifier에만 둔다.
2. Import된 COLMAP 데이터는 Modifier가 아니라 상위 공용 Dataset 계층이 소유한다.
3. Modifier와 Projection Painting은 같은 Point, Camera와 이미지 경로를 사용한다.
4. Modifier에서 확정된 편집은 Projection Painting 재진입 시 자동 반영한다.
5. Workspace, Marker, Undo, Paint Layer와 Stencil 같은 도구별 실행 상태는 계속 분리한다.
6. Align은 공용 Dataset에 연결하지 않고 현재와 같이 완전히 독립적으로 유지한다.

## 3. 쉬운 결론

```text
COLMAP Tools
├─ COLMAP Align
│  └─ 독립 Import·Runtime·Workspace·Export
│
└─ 공용 COLMAP Dataset
   ├─ Modifier
   │  └─ Import·편집·검토·COLMAP Export
   └─ Projection Painting
      └─ Camera Review·Stencil·Texture Painting
```

Modifier가 Import 버튼을 제공하지만, 실제 Point와 Camera의 소유자는 Modifier가 아니다. 중립적인 공용 Dataset이 실제 데이터를 소유하고 두 도구가 각자의 기능에 맞는 Adapter를 통해 사용한다.

Projection Painting은 Modifier Runtime이나 `Scene.colmap_import`를 직접 읽지 않는다. 공용 Dataset 계약만 사용하므로 Modifier 세션이 종료되어도 데이터 연결은 유지된다.

## 4. 현재 구조와 문제점

### 4.1 중복 Import

현재 두 도구는 다음 작업을 각각 수행한다.

- Dataset Root와 sparse/images 경로 탐색
- TXT/BIN Camera, Image와 Point3D 읽기
- Source 변경 검사와 이미지 경로 검증
- Display Axis Root 생성
- Point Mesh, 색상과 Geometry Nodes 생성
- Camera Data와 Camera Object 생성
- Dataset UUID, Source Fingerprint와 Manifest 저장

사용자는 같은 데이터를 두 번 Import해야 하며 Scene에는 역할이 같은 Point와 Camera가 두 벌 존재한다.

### 4.2 내부 계약은 완전히 같지 않음

화면 결과는 유사하지만 두 Importer의 내부 데이터 계약은 다르다.

| 구분 | Modifier | Projection Painting |
| --- | --- | --- |
| Reconstruction | Point2D, Track, Rig, Frame을 포함한 편집·Export용 전체 모델 | Camera Pose 중심의 Painting용 경량 모델 |
| Camera Model | 모든 파싱 가능 모델을 보존하고 비정확 투영은 경고 | 정확한 Paint 투영을 위해 `PINHOLE`, `SIMPLE_PINHOLE`만 허용 |
| Camera Data | IMAGE_ID별 독립 Camera Data | CAMERA_ID별 Camera Data 공유 |
| Point 데이터 | 편집·Pending Filter Attribute 포함 | 표시용 Point Attribute 포함 |
| Marker | Modifier Camera Review Manifest | Projection Painting Camera Manifest |
| 소유 태그 | `blender_colmap_*` | `pp_*` |
| 데이터 표시 | Modifier 전용 View Layer | Projection Painting 소유 Collection |

따라서 Projection Painting Import 버튼만 삭제하고 Modifier Object를 이름으로 찾아 쓰는 방식은 허용하지 않는다. 공용 소유권과 소비 계약을 먼저 만들어야 한다.

### 4.3 현재 제품 경계 변경

기존 제품 문서는 세 도구를 모두 데이터까지 독립된 도구로 정의한다. 이번 변경 후에도 Align은 독립 도구지만 Projection Painting은 같은 Scene의 공용 COLMAP Dataset을 필요로 하는 소비 도구가 된다.

구현 시 다음 문서를 함께 갱신한다.

- `docs/architecture/PRODUCT_BOUNDARIES.md`
- `docs/architecture/PACKAGE_STRUCTURE.md`
- `docs/decisions/0001-independent-tools.md`
- 새 ADR: Modifier·Projection Painting 공용 Dataset 채택

## 5. 목표 구조

### 5.1 패키지 구조

```text
shared/
└─ colmap_dataset/
   ├─ __init__.py
   ├─ contracts.py
   ├─ core/
   │  ├─ model.py
   │  ├─ discovery.py
   │  ├─ reader.py
   │  ├─ validation.py
   │  ├─ fingerprint.py
   │  └─ image_manifest.py
   └─ blender/
      ├─ properties.py
      ├─ ownership.py
      ├─ registry.py
      ├─ scene_resources.py
      ├─ transaction.py
      ├─ visibility.py
      └─ state_handlers.py

tools/modifier/
├─ blender/dataset_adapter.py
└─ 기존 편집·Review·Export 구현

tools/projection_paint/
├─ blender/dataset_adapter.py
└─ 기존 Paint·Layer·Stencil·Review 구현
```

`shared/colmap_dataset`은 두 도구가 함께 사용하는 제품 도메인이다. 어느 도구도 다른 도구 패키지를 Import하지 않으며 공용 Dataset 계층도 `tools/modifier` 또는 `tools/projection_paint`를 Import하지 않는다.

### 5.2 의존 방향

```text
Modifier UI ────────────────┐
                            ▼
                   shared.colmap_dataset
                            ▲
Projection Painting Adapter┘

Align ── 자기 Core와 Runtime만 사용
```

허용 규칙은 다음과 같다.

- Modifier는 공용 Dataset의 Import·편집 Transaction API를 사용한다.
- Projection Painting은 공용 Dataset의 읽기 전용 Snapshot과 Blender ID 조회 API를 사용한다.
- Projection Painting에서 Modifier 모듈을 직접 Import하지 않는다.
- 공용 Dataset은 도구별 Workspace, Marker, Undo와 UI를 해석하지 않는다.

## 6. 공용 Dataset 데이터 계약

### 6.1 Scene 상태

새 Scene Property를 추가한다.

```text
Scene.colmap_dataset
```

최소 영구 필드는 다음과 같다.

| 필드 | 의미 |
| --- | --- |
| `dataset_uuid` | 현재 Scene의 공용 Dataset 식별자 |
| `dataset_revision` | 확정된 Geometry·Camera 변경 Revision |
| `display_revision` | Axis·Point Scale·Frustum 표시 변경 Revision |
| `source_fingerprint` | COLMAP Source 변경 검증값 |
| `dataset_root` | Dataset Root |
| `model_directory` | 실제 sparse 모델 경로 |
| `images_directory` | 이미지 Root |
| `input_format` | TXT 또는 BIN |
| `layout` | legacy 또는 modern |
| `selected_files_json` | 검증된 Source 파일 목록 |
| `image_path_manifest_json` | IMAGE_ID별 검증 이미지 경로 |
| `camera_count` | Camera Record 수 |
| `image_count` | Image Pose 수 |
| `point3d_count` | Point3D 수 |
| `axis_preset` | 공용 표시축 Preset |
| `axis_forward`, `axis_up` | Custom 표시축 |
| `point_size` | 공용 Point 표시 크기 |
| `camera_frustum_size` | 공용 Camera 표시 크기 |
| `schema_version` | 공용 Dataset 저장 Schema |

Modifier의 편집 이력, Pending Filter와 Export 상태는 `Scene.colmap_import`에 유지한다. Projection Painting의 Paint Target, Layer, Texture와 Composite 상태는 `Scene.projection_paint`에 유지한다.

### 6.2 Runtime 상태

공용 Runtime은 Scene별로 다음 권위 상태를 가진다.

```text
DatasetRuntimeState
├─ SourceDescriptor
├─ 현재 Reconstruction
├─ ImagePathManifest
├─ Dataset UUID
├─ Dataset Revision
├─ Point·Camera Blender ID Manifest
└─ 변경 종류
```

도구는 Runtime 객체를 직접 수정하지 않는다.

- Modifier Import는 `commit_import()`를 호출한다.
- Modifier 편집 확정은 `commit_edit()`를 호출한다.
- Projection Painting은 `snapshot()`으로 읽기 전용 상태를 받는다.
- 모든 변경은 Staging, 검증, 짧은 Commit과 Rollback을 거친다.

### 6.3 공용 Blender 데이터

한 Scene에는 기본적으로 활성 공용 Dataset 하나만 둔다.

```text
COLMAP Dataset <uuid>
├─ AXIS_ROOT
├─ POINTS
│  └─ POINT_MESH
└─ CAMERAS
   └─ CAMERA_OBJECT × IMAGE_ID
```

소유 태그는 이름 대신 다음 중립 Key를 사용한다.

```text
colmap_dataset_owner
colmap_dataset_uuid
colmap_dataset_schema
colmap_dataset_role
colmap_dataset_record_id
colmap_dataset_source_fingerprint
```

Camera Object는 IMAGE_ID마다 하나씩 생성한다. Camera Background Image가 Camera Data에 저장되는 Blender 구조와 이미지별 Review 독립성을 고려해 Camera Data도 IMAGE_ID별로 유지한다. 같은 CAMERA_ID의 Intrinsics가 중복되더라도 Manifest에서 원본 CAMERA_ID 연결을 보존한다.

### 6.4 Dataset UUID와 Revision

- 다른 Dataset을 Import하면 새 Dataset UUID를 발급한다.
- 같은 Model Directory를 다시 Import하면 기존 UUID를 유지하고 Revision을 증가시킨다.
- Modifier의 확정된 Point·Camera 편집은 Dataset Revision을 증가시킨다.
- Pending 상태의 편집은 공용 Dataset에 반영하지 않는다.
- Axis, Point Scale과 Frustum 변경은 Display Revision만 증가시킨다.
- Export만 수행하고 메모리 Geometry가 바뀌지 않으면 Dataset Revision을 증가시키지 않는다.
- Source 파일을 교체하는 Export가 성공하면 새 Source Fingerprint와 기준 Revision을 원자적으로 갱신한다.

## 7. Modifier 역할

### 7.1 Import의 단일 진입점

Modifier의 `COLMAP Import` 패널을 유일한 사용자 Import 진입점으로 유지한다.

```text
Modifier Import 버튼
→ 공용 Core 읽기·검증
→ 공용 Blender Staging 생성
→ 공용 Dataset Commit
→ Modifier 편집 Runtime 연결
```

Modifier는 Import UI와 편집 기능을 제공하지만 공용 Point·Camera의 소유자로 표시하지 않는다.

### 7.2 편집 반영 규칙

| Modifier 작업 | 공용 Dataset 반영 시점 | Projection Painting 결과 |
| --- | --- | --- |
| Point 삭제 | 편집 확정 시 | Point Mesh에서 제거 |
| Image Camera 삭제 | Pending Apply 확정 시 | Camera·Marker·검색 Index에서 제거 |
| Point/Camera 선택 | 반영하지 않음 | 영향 없음 |
| Pending Filter | 반영하지 않음 | Apply 전까지 영향 없음 |
| Point Scale·Frustum | 즉시 표시 Revision 반영 | 두 Workspace에서 같은 표시값 사용 |
| Display Axis | 즉시 표시 Revision 반영 | 두 Workspace에서 같은 축 사용 |

편집 확정은 공용 Transaction 안에서 Reconstruction, Blender ID와 Revision을 함께 변경한다. 일부만 변경된 상태를 성공으로 남기지 않는다.

현재 Modifier의 확정 편집 범위는 Point 제거와 Image Camera 제거다. Camera Pose나 Intrinsics 편집은 이번 구조변경의 신규 기능으로 추가하지 않는다.

### 7.3 Modifier 전용 상태

다음 상태는 공용화하지 않는다.

- Point Edit Mode와 Camera Edit Mode
- 편집 History와 Modifier Undo
- Pending Camera Filter
- 관측 Camera 역추적 Index
- Modifier Camera Review 현재 위치
- COLMAP Export 진행 상태와 Backup

## 8. Projection Painting 역할

### 8.1 Import UI 제거

다음 UI와 신규 실행 경로를 제거한다.

- Dataset Root
- Input Format
- Projection Painting 전용 Import 버튼
- Projection Painting 전용 Point·Camera 중복 생성

대신 `COLMAP Dataset` 연결 상태를 표시한다.

```text
COLMAP Dataset
├─ 상태: 연결됨 / 없음 / 변경됨 / 호환 불가
├─ Dataset 경로와 수량
├─ Dataset Revision
└─ 데이터가 없을 때: Modifier에서 Import 안내
```

Dataset이 없더라도 Projection Painting Workspace 자체는 열 수 있다. Paint Layer와 Target 관리는 유지하되 Camera 기반 Review·Stencil·Paint 시작 버튼만 비활성화한다.

### 8.2 연결과 재연결

Projection Painting Workspace 진입 시 다음 순서로 연결한다.

1. 현재 Scene의 공용 Dataset 존재 여부 확인
2. Dataset UUID와 Revision 확인
3. 정확한 Paint 투영을 지원하는 Camera Model 검사
4. 공용 Camera Object와 이미지 경로 Manifest 조회
5. Projection 전용 Camera 검색 Index 구성
6. Projection Marker 세션 Attach
7. 마지막 IMAGE_ID와 Stencil 연결 복구

같은 UUID에서 Revision만 바뀐 경우 파생 Runtime만 다시 만든다. Point·Camera Blender ID는 공용 Manifest를 기준으로 다시 찾는다.

다른 UUID로 교체된 경우 다음 상태를 초기화한다.

- 현재 Camera와 IMAGE_ID
- Camera Marker 실행 사본
- 최근접 Camera 검색 Index
- Stencil 이미지 연결

다음 데이터는 삭제하지 않는다.

- Paint Target
- Original Layer와 일반 Paint Layer
- Mask와 Adjustment
- Composite와 내보낸 Texture
- 기존 Paint Image Pixel

### 8.3 Camera Model 호환성

Modifier Import는 파싱 가능한 COLMAP Camera Model을 모두 보존한다. Projection Painting 진입 시 `PINHOLE` 또는 `SIMPLE_PINHOLE`이 아닌 Camera가 하나라도 있으면 정확한 투영을 시작하지 않는다.

표시 문구는 다음처럼 단순하게 유지한다.

```text
Modifier에서 불러온 카메라 중 Projection Painting이 지원하지 않는 카메라 모델이 있습니다.
```

공용 Dataset 자체와 Modifier 편집·Export는 계속 사용할 수 있어야 한다.

### 8.4 Projection Painting 전용 상태

다음 상태는 공용화하지 않는다.

- Paint Target과 Material
- Texture Paint Mode
- Stencil Session
- Paint Layer, Mask와 Adjustment
- Composite Session
- Paint Undo와 복구 파일
- Camera Review 현재 선택
- Projection Painting Marker Attach 상태

## 9. Workspace와 가시성

### 9.1 View Layer 구성

공용 Dataset은 Object나 Collection의 전역 `hide_viewport`로 숨기지 않는다. View Layer별 포함·제외로 관리한다.

| View Layer | 공용 Dataset | 사용자 일반 데이터 | Paint Target |
| --- | --- | --- | --- |
| Modifier 전용 | 표시 | 숨김 | 숨김 |
| Projection Painting 전용 | 표시 | 기존 정책 유지 | 표시 |
| 일반 Blender | 숨김 | 표시 | 표시 |
| Align | 숨김 | Align GPU 정책 유지 | 숨김 |

Projection Painting 전용 View Layer를 새로 만들고 Workspace 진입 시 활성화한다. 기존 임의 View Layer의 제외 상태를 덮어쓰지 않는다.

### 9.2 세션 종료

Workspace를 벗어나면 도구 Runtime만 종료한다.

- 공용 Point·Camera 데이터는 삭제하지 않는다.
- Modifier와 Projection Painting의 실제 Timeline Marker는 제거한다.
- Marker Manifest는 보존한다.
- 일반 Blender Workspace에서는 공용 Dataset을 보이지 않게 한다.
- 재진입 시 공용 Dataset과 도구별 파생 Runtime을 자동 재연결한다.

## 10. Marker와 Frame Mapping

Frame Mapping은 Dataset 공용 Manifest로 승격한다.

```text
IMAGE_ID 오름차순
→ 공용 frame_start
→ IMAGE_ID ↔ Frame Mapping
```

실제 `Scene.timeline_markers`는 Scene 전역 데이터이므로 공용 영구 Marker로 만들지 않는다.

- Modifier 활성: Modifier Marker만 Attach
- Projection Painting 활성: Projection Marker만 Attach
- 일반 Workspace 또는 Align 활성: 두 Marker 모두 Detach
- Camera Object는 공용 Object를 참조
- Modifier Pending Camera는 Modifier Marker에서만 제외
- Projection Painting은 현재 공용 Dataset의 유효 Camera 전체를 사용

## 11. Undo, Load와 저장

### 11.1 Undo 경계

- Modifier Undo는 확정된 공용 Dataset 편집 Revision을 되돌린다.
- Projection Painting Undo는 Paint Layer와 Texture 변경만 되돌린다.
- 한 도구의 Undo Stack을 다른 도구에서 직접 사용하지 않는다.
- Blender Global Undo 뒤에는 공용 Dataset Registry를 Scene Metadata와 소유 Blender ID에서 재구성한다.

### 11.2 파일 Load

`.blend` Load 후 다음 순서로 복구한다.

1. 공용 Dataset Scene Property 검증
2. 공용 소유 Collection과 Manifest 검증
3. Runtime Reconstruction 재구성 또는 Source 재검증
4. 현재 Workspace 도구 판정
5. 해당 도구 Adapter만 재연결
6. 비활성 도구 Marker는 생성하지 않음

Source 파일이 변경되었더라도 공용 Blender 표시 데이터는 유지한다. Modifier 편집과 Export는 중단하고 명확한 경고를 표시하며 Projection Painting은 저장된 Camera Manifest의 검증 범위 안에서만 연결한다.

## 12. 기존 `.blend` 마이그레이션

### 12.1 Modifier 데이터만 있는 파일

기존 Modifier 소유 Collection, Point와 Camera의 UUID, Source Fingerprint와 수량을 검증한다. 검증에 성공하면 공용 소유 Metadata로 원자적으로 승격하고 기존 Modifier 편집 이력을 새 Dataset UUID에 연결한다.

검증에 실패하면 데이터를 임의 변환하지 않고 Modifier에서 다시 Import하도록 안내한다.

### 12.2 Projection Painting 데이터만 있는 파일

기존 Projection Painting 데이터는 전체 Point2D, Rig와 Frame을 보장하지 않으므로 공용 권위 Dataset으로 자동 승격하지 않는다.

- Paint Target, Layer, Texture와 Composite는 보존한다.
- 기존 PP Camera·Point Collection은 Legacy 상태로 유지한다.
- Modifier에서 같은 Source를 Import하도록 안내한다.
- 공용 Dataset Import가 성공하면 Source 경로, Camera/Image/Point ID, Pose와 이미지 Manifest를 비교한다.
- 일치가 확인된 뒤에만 기존 PP Camera·Point 중복 데이터를 제거한다.

### 12.3 두 데이터가 모두 있는 파일

Modifier의 전체 Reconstruction을 공용 권위 후보로 사용한다. 두 Dataset의 Source 경로, ID 집합, Camera Pose, Point 수와 이미지 경로를 비교한다.

- 일치: Modifier 데이터를 공용 Dataset으로 승격하고 PP 파생 Runtime을 재연결한 뒤 PP 중복 Point·Camera 제거
- 불일치: 자동 삭제 금지, Modifier Dataset을 기본 후보로 표시하고 사용자에게 다시 Import 안내

### 12.4 Projection Painting Import 코드 제거 순서

1. 새 공용 Dataset 구조와 Legacy 탐지기 추가
2. Projection Painting Import UI 비노출
3. 기존 PP Dataset의 읽기 전용 마이그레이션 Adapter 유지
4. 저장·재로드와 실제 Legacy 파일 검증
5. 마이그레이션 완료 버전 이후 PP Reader, Import Job과 중복 Scene Builder 삭제

기존 파일을 읽을 수 있는 경로가 없는 상태에서 Import 코드를 먼저 삭제하지 않는다.

## 13. 오류 처리

| 상황 | 처리 |
| --- | --- |
| 공용 Dataset 없음 | Projection 기능 비활성화, Modifier Import 안내 |
| Source 파일 변경 | 기존 화면 데이터 유지, 편집·Export 잠금과 재Import 안내 |
| 지원하지 않는 Camera Model | Projection 기능만 차단, Modifier는 정상 유지 |
| Modifier 편집 Commit 실패 | 공용 Dataset과 Revision 원상 복구 |
| Projection Runtime 재연결 실패 | Paint 데이터 유지, Camera·Stencil만 비활성화 |
| 다른 Dataset Import | 새 UUID 생성, 기존 Paint Texture 보존, Camera·Stencil 연결 초기화 |
| View Layer 구성 실패 | 사용자 View Layer 변경 Rollback, Workspace 진입 실패 처리 |
| Legacy 소유권 불명확 | 자동 삭제·승격 금지 |

## 14. 단계별 구현 계획

### 단계 0. 기준선과 제품 결정 고정

- 두 Importer의 지원 형식, Camera Model, Source Fingerprint와 Scene 결과를 같은 Fixture로 비교한다.
- 기존 Modifier 편집·Export와 Projection Painting Review·Stencil 회귀를 고정한다.
- 한 Scene에 활성 공용 Dataset 하나만 지원하는 범위를 문서화한다.
- 새 ADR과 Architecture 테스트의 변경 초안을 작성한다.

완료 조건: 현재 동작 차이와 유지해야 할 기능이 자동 테스트로 고정되어야 한다.

### 단계 1. 공용 Dataset 계약 추가

- `shared/colmap_dataset` 패키지와 Scene Property를 추가한다.
- 중립 소유 태그, Dataset Registry, Snapshot과 Transaction 계약을 구현한다.
- Modifier의 전체 Reconstruction을 기준으로 공용 Core Model을 정의한다.
- Source Fingerprint와 ImagePathManifest를 공용 계약으로 통합한다.

완료 조건: Blender ID 없이 Core Import와 Snapshot 테스트가 통과해야 한다.

### 단계 2. Modifier Import를 공용 Dataset 생산자로 전환

- Modifier Import UI는 유지한다.
- 기존 Modifier 전용 Staging을 공용 Scene Resource Staging으로 교체한다.
- 기존 편집 Runtime과 Export가 공용 Reconstruction을 사용하도록 Adapter를 추가한다.
- Import 실패·취소·교체 Rollback을 검증한다.

완료 조건: Modifier 단독 Import·편집·Undo·Export 동작이 기존과 같고 생성 데이터의 소유 태그만 공용으로 바뀌어야 한다.

### 단계 3. 공용 View Layer 가시성 전환

- Modifier 전용 View Layer가 공용 Dataset을 표시하도록 변경한다.
- Projection Painting 전용 View Layer를 추가한다.
- 일반 Blender와 Align에서는 공용 Dataset을 제외한다.
- Workspace 왕복과 다중 Window 가시성을 검증한다.

완료 조건: 공용 Blender ID 한 벌이 Modifier와 Projection Painting에서만 보이고 일반 Workspace와 Align에서는 보이지 않아야 한다.

### 단계 4. Projection Painting을 공용 Dataset 소비자로 전환

- Projection Painting Dataset Adapter를 구현한다.
- 기존 Camera Runtime, 검색 Index와 Stencil을 공용 Camera Object에 연결한다.
- Camera Model 호환성 검사를 Workspace 진입 경계로 이동한다.
- 기존 Import UI를 Dataset 연결 상태 UI로 교체한다.
- 신규 `projection_paint.import_colmap` 실행 경로를 등록 목록에서 제거한다.

완료 조건: Projection Painting에서 재Import하지 않고 Modifier Dataset으로 Camera Review, F7, 최근접 Camera와 Stencil을 사용할 수 있어야 한다.

### 단계 5. Modifier 편집 Revision 동기화

- Point·Camera 확정 편집을 공용 Transaction에 연결한다.
- 변경 종류와 Dataset Revision을 기록한다.
- Projection Painting 재진입 시 변경 범위에 맞는 파생 Runtime만 다시 만든다.
- Paint Layer와 Texture 불변 회귀를 추가한다.

완료 조건: Modifier의 확정 편집이 Projection Painting에 반영되고 Pending 편집은 반영되지 않아야 한다.

### 단계 6. Marker·Frame Mapping 공용화

- Frame Mapping Manifest를 공용 Dataset으로 이동한다.
- 두 도구의 실제 Marker Attach·Detach는 기존 세션 소유권을 유지한다.
- 공용 Camera Object를 참조하는 Marker Manifest Adapter를 추가한다.
- 같은 Frame에서 Modifier와 Projection Painting을 왕복 검증한다.

완료 조건: 현재 활성 도구의 Marker만 존재하면서 IMAGE_ID와 Frame이 두 도구에서 일치해야 한다.

### 단계 7. Legacy 마이그레이션

- Modifier 전용 데이터의 공용 승격을 구현한다.
- PP 전용 데이터의 보존·재Import·검증 후 제거 절차를 구현한다.
- 두 데이터가 함께 있는 파일의 중복 제거를 구현한다.
- 소유권이 모호한 파일은 Fail Closed로 유지한다.

완료 조건: 기존 `.blend`의 Paint Texture와 Modifier 편집 결과가 손실되지 않아야 한다.

### 단계 8. 중복 코드 제거와 문서 갱신

- 사용되지 않는 Projection Painting COLMAP Reader와 Import Job을 제거한다.
- 중복 Point·Camera Scene Builder를 제거한다.
- Architecture 테스트를 새 의존 방향으로 갱신한다.
- 제품 경계, 패키지 구조, 테스트 문서와 Release Note를 갱신한다.

완료 조건: Production Source에 Projection Painting 전용 COLMAP Import 경로와 중복 Scene 생성 코드가 남지 않아야 한다.

### 단계 9. 패키징과 설치 QC

- Extension Version을 구조변경 Version으로 갱신한다.
- `SmallGicp` Production ZIP을 빌드한다.
- 격리 설치본에서 전체 자동 회귀를 실행한다.
- 실제 Blender 사용자 Repository에 새 패키지를 설치한다.
- MCP로 Modifier Import → 편집 → Projection Painting 재연결 전체 흐름을 검증한다.

완료 조건: Source, 격리 설치본과 실제 설치본에서 같은 Dataset UUID·Revision·Object Pointer 결과를 얻어야 한다.

## 15. 테스트 계획

### 15.1 공용 Core

- TXT/BIN legacy와 modern Layout
- 비연속 Camera, Image와 Point3D ID
- Point2D, Track, Rig와 Frame 보존
- Image 경로 안전성, 한글·공백·중첩 경로
- Source 변경 감지와 Fingerprint 재현성
- `PINHOLE`, `SIMPLE_PINHOLE` 및 기타 Camera Model 보존
- 대형 Point3D 증분 Import와 취소

### 15.2 Modifier

- 공용 Dataset Import와 원자 교체
- Point·Camera 편집과 Revision 증가
- Pending Filter의 비공개 상태
- Modifier Undo·Reset과 Revision 복원
- TXT/BIN Export, Backup과 Source Fingerprint 갱신
- Camera Review, F7과 이미지 Background

### 15.3 Projection Painting

- Dataset 없음 상태 UI
- 공용 Dataset 자동 연결
- Camera Model 호환성 차단
- Camera Review, F7과 최근접 Camera 검색
- Stencil 연결과 Paint 진입
- Dataset Revision 변경 후 자동 재연결
- 다른 Dataset 교체 뒤 Paint Layer·Texture 보존
- PP Import UI와 Operator 미등록

### 15.4 교차 도구 GUI

```text
1. Modifier에서 Dataset Import
2. Projection Painting 진입
3. 같은 Point·Camera Object Pointer 사용 확인
4. Projection Painting Camera Review와 Stencil 확인
5. Modifier로 이동
6. Point와 Camera 편집 확정
7. Projection Painting 재진입
8. Dataset Revision과 Camera·Point 변경 반영 확인
9. Paint Layer와 Texture 불변 확인
10. 일반 Workspace와 Align에서 공용 Dataset 비표시 확인
```

### 15.5 Legacy

- 기존 Modifier 데이터만 있는 `.blend`
- 기존 PP 데이터만 있는 `.blend`
- 두 데이터가 함께 있는 `.blend`
- Source 파일이 없거나 변경된 `.blend`
- 이름은 같지만 소유 태그가 없는 사용자 Collection
- 저장·재로드와 Global Undo/Redo
- 같은 Scene을 사용하는 다중 Window

## 16. 예상 변경 범위

| 경로 | 변경 내용 |
| --- | --- |
| `shared/colmap_dataset/` | 공용 Core, Scene Property, 소유권, Registry, Transaction과 가시성 추가 |
| `shell/lifecycle.py` | 공용 Dataset Lifecycle 등록·해제 순서 추가 |
| `tools/modifier/core/` | 공용 Core Model·Reader 사용과 Modifier 전용 편집·Export Adapter 분리 |
| `tools/modifier/blender/operators_import.py` | 공용 Import Transaction 호출 |
| `tools/modifier/blender/scene_elements.py` | 공용 Scene Resource Builder로 이동 |
| `tools/modifier/blender/ownership.py` | Legacy 식별과 공용 소유권 Adapter |
| `tools/modifier/blender/modifier_data_visibility.py` | 공용 Dataset View Layer 정책 적용 |
| `tools/projection_paint/core/colmap/` | 마이그레이션 완료 후 중복 Reader·Model 제거 |
| `tools/projection_paint/blender/camera_import.py` | Legacy Adapter 분리 후 신규 Import 경로 제거 |
| `tools/projection_paint/blender/camera_scene.py` | 공용 Camera Manifest Adapter 사용 |
| `tools/projection_paint/blender/point_scene.py` | 중복 Point 생성 제거, 공용 Point 표시 갱신 연결 |
| `tools/projection_paint/blender/paint_workspace.py` | 공용 Dataset View Layer와 재연결 적용 |
| `tools/projection_paint/blender/panels.py` | Import UI를 Dataset 상태 UI로 교체 |
| `tools/projection_paint/blender/properties.py` | 중복 Import 설정 제거 또는 Legacy 읽기 전용 유지 |
| `shared/blender_runtime/scene_marker_session.py` | 공용 Camera Object를 참조하는 도구별 Marker 계약 적용 |
| `tests/` | 공용 Dataset, 편집 동기화, Legacy와 전체 GUI 회귀 추가 |
| `docs/` | 제품 경계, ADR, 구현 상태, 테스트와 Release Note 갱신 |

## 17. 위험과 대응

| 위험 | 대응 |
| --- | --- |
| Projection Painting이 Modifier Runtime에 종속됨 | 직접 Import 금지, 공용 Dataset 계약만 사용 |
| Modifier 편집 중 Projection Runtime이 오래된 상태를 사용 | Workspace 배타 실행과 Dataset Revision 재연결 |
| 공용 Object를 일반 Workspace에서 보게 됨 | 도구별 전용 View Layer와 일반 View Layer 제외 계약 |
| Camera Background가 이미지 사이에서 공유됨 | IMAGE_ID별 Camera Data 유지 |
| 비지원 Camera Model이 전체 Import를 막음 | Modifier Import 허용, Projection 기능만 호환성 검사로 차단 |
| 기존 PP Paint 데이터가 삭제됨 | Layer·Texture 보존 후 공용 Dataset 검증이 끝난 뒤 중복 Camera·Point만 제거 |
| 두 Fingerprint 방식 차이로 Legacy가 잘못 병합됨 | 경로뿐 아니라 ID, Pose, 수량과 이미지 Manifest까지 비교 |
| Undo가 다른 도구 상태를 손상함 | 공용 Dataset Revision과 도구별 Undo Domain 분리 |
| Import 코드를 조기에 삭제해 기존 파일을 못 읽음 | UI 제거와 물리 코드 삭제를 별도 단계로 수행 |
| Align까지 의존 관계가 확장됨 | Architecture 테스트로 Align의 공용 Dataset Import 금지 |

## 18. 제외 범위

이번 구조변경에는 다음을 포함하지 않는다.

- Align Runtime과 공용 Dataset 연결
- Align 결과의 Modifier 자동 전달
- 여러 공용 Dataset을 동시에 활성화하는 기능
- Modifier Undo와 Projection Painting Undo 통합
- 두 도구 Workspace와 Session 통합
- 실제 Timeline Marker 영구 공유
- Paint Layer와 Modifier 편집 History 통합
- 비 Pinhole Camera의 왜곡 보정 Projection 구현
- UI 전면 재설계

## 19. 완료 조건

다음을 모두 만족해야 구조변경을 완료로 판단한다.

1. COLMAP Import UI가 Modifier에만 존재한다.
2. Projection Painting의 신규 COLMAP Import Operator가 등록되지 않는다.
3. Scene에는 공용 Point·Camera 데이터가 한 벌만 존재한다.
4. Modifier와 Projection Painting이 같은 Dataset UUID와 Blender Object Pointer를 사용한다.
5. Modifier의 확정된 Point·Camera 편집이 Projection Painting 재진입 시 반영된다.
6. Pending 편집은 확정 전까지 Projection Painting에 반영되지 않는다.
7. Paint Layer, Texture, Mask와 Composite는 Dataset 편집·교체 뒤에도 보존된다.
8. 비지원 Camera Model은 Projection 기능만 차단한다.
9. 일반 Blender Workspace와 Align에서는 공용 Dataset이 보이지 않는다.
10. Workspace 전환 시 실제 Marker는 활성 도구 것만 존재한다.
11. Modifier와 Projection Painting의 Undo, Runtime과 오류 상태가 분리되어 있다.
12. 기존 Modifier 데이터는 검증 후 공용 Dataset으로 승격할 수 있다.
13. 기존 PP Paint 데이터는 손실 없이 공용 Dataset에 재연결할 수 있다.
14. 소유권이 모호하거나 Source가 다른 데이터는 자동 삭제하지 않는다.
15. Align 코드와 Runtime은 공용 Dataset에 의존하지 않는다.
16. Background, GUI, Legacy, 격리 설치본과 사용자 실데이터 회귀가 통과한다.
17. Production ZIP 설치와 MCP 최종 QC가 통과한다.
18. Debug 로그, Legacy 중복 데이터와 임시 마이그레이션 파일이 최종 배포물에 남지 않는다.

## 20. 최종 사용자 흐름

```text
1. Modifier 열기
2. COLMAP Dataset Import
3. 필요하면 Point·Camera 편집 후 확정
4. Projection Painting Workspace로 이동
5. 공용 Dataset 자동 연결
6. Camera 선택과 Stencil Painting
7. Modifier로 돌아가 추가 편집
8. Projection Painting 재진입 시 변경 자동 반영
```

사용자는 같은 COLMAP 데이터를 두 번 Import하지 않는다. Align은 이 흐름과 무관하게 독립적으로 열고 사용할 수 있다.
