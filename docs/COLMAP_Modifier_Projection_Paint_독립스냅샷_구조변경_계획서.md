# COLMAP Modifier·Projection Painting 독립 Snapshot 구조변경 계획서

## 1. 문서 정보

- 작성일: 2026-08-29
- 대상 제품: `COLMAP Tools` Blender Extension
- 대상 Blender: 5.2 LTS 이상
- 기준 버전: `0.2.0`
- 목표 버전 후보: `0.3.0`
- 대상 도구: `COLMAP Modifier`, `Projection Painting`
- 독립 유지 도구: `COLMAP Align`
- 문서 상태: 구현 전 구조변경 계획

## 2. 결정 요약

Modifier와 Projection Painting의 실시간 공용 COLMAP Dataset 사용을 중단한다.

Modifier는 COLMAP Import·편집·Export의 권위 데이터를 계속 소유한다. Projection Painting은 사용자가 `COLMAP 데이터 업데이트` 버튼을 눌렀을 때 Modifier의 현재 확정 데이터를 값 Snapshot으로 복사하고, 이후에는 자기 Runtime·Camera·Point·Manifest를 독립적으로 소유한다.

```text
COLMAP Modifier
└─ 권위 Reconstruction·Camera·Point·편집 History

          COLMAP 데이터 업데이트
                    │
                    ▼

Projection Painting
└─ 독립 Snapshot·Camera·Point·Manifest·Paint Runtime
```

업데이트 이후에는 두 데이터가 자동 동기화되지 않는다.

- Modifier 편집은 Projection Painting에 즉시 반영되지 않는다.
- Projection Painting 데이터는 Modifier Undo에 의해 변경되지 않는다.
- Projection Painting Undo는 Modifier 데이터를 변경하지 않는다.
- 최신 Modifier 데이터를 사용하려면 사용자가 다시 업데이트한다.
- 업데이트해도 기존 Paint Target, Layer, Texture, Mask와 Composite는 유지한다.

Blender Undo Stack은 새로 만들거나 도구별로 분리하지 않는다. Blender 기본 Undo를 그대로 사용하고, 비활성 도구가 다른 도구의 Undo마다 무거운 Runtime 복구를 실행하지 않도록 Lifecycle 조건만 정리한다.

## 3. 쉬운 결론

이 구조는 통합 전 Projection Painting이 자기 COLMAP 데이터를 별도로 Import하던 방식과 데이터 소유권이 같다. 차이는 COLMAP 파일을 다시 선택하는 대신 Modifier의 현재 확정 데이터를 버튼으로 가져온다는 점이다.

```text
통합 전
Projection Painting Import 버튼
→ COLMAP 파일에서 PP 전용 데이터 생성

변경 후
Projection Painting 업데이트 버튼
→ Modifier의 현재 데이터에서 PP 전용 데이터 생성
```

따라서 실시간 공용 Revision, 공용 Camera Object와 공용 Point Object를 Undo마다 재연결할 필요가 없다.

## 4. 변경 이유

### 4.1 현재 공용 구조의 문제

현재 `0.2.0` 구조에서는 Modifier와 Projection Painting이 같은 Camera Object, Camera Data, Point Object와 공용 Dataset Revision을 사용한다.

이 구조는 중복 Import를 없애고 Modifier 편집을 Projection Painting에 자동 반영하지만 다음 결합을 만든다.

- Painting Undo 뒤 공용 Dataset Binding 재실행
- Projection Camera Runtime과 Marker의 Revision 재검증
- Modifier Global Undo Handler의 불필요한 동시 실행
- Modifier 편집 Undo가 Projection Painting의 Camera·Stencil 연결에 미치는 영향
- Workspace 왕복 중 공용 Blender ID Pointer의 재해석
- 두 도구의 복구 순서와 Dataset Revision 순서 의존

### 4.2 확인된 Undo 병목

현재 Extension은 같은 Blender Global Undo에 Modifier와 Projection Painting Handler를 모두 등록한다.

```text
Painting Undo
├─ Modifier Runtime·Review Cache 폐기
├─ Projection Painting Runtime Cache 폐기
├─ Blender Image Undo
├─ Modifier 전체 Runtime 복구 예약
├─ 공용 Dataset 재바인딩
├─ Projection Camera Runtime 재구성
└─ Projection Marker 전체 검사
```

사용자 Scene 기준 Camera 593개와 8192×8192 Paint Image를 사용한다. `Use Base Resolution`이 활성화됐을 때 2K 선택값 대신 원본 8K를 사용하는 것은 정상이며 변경 대상이 아니다.

### 4.3 별도 Undo Stack을 채택하지 않는 이유

Blender는 Global, Edit Mode와 Paint Undo 단계를 하나의 시간순 Stack에서 관리한다. 애드온이 Modifier와 Projection Painting의 Ctrl+Z Stack을 따로 만들면 Blender Native Texture Paint Undo와 애드온 History의 순서가 달라진다.

다음 구조는 사용하지 않는다.

- Ctrl+Z Keymap을 도구별 Custom Undo로 교체
- Modifier와 Projection Painting의 병렬 Undo Stack 생성
- Blender Undo Step과 별도 Stack의 위치를 추측해 동기화
- Workspace 이름만으로 과거 Undo Step의 소유 도구 판정

공식 기준:

- Blender Undo System: https://developer.blender.org/docs/features/core/undo/
- Blender 5.2 Application Handlers: https://docs.blender.org/api/5.2/bpy.app.handlers.html
- Blender 5.2 Operator Undo: https://docs.blender.org/api/5.2/bpy.types.Operator.html
- Undo 뒤 Blender ID 참조 안전 지침: https://docs.blender.org/api/5.2/info_gotchas_crashes.html

## 5. 목표와 제외 범위

### 5.1 목표

1. Modifier와 Projection Painting이 서로 다른 COLMAP Core Model과 Blender Resource를 소유한다.
2. Projection Painting은 버튼을 눌렀을 때만 Modifier의 확정 데이터를 복사한다.
3. Modifier 편집·Undo는 기존 Projection Painting Snapshot을 변경하지 않는다.
4. Projection Painting의 Texture Paint·Layer Undo는 Modifier 데이터를 변경하지 않는다.
5. 기존 Paint Target, Layer, Texture, Mask, Adjustment와 Composite를 업데이트 전후 보존한다.
6. 업데이트는 Staging·검증·Commit·Rollback을 갖는 원자적 작업으로 수행한다.
7. 두 도구는 서로의 Python 패키지를 직접 Import하지 않는다.
8. 비활성 도구는 Global Undo 뒤 무거운 Runtime을 즉시 복구하지 않는다.
9. 활성 도구 Runtime은 Undo 뒤 현재 Blender ID에서 한 번만 복구한다.
10. 일반 Blender Workspace와 Align에서는 두 COLMAP 데이터가 보이지 않는다.

### 5.2 제외 범위

- Modifier 편집의 Projection Painting 자동 반영
- 실시간 Dataset Revision 구독
- 공용 Camera·Point Blender Object 유지
- 도구별 별도 Ctrl+Z Stack
- 별도 픽셀 Undo 저장소
- `Use Base Resolution` 의미 변경
- Paint Image 해상도 자동 축소
- Align 데이터 연결
- COLMAP 파일 형식 변경
- Modifier 편집 History와 Paint Layer History 통합

## 6. 목표 제품 구조

```text
COLMAP Tools
├─ COLMAP Align
│  └─ 기존 독립 데이터·Runtime
│
├─ COLMAP Modifier
│  ├─ COLMAP 파일 Import
│  ├─ 권위 Reconstruction
│  ├─ Modifier 전용 Point·Camera
│  ├─ 편집 History·Undo
│  └─ COLMAP Export
│
├─ 중립 Snapshot Transfer 계약
│  └─ Blender ID를 포함하지 않는 값 전달
│
└─ Projection Painting
   ├─ COLMAP 데이터 업데이트 버튼
   ├─ PP 전용 Camera·Point
   ├─ PP 전용 Manifest·Runtime
   └─ Paint Target·Layer·Texture·Stencil·Undo
```

Modifier는 데이터 생산자이지만 Projection Painting Runtime의 소유자가 아니다. Projection Painting은 업데이트가 완료된 순간부터 전달받은 값의 독립 소유자가 된다.

## 7. 중립 Snapshot Transfer 계약

### 7.1 패키지 구조

```text
shared/
└─ colmap_transfer/
   ├─ __init__.py
   ├─ contracts.py
   ├─ provider_registry.py
   └─ validation.py

tools/modifier/blender/
└─ projection_snapshot_provider.py

tools/projection_paint/blender/
├─ snapshot_update.py
└─ snapshot_scene_transaction.py
```

`shared/colmap_transfer`는 도구 Runtime을 Import하지 않는다. Modifier가 Provider Callback을 등록하고 Projection Painting이 중립 Registry를 통해 Snapshot을 요청한다.

### 7.2 전달 데이터

Projection Painting에 필요한 값만 전달한다.

```text
ProjectionColmapSnapshot
├─ source_dataset_uuid
├─ source_dataset_revision
├─ source_fingerprint
├─ images_directory
├─ Camera Model·Intrinsics
├─ IMAGE_ID·Camera ID
├─ Image 이름·검증된 경로
├─ Camera Pose
├─ Point3D 위치·색상
├─ Frame Mapping 기준값
└─ Axis·Point Scale·Frustum 초기값
```

Modifier 전용 데이터는 복사하지 않는다.

- Point2D 관측 편집 Index
- Track과 Pending Camera Filter 상태
- Modifier Edit Operation 객체
- Camera Review Session Pointer
- Blender Object·Mesh·Camera·Collection Pointer
- Export Progress와 Backup 상태

### 7.3 Snapshot 불변 조건

- 모든 ID는 비연속 값을 허용하고 중복을 금지한다.
- Camera Model, Parameter 수, Pose와 Image 경로를 검증한다.
- Projection Painting이 지원하는 `PINHOLE`, `SIMPLE_PINHOLE` 조건은 Commit 전에 확인한다.
- Point와 Camera 값 배열은 생성 이후 읽기 전용으로 취급한다.
- Blender ID와 Context 객체를 Snapshot에 포함하지 않는다.
- Source UUID·Revision·Fingerprint를 전달 결과에 기록한다.
- 전달 중 Modifier 데이터가 변경되면 Snapshot을 폐기하고 다시 시도한다.

## 8. Projection Painting 독립 데이터 계약

### 8.1 Scene 상태

`Scene.projection_paint`에 다음 영구 상태를 둔다.

| 필드 | 의미 |
| --- | --- |
| `snapshot_uuid` | PP 독립 Snapshot 식별자 |
| `snapshot_revision` | PP 내부 Snapshot 교체 Revision |
| `source_modifier_dataset_uuid` | 가져온 Modifier Dataset 식별자 |
| `source_modifier_revision` | 가져온 시점의 Modifier Revision |
| `source_fingerprint` | 가져온 Source 기준값 |
| `snapshot_camera_count` | PP Camera 수 |
| `snapshot_point_count` | PP Point 수 |
| `snapshot_created_at` | 마지막 성공 업데이트 시각 |
| `snapshot_state` | `NONE`, `UPDATING`, `READY`, `STALE`, `ERROR` |
| `snapshot_schema_version` | 저장 Schema |

기존 Paint Target, Layer, Texture와 Composite 필드는 그대로 유지한다.

### 8.2 Blender Resource

Projection Painting은 별도 소유 Root를 만든다.

```text
Projection Paint COLMAP <snapshot_uuid>
├─ AXIS_ROOT
├─ POINTS
│  └─ PP_POINT_MESH
└─ CAMERAS
   └─ PP_CAMERA_OBJECT × IMAGE_ID
```

소유 태그는 공용 Dataset 태그가 아니라 Projection Painting 전용 태그를 사용한다.

```text
pp_snapshot_owner
pp_snapshot_uuid
pp_snapshot_schema
pp_snapshot_role
pp_snapshot_record_id
pp_source_modifier_dataset_uuid
pp_source_modifier_revision
```

Modifier Camera·Point와 PP Camera·Point의 Object/Data Pointer는 달라야 한다.

### 8.3 표시 설정

Point Scale, Camera Frustum과 Axis는 업데이트 시 Modifier 값을 초기값으로 복사한다. 이후 두 도구에서 독립적으로 변경한다.

- Modifier 표시 변경은 PP에 자동 반영하지 않는다.
- PP 표시 변경은 Modifier에 반영하지 않는다.
- 다시 업데이트할 때 현재 PP 표시값을 유지할지 Modifier 값으로 교체할지는 UI Option으로 만들지 않고, 기본 계약은 PP 현재값 유지로 고정한다.

## 9. 사용자 업데이트 흐름

### 9.1 UI

Projection Painting의 Dataset Panel은 다음 상태를 표시한다.

```text
COLMAP Snapshot
├─ 상태: 없음 / 최신 / 업데이트 가능 / 업데이트 중 / 오류
├─ 가져온 Modifier Dataset 경로
├─ 가져온 Revision
├─ 현재 Modifier Revision
├─ Camera·Point 수
└─ [COLMAP 데이터 업데이트]
```

현재 Modifier 데이터가 없으면 버튼을 비활성화하고 다음 문구를 표시한다.

```text
COLMAP Modifier에서 데이터를 먼저 불러오세요.
```

현재 PP Snapshot이 있어도 Modifier Revision이 달라지면 기존 데이터는 그대로 사용하고 `업데이트 가능`만 표시한다. 자동 업데이트하지 않는다.

### 9.2 업데이트 순서

```text
1. Modifier의 확정 Dataset 존재·호환성 확인
2. Source UUID·Revision·Fingerprint 캡처
3. 중립 ProjectionColmapSnapshot 생성
4. PP Core Model Staging 생성
5. PP 전용 Point·Camera·Manifest Staging 생성
6. Camera Model·이미지 경로·소유권·수량 검증
7. Source UUID·Revision·Fingerprint 재검증
8. 활성 PP Marker Session 일시 분리
9. 새 PP Snapshot과 Blender Resource 원자 Commit
10. 기존 Paint Target·Layer·Texture 연결 복구
11. 활성 Workspace이면 새 PP Marker와 Stencil 연결
12. 이전 PP COLMAP Resource만 제거
```

업데이트 작업은 Blender 데이터를 변경하므로 하나의 표준 Blender Undo Step으로 기록한다. 별도 Undo Stack은 만들지 않는다.

### 9.3 업데이트 후 독립성

- Modifier Camera 제거 후 PP Snapshot의 Camera는 유지된다.
- Modifier Point 제거 후 PP Point는 유지된다.
- Modifier Source Export 후 PP 이미지 경로는 기존 Snapshot 값을 유지한다.
- PP Camera Review, F7과 Stencil은 PP 소유 Camera만 사용한다.
- 최신 결과가 필요할 때 사용자가 다시 업데이트한다.

## 10. Paint 데이터 보존

COLMAP Snapshot 업데이트는 Paint 자산을 초기화하지 않는다.

보존 대상:

- Paint Target Object와 Mesh
- Paint Material
- Original Image
- 일반 Paint Layer
- Layer Mask
- Adjustment Layer
- Overlay와 Composite Image
- Export된 Texture
- Brush와 Stencil 설정 중 Camera Dataset과 무관한 값

연결을 다시 계산할 대상:

- 현재 IMAGE_ID와 Camera Object
- 현재 Frame
- Camera Background Image
- Stencil Source Image
- 최근접 Camera 검색 Index
- PP Marker Manifest 실행 사본

기존 현재 IMAGE_ID가 새 Snapshot에도 있으면 유지한다. 없으면 가장 가까운 유효 IMAGE_ID로 이동하고 사용자에게 변경 사실을 표시한다.

## 11. Undo와 Runtime Lifecycle

### 11.1 유지할 원칙

- Blender의 단일 Undo Stack을 그대로 사용한다.
- Modifier의 `Undo Last Edit`와 Pending Filter Undo를 유지한다.
- Projection Painting의 Native Texture Paint Undo와 Layer Operator Undo를 유지한다.
- 모든 Undo·Redo 전에 오래된 Blender ID Runtime Cache는 폐기한다.
- Undo 전에는 Scene Object, Image, Marker와 Dataset을 수정하지 않는다.

### 11.2 비활성 도구 지연 복구

두 도구의 Handler가 같은 Global Undo 이벤트를 받는 것은 허용하되, 비활성 도구는 가벼운 Cache 무효화만 수행한다.

```text
Painting Workspace에서 Undo
├─ Modifier: Pointer Cache 폐기, 전체 Reconcile 예약 안 함
└─ Projection Painting: 독립 PP Runtime만 한 번 복구

Modifier Workspace에서 Undo
├─ Modifier: Modifier Runtime만 복구
└─ Projection Painting: Pointer Cache 폐기, 전체 Rebuild 안 함

일반 Blender Workspace에서 Undo
├─ Modifier: Pointer Cache만 폐기
└─ Projection Painting: Pointer Cache만 폐기
```

비활성 도구의 직렬화된 영구 상태와 Blender 데이터는 그대로 둔다. 해당 Workspace 재진입 또는 기능 실행 시 기존 `ensure_runtime()` 경계에서 현재 Blender ID를 다시 찾는다.

### 11.3 활성 세션 판정

이 구조에는 복잡한 Undo Step Domain Router가 필요하지 않다. 현재 Undo 뒤 즉시 복구할 도구만 다음 값으로 판정한다.

- Undo를 실행한 Window와 Scene
- Tool ID가 일치하는 READY Workspace
- 해당 도구의 활성 Session 소유권
- Projection Painting은 Paint Workspace와 PP Snapshot READY 여부

Workspace 이름만 사용하지 않고 기존 공통 Workspace 소유 Metadata와 Session Pointer를 사용한다.

### 11.4 Projection Painting 중복 복구 제거

독립 Snapshot으로 전환해도 활성 Painting Undo 뒤 PP Runtime 복구는 필요하다. 다음 단일 진입점으로 합친다.

```text
rehydrate_projection_snapshot_runtime(scene, reason)
├─ PP Snapshot Manifest 검증
├─ PP Camera Runtime 현재 ID 재해석 1회
├─ 활성 Marker Lease 검증·연결 1회
├─ Paint State·Layer Graph Reconcile 1회
└─ Stencil·현재 Frame 연결 1회
```

공용 Dataset Binding Hook은 제거한다. Camera Runtime Hook과 Marker Session Hook이 같은 Camera와 Marker를 중복 처리하지 않게 한다.

### 11.5 Blender Stack의 시간순 의미

데이터가 독립되어도 Ctrl+Z 기록은 Workspace별 Stack이 아니다. 예를 들어 Modifier 작업 뒤 Painting 작업을 했다면 어느 Workspace에서 Ctrl+Z를 눌러도 Blender는 전체 Stack의 가장 최근 단계를 되돌린다.

이번 구조가 보장하는 것은 다음과 같다.

- 어떤 단계가 되돌아가도 다른 도구의 독립 데이터가 직접 참조로 손상되지 않는다.
- 현재 활성 도구가 아닌 도구의 무거운 복구는 지연된다.
- 도구별 전용 되돌리기가 필요하면 Modifier의 `Undo Last Edit` 같은 명시적 기능을 사용한다.

## 12. Marker와 Workspace 가시성

### 12.1 Marker

기존 Scene Marker 단일 사용권을 유지한다.

- Modifier 활성: Modifier 소유 Camera Marker만 Scene에 연결
- Projection Painting 활성: PP Snapshot Camera Marker만 Scene에 연결
- 일반 Workspace·Align: 두 도구 Marker 모두 분리
- Workspace 전환 시 Manifest에서 활성 도구 Marker만 재생성
- 사용자 Marker는 수정하거나 삭제하지 않음

### 12.2 View Layer

| View Layer | Modifier 데이터 | PP Snapshot 데이터 | 사용자 데이터 |
| --- | --- | --- | --- |
| Modifier 전용 | 표시 | 숨김 | 기존 Modifier 단독 표시 정책 |
| Projection Painting 전용 | 숨김 | 표시 | Paint 정책에 따라 표시 |
| 일반 Blender | 숨김 | 숨김 | 표시 |
| Align | 숨김 | 숨김 | Align 전용 정책 |

두 도구의 Collection을 전역 `hide_viewport`로 전환하지 않는다. 기존처럼 View Layer 포함·제외로 관리한다.

## 13. 현재 공용 Dataset 마이그레이션

### 13.1 `0.2.0` 파일

현재 `Scene.colmap_dataset`과 중립 소유 Camera·Point가 있는 파일은 다음 순서로 이전한다.

1. 공용 Dataset UUID·Revision·Source Metadata와 Resource 소유권을 검증한다.
2. Modifier `Scene.colmap_import`의 Reconstruction과 일치하는지 확인한다.
3. 일치하면 공용 Resource를 Modifier 소유 Resource로 원자 승격한다.
4. Projection Painting의 기존 Paint Target, Layer, Texture와 Composite를 보존한다.
5. PP에는 Snapshot이 없는 상태를 기록하고 `COLMAP 데이터 업데이트`를 안내한다.
6. 사용자가 업데이트하면 새 PP 전용 Resource를 생성한다.
7. PP 전용 Snapshot 검증 후 공용 Dataset 소비 Binding을 제거한다.

마이그레이션 과정에서 기존 Paint 자산이나 Modifier 편집 결과를 자동 삭제하지 않는다.

### 13.2 통합 전 Legacy PP 데이터

기존 Projection Painting 소유 Camera·Point와 Manifest가 있고 소유권·Source가 명확하면 PP 독립 Snapshot 후보로 검증한다.

- Camera Model, IMAGE_ID, Pose, 이미지 경로와 Point 수가 유효하면 기존 PP Resource를 재사용한다.
- Source UUID·Revision을 알 수 없으면 `Legacy Snapshot`으로 표시한다.
- Modifier 데이터와 자동 병합하지 않는다.
- 사용자가 업데이트하면 새 Snapshot으로 원자 교체한다.
- 소유권이 모호한 Camera·Point는 자동 삭제하지 않는다.

### 13.3 `Scene.colmap_dataset` 폐기 순서

1. 새 Transfer 계약과 PP Snapshot 구조 추가
2. Modifier 공용 Resource의 Modifier 소유권 승격
3. PP 업데이트 UI와 Transaction 구현
4. 저장·재로드와 Legacy 이전 검증
5. Production 코드의 공용 Resource 소비 제거
6. `Scene.colmap_dataset`은 한 버전 동안 읽기 전용 마이그레이션 입력으로 유지
7. 후속 호환성 Version에서 Property와 사용되지 않는 공용 Registry 제거

기존 파일을 읽을 수 있는 경로가 확보되기 전에 `shared/colmap_dataset`을 먼저 삭제하지 않는다.

## 14. 실패와 Rollback

| 상황 | 처리 |
| --- | --- |
| Modifier Dataset 없음 | 기존 PP Snapshot 유지, 업데이트 버튼 비활성화 |
| 지원하지 않는 Camera Model | 기존 PP Snapshot 유지, 새 업데이트 실패 |
| Snapshot 생성 중 Modifier Revision 변경 | Staging 폐기 후 다시 업데이트 안내 |
| 이미지 경로 누락 | 누락 IMAGE_ID 표시, 기존 Snapshot 유지 |
| PP Scene Resource 생성 실패 | Staging만 삭제, 기존 PP Resource 유지 |
| Marker 분리·재연결 실패 | 새 Commit Rollback, 기존 Marker Session 복원 |
| Paint 연결 복구 실패 | 기존 Paint 자산 보존, 새 Snapshot Commit Rollback |
| 업데이트 취소 | Staging과 진행 Runtime만 정리 |
| Undo 뒤 비활성 Runtime 없음 | 오류 아님, 재진입 시 지연 복구 |
| Legacy 소유권 불명확 | 자동 변환·삭제 금지 |

## 15. 예상 변경 파일

| 경로 | 변경 내용 |
| --- | --- |
| `shared/colmap_transfer/` | 값 전용 Snapshot 계약, Provider Registry와 검증 추가 |
| `shared/colmap_dataset/` | 한 버전 동안 Legacy Migration Adapter로 축소 |
| `shell/lifecycle.py` | Transfer Registry와 Migration 등록 순서 연결 |
| `tools/modifier/blender/projection_snapshot_provider.py` | 확정 Reconstruction의 PP용 Snapshot Provider 추가 |
| `tools/modifier/blender/dataset_adapter.py` | 실시간 공용 Commit 제거, Legacy 승격과 Transfer 연결 |
| `tools/modifier/blender/scene_elements.py` | Modifier 전용 소유 태그와 Resource 복원 |
| `tools/modifier/blender/state_handlers.py` | 비활성 Workspace의 전체 Undo Reconcile 지연 |
| `tools/modifier/blender/lifecycle.py` | Snapshot Provider 등록·해제 |
| `tools/projection_paint/blender/snapshot_update.py` | 업데이트 Operator·Job·진행 상태 추가 |
| `tools/projection_paint/blender/snapshot_scene_transaction.py` | PP Resource Staging·Commit·Rollback 추가 |
| `tools/projection_paint/blender/dataset_adapter.py` | 실시간 공용 Binding 제거, Legacy Migration으로 축소 |
| `tools/projection_paint/blender/camera_scene.py` | PP 소유 Camera Runtime과 Manifest 복원 |
| `tools/projection_paint/blender/point_scene.py` | PP 소유 Point Resource 복원 |
| `tools/projection_paint/blender/dataset_view_layer.py` | PP Snapshot Root 전용 표시 정책 적용 |
| `tools/projection_paint/blender/state_handlers.py` | 비활성 복구 지연과 활성 단일 Rehydrate 연결 |
| `tools/projection_paint/blender/lifecycle.py` | 공용 Binding Hook 제거와 Snapshot Runtime Hook 등록 |
| `tools/projection_paint/blender/panels.py` | Snapshot 상태와 업데이트 버튼 UI 추가 |
| `tools/projection_paint/blender/properties.py` | Snapshot Source·Revision·State 속성 추가 |
| `tests/shared/` | Transfer Snapshot 불변성과 Provider 격리 테스트 |
| `tests/modifier/` | Snapshot Provider·독립 Undo와 지연 복구 회귀 |
| `tests/projection_paint/` | Update Transaction·Paint 보존·독립 Undo 회귀 |
| `tests/blender_gui/` | Update·Workspace 왕복·Undo·Redo·성능 회귀 |
| `docs/` | 제품 경계, 패키지 구조, ADR, 테스트와 Release Note 갱신 |

Align Source와 Runtime, Workspace Factory, OBJ 입출력과 Small GICP는 수정 대상이 아니다.

## 16. 단계별 구현 계획

### 단계 0. 기준선과 실패 회귀 고정

- 현재 공용 Object Pointer와 Dataset Revision 동기화 동작을 Fixture로 기록한다.
- Painting Undo에서 Modifier 전체 Reconcile이 실행되는 현재 문제를 실패 테스트로 고정한다.
- PP Camera Runtime과 Marker가 중복 복구되는 호출 수를 고정한다.
- 현재 실패 중인 Modifier Global Undo Background 검사의 Timeline 사전 조건을 복구한다.
- 593 Camera·8K Base Resolution 성능 기준선을 기록한다.

완료 조건: 현재 결합과 성능 병목, 기존 Undo 정상 동작을 자동으로 재현할 수 있어야 한다.

### 단계 1. 중립 Transfer 계약

- `ProjectionColmapSnapshot` 불변 값 모델을 구현한다.
- Modifier Provider Registry를 도구 독립적으로 구현한다.
- Blender ID 없는 Camera·Image·Point 전달을 검증한다.
- Snapshot 생성 중 Revision 변경을 감지한다.

완료 조건: Modifier 확정 Reconstruction에서 PP가 소비 가능한 읽기 전용 Snapshot을 얻어야 한다.

### 단계 2. Projection Painting 독립 Resource

- 기존 Legacy PP Camera·Point Builder를 현재 계약에 맞게 복구한다.
- PP Snapshot Property, 소유 태그와 Manifest를 추가한다.
- PP 전용 Camera·Point·Axis를 Staging으로 생성한다.
- Modifier Resource와 Object/Data Pointer가 모두 다른지 검증한다.

완료 조건: 같은 입력에서 두 도구가 독립 Blender Resource를 가져야 한다.

### 단계 3. 업데이트 Transaction과 UI

- Dataset Panel에 상태와 업데이트 버튼을 추가한다.
- Snapshot 생성, Source 재검증, Staging과 원자 Commit을 구현한다.
- 업데이트 전후 Paint 자산 불변 검사를 추가한다.
- 활성 Marker·Stencil의 분리와 재연결 Rollback을 구현한다.

완료 조건: 업데이트 실패·취소 시 기존 PP Snapshot과 Paint 데이터가 그대로 유지돼야 한다.

### 단계 4. 실시간 공용 연결 제거

- Modifier 편집의 공용 Dataset 자동 Commit을 제거한다.
- Projection Painting의 Dataset Revision Listener와 자동 Binding을 제거한다.
- Point Scale·Frustum·Axis를 도구별 속성으로 되돌린다.
- View Layer가 두 독립 Root를 각각 표시하도록 변경한다.

완료 조건: Modifier 편집 뒤 PP Snapshot과 Blender ID가 업데이트 버튼 전까지 변하지 않아야 한다.

### 단계 5. Undo Lifecycle 최소 격리

- Undo 전 두 도구의 직접 Pointer Cache를 안전하게 폐기한다.
- 활성 Workspace·Session의 도구만 Undo 후 전체 Runtime을 복구한다.
- 비활성 도구는 재진입 시 `ensure_runtime()`으로 복구한다.
- PP Camera·Marker·Paint 복구를 단일 Pipeline으로 합친다.
- 별도 Undo Domain Stack이나 Ctrl+Z Keymap을 추가하지 않는다.

완료 조건: Painting Undo의 Modifier 전체 Reconcile은 0회이고 PP Runtime 복구는 한 번이어야 한다.

### 단계 6. Legacy 마이그레이션

- `0.2.0` 공용 Resource를 Modifier 소유로 승격한다.
- 기존 Paint 자산을 Snapshot 없음 상태에서도 보존한다.
- 통합 전 PP Resource를 독립 Legacy Snapshot으로 검증한다.
- `Scene.colmap_dataset`을 읽기 전용 Migration 입력으로 제한한다.

완료 조건: 기존 `.blend`의 Modifier 편집과 Paint Texture가 손실되지 않아야 한다.

### 단계 7. 전체 QC와 배포

- Source·Background·GUI·격리 설치본 전체 회귀를 실행한다.
- 실제 사용자 데이터에서 Modifier Import → PP Update → Paint → Undo·Redo를 검증한다.
- Modifier 추가 편집 뒤 PP 불변과 수동 재Update를 검증한다.
- 실제 Blender 사용자 Repository에 새 패키지를 설치한다.
- MCP 성능 측정 후 임시 로그와 Probe를 제거한다.

완료 조건: 아래 승인 조건과 설치본 실데이터 QC를 모두 통과해야 한다.

## 17. 테스트 계획

### 17.1 Snapshot Transfer

- TXT·BIN, Legacy·Modern Layout
- 비연속 Camera·Image·Point3D ID
- `PINHOLE`, `SIMPLE_PINHOLE`과 미지원 Camera Model 차단
- 한글·공백·중첩 Image 경로
- Camera Pose·Intrinsics·Point 위치·색상 값 일치
- Blender ID와 Modifier Runtime 객체 미포함
- Snapshot 도중 Modifier Revision 변경 감지

### 17.2 Projection Painting 업데이트

- Snapshot 없음 상태 UI
- 최초 업데이트와 재업데이트
- 같은 Modifier Revision 재업데이트
- Camera 감소·증가와 Point 변경
- 다른 Modifier Dataset UUID로 교체
- 업데이트 취소와 Staging 실패 Rollback
- 기존 current IMAGE_ID 유지와 제거된 IMAGE_ID 대체
- Paint Target·Layer·Texture·Mask·Composite Hash 불변
- PP 전용 Object/Data Pointer와 소유 태그

### 17.3 Undo·Redo

- Painting Stroke Undo·Redo에서 Modifier Reconcile 0회
- Modifier 편집 Undo·Redo에서 PP Snapshot 값·Pointer 불변
- PP Layer·Mask·Adjustment Undo·Redo
- PP Snapshot 업데이트 자체의 Blender Undo·Redo
- 일반 Blender Undo에서 두 도구 전체 Rebuild 0회
- 비활성 도구 재진입 시 Lazy Runtime 복구
- Undo 뒤 이전 Blender ID Pointer 미사용
- 연속 Undo·Redo와 Workspace 왕복

### 17.4 Marker·Workspace

- Modifier 활성 시 Modifier Marker만 존재
- PP 활성 시 PP Snapshot Marker만 존재
- 일반·Align Workspace에서 도구 Marker 없음
- PP Update 중 Marker Detach·Attach와 실패 Rollback
- 두 독립 Root의 View Layer 포함·제외
- 같은 Scene 다중 Window Marker 사용권 차단

### 17.5 실제 사용자 흐름

```text
1. Modifier에서 Dataset Import
2. Projection Painting 열기
3. COLMAP 데이터 업데이트
4. PP Camera Review·F7·Stencil 확인
5. 8K Base Resolution Texture Paint Stroke
6. Undo·Redo
7. Modifier 전체 Reconcile 미실행 확인
8. Modifier로 이동해 Camera·Point 편집
9. Modifier Undo·Redo
10. 기존 PP Camera·Point·Paint 데이터 불변 확인
11. Projection Painting 재진입
12. 업데이트 가능 상태 확인
13. COLMAP 데이터 다시 업데이트
14. 새 Camera·Point 반영과 Paint 데이터 보존 확인
```

### 17.6 성능 승인

- Painting Undo의 Modifier 전체 Reconcile: 0회
- 공용 Dataset Binding: 0회
- PP Camera Runtime 재해석: 최대 1회
- PP Marker 검증·연결: 최대 1회
- Paint State Reconcile: 1회
- 593 Camera Update의 소요 시간과 Peak Memory 기록
- 8K Image Undo 10회 중앙값·95백분위 변경 전후 비교
- Update 이후 Painting Undo 중 Snapshot 복사 작업: 0회

## 18. 메모리와 성능 관리

독립 Resource는 Camera·Point 데이터를 두 벌 보유하므로 공용 구조보다 메모리를 더 사용한다. 이는 통합 전 독립 Import 구조와 같은 비용이다.

다음 제한을 적용한다.

- Transfer Snapshot은 Python Object를 Camera·Point마다 중복 생성하지 않고 연속 배열과 불변 Record를 사용한다.
- PP Staging은 Chunk 단위로 생성한다.
- 새 Snapshot 검증 전에는 기존 PP Resource를 삭제하지 않는다.
- Commit 후 이전 PP Resource와 중간 Transfer Buffer를 즉시 해제한다.
- Paint Image와 Layer Pixel은 Snapshot 업데이트를 위해 복사하지 않는다.
- PP에서 사용하지 않는 Modifier Point2D·Track·Rig·Frame 세부 데이터는 전달하지 않는다.
- Update 중 예상 Peak Memory를 사전 계산하고 안전 한계를 넘으면 기존 Snapshot을 유지한 채 중단한다.

## 19. 위험과 대응

| 위험 | 대응 |
| --- | --- |
| 사용자가 업데이트를 잊음 | 현재 Modifier Revision과 가져온 Revision을 비교해 `업데이트 가능` 표시 |
| 자동 반영이 사라져 혼란 | Panel에 `마지막 업데이트 Revision`과 독립 Snapshot 설명 표시 |
| Camera·Point 두 벌로 메모리 증가 | PP 필요 필드만 연속 배열로 복사하고 중간 Buffer 즉시 해제 |
| Update Staging 중 Peak Memory 증가 | 사전 추정, Chunk 생성, 한도 초과 시 기존 Snapshot 유지 |
| Update 실패로 Paint 데이터 손실 | Paint 자산을 Transaction 교체 대상에서 제외하고 연결만 복구 |
| Blender Undo가 Update를 되돌림 | Update를 하나의 표준 Undo Step으로 기록하고 Snapshot Manifest까지 함께 검증 |
| 비활성 도구 Runtime이 없음 | 오류로 처리하지 않고 Workspace 재진입 시 Lazy Recovery |
| Undo 뒤 오래된 Pointer 사용 | 두 도구 Cache는 항상 Undo 전에 폐기 |
| Legacy 공용 Resource 잘못 승격 | UUID·Source·수량·소유 Metadata가 모두 일치할 때만 Modifier 소유로 전환 |
| PP Legacy 데이터 자동 삭제 | 소유권이 모호하면 보존하고 새 Update 성공 뒤 검증된 PP Resource만 교체 |
| 도구 사이 직접 의존 재발 | 중립 Transfer 계약만 허용하는 Architecture 테스트 추가 |

## 20. 완료 조건

다음을 모두 만족해야 구조변경 완료로 판단한다.

1. Modifier와 Projection Painting이 같은 Camera·Point Blender ID를 공유하지 않는다.
2. Modifier가 COLMAP 파일 Import·편집·Export의 유일한 권위 도구다.
3. Projection Painting에 `COLMAP 데이터 업데이트` 버튼이 존재한다.
4. 버튼을 누르기 전에는 Modifier 변경이 PP Snapshot에 반영되지 않는다.
5. 업데이트가 성공한 경우에만 PP Snapshot과 Resource가 원자 교체된다.
6. 업데이트 실패·취소 시 기존 PP Snapshot과 Paint 데이터가 유지된다.
7. Paint Target, Layer, Texture, Mask, Adjustment와 Composite가 업데이트 전후 보존된다.
8. Modifier Undo·Redo 뒤 PP Snapshot 값과 Blender ID가 변하지 않는다.
9. Painting Undo·Redo에서 Modifier 전체 Runtime Reconcile이 실행되지 않는다.
10. 비활성 도구는 Undo 뒤 Cache만 폐기하고 재진입 시 복구한다.
11. 별도 Ctrl+Z Stack과 Undo Domain Router가 추가되지 않는다.
12. PP Camera Runtime·Marker·Paint 복구가 각각 한 번만 실행된다.
13. Modifier와 PP 표시 설정이 독립적으로 동작한다.
14. 현재 활성 도구의 Marker만 Scene에 존재한다.
15. 일반 Blender Workspace와 Align에서 두 COLMAP Root가 보이지 않는다.
16. 기존 `0.2.0` Modifier 편집 결과와 Paint 자산을 손실 없이 이전한다.
17. 통합 전 Legacy PP Resource를 명확한 소유권 조건에서 보존할 수 있다.
18. 현재 실패 중인 Modifier Global Undo Background 검사가 실제 Undo까지 통과한다.
19. 593 Camera·8K 실데이터 성능과 Peak Memory 결과가 기록된다.
20. Source, 격리 설치본과 실제 사용자 설치본 QC가 통과한다.
21. Debug 로그, 임시 Probe와 Migration 중간 Resource가 배포물에 남지 않는다.

## 21. 최종 사용자 흐름

```text
1. Modifier에서 COLMAP 데이터 Import
2. 필요한 Point·Camera 편집
3. Projection Painting Workspace로 이동
4. COLMAP 데이터 업데이트 버튼 실행
5. 독립 Camera Review·Stencil·Texture Painting
6. Modifier에서 추가 편집해도 기존 PP 작업은 그대로 유지
7. 새 데이터가 필요할 때만 다시 업데이트
```

이 구조는 Modifier와 Projection Painting을 실시간 데이터 Pipeline으로 묶지 않는다. Modifier는 Snapshot을 제공하고 Projection Painting은 사용자가 선택한 시점의 복사본을 독립적으로 사용하는 두 도구로 유지한다.
