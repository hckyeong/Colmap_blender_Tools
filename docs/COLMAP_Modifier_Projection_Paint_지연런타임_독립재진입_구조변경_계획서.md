# COLMAP Modifier 지연 Runtime·Projection Painting 독립 재진입 구조변경 계획서

## 1. 문서 정보

- 작성일: 2026-08-30
- 대상 제품: `COLMAP Tools` Blender Extension
- 대상 Blender: 5.2 LTS 이상
- 기준 버전: `0.3.8`
- 목표 버전 후보: `0.3.9`
- 대상 도구: `COLMAP Modifier`, `Projection Painting`
- 독립 유지 도구: `COLMAP Align`
- 문서 상태: 구현 전 구조변경 계획
- 재현 파일: `C:\Users\gksru\Desktop\Untitled.blend`
- 선행 결정: `ADR-0008: Projection Painting 수동 독립 Snapshot`

## 2. 쉬운 결론

Projection Painting은 `.blend`에 저장된 자기 COLMAP Snapshot으로 바로 실행하고, 파일을 열 때 Modifier 원본 COLMAP을 다시 읽지 않는다.

Modifier의 원본 데이터가 필요한 시점에만 Modifier Runtime을 복원한다.

```text
.blend 열기
├─ Modifier 영구 정보: 경로·UUID·Revision·편집 이력만 확인
├─ Modifier 대용량 Runtime: 아직 만들지 않음
└─ Projection Painting Snapshot: 저장된 독립 Camera·Point로 바로 사용

Modifier Workspace 진입
└─ Modifier Runtime을 진행률과 함께 지연 복원

Projection Painting의 COLMAP 데이터 업데이트
└─ Modifier Runtime을 진행률과 함께 지연 복원
   → 확정값 Snapshot 생성
   → PP 전용 Camera·Point로 원자 교체
```

Modifier와 Projection Painting의 관계는 실시간 공유가 아니라 아래와 같다.

```text
Modifier 원본·편집 데이터
          │
          │ 사용자가 업데이트 버튼을 누른 시점에만 값 복사
          ▼
Projection Painting 독립 Snapshot
```

- Modifier 변경은 기존 PP Snapshot에 자동 반영되지 않는다.
- PP의 Painting, Layer와 Undo는 Modifier Runtime을 필요로 하지 않는다.
- Blender를 다시 열어도 READY PP Snapshot이 있으면 Modifier 복원 완료를 기다리지 않는다.
- 최신 Modifier 데이터가 필요할 때만 `COLMAP 데이터 업데이트`를 누른다.

## 3. 실제 재현 결과

### 3.1 파일 로드 뒤 약 30초 정지 원인

`Untitled.blend`를 Blender 5.2 실제 GUI 프로세스로 열어 Main Thread Stack을 추적했다.

- `.blend` 자체 로드는 약 2.6초에 완료됐다.
- Projection Painting 진입 준비 단계 자체는 약 0.031초였다.
- Painting 완료 Timer가 약 29.7초 동안 실행되지 못했다.
- 그 시간 동안 Main Thread는 Modifier의 저장 Runtime을 복원하고 있었다.

확인된 호출 흐름은 다음과 같다.

```text
tools/modifier/blender/state_handlers.py
└─ process_pending_load_restore()
   └─ ensure_runtime()
      ├─ 원본 COLMAP 전체 재해시
      ├─ Camera·Image·Point3D 전체 재파싱
      ├─ Reconstruction 전체 무결성 검사
      ├─ 관계 Index 전체 생성
      └─ 저장된 편집 History 재생
```

사용자 데이터는 Camera 593개, Image 593개, Point3D 약 709,048개다. 이 전체 작업이 Blender Main Thread에서 동기 실행되어 다른 App Timer와 UI 입력을 막았다.

따라서 느린 원인은 Projection Painting Layer 준비나 Align 프로젝트 복원이 아니다. 파일 로드 직후 조건 없이 시작되는 Modifier Runtime 전체 복원이 직접 원인이다.

### 3.2 현재 불필요한 복원 경로

현재 `load_post` 흐름은 활성 Workspace와 PP Snapshot 상태를 구분하지 않는다.

```text
invalidate_loaded_state()
└─ _schedule_load_restore()
   └─ process_pending_load_restore()
      └─ ensure_runtime(scene)
```

Projection Painting 전용 Snapshot이 이미 READY여도 같은 복원이 실행된다.

### 3.3 업데이트 버튼에도 남아 있는 동기 복원

현재 `Projection Painting → COLMAP 데이터 업데이트`의 `_prepare()`는 진행 UI를 시작하기 전에 아래 호출을 실행한다.

```text
build_snapshot(context, "modifier")
└─ Modifier Provider
   └─ ensure_runtime(scene)
```

파일 로드의 자동 복원만 제거하면, 사용자가 업데이트 버튼을 누르는 순간 같은 약 30초 작업이 진행률 없이 다시 UI를 막는다. 그러므로 Load Handler와 업데이트 Provider를 함께 변경해야 한다.

### 3.4 별도로 확인된 Painting 진입 실패

Modifier 복원 지연과 별개로 현재 파일에는 다음 Painting 진입 오류도 존재한다.

```text
Properties Space Context에 "OBJECT" 설정
→ 현재 Area의 동적 Enum에 OBJECT가 없음
→ TypeError
→ Paint 진입 Timer 종료
→ paint_enabled는 True지만 Target은 OBJECT Mode에 남음
```

이 상태에서 `Reconnect Stencil Texture`를 누르면 실제 Texture Paint Session이 없으므로 다음 메시지가 나온다.

```text
전용 작업공간의 실제 Texture Paint Target이 아닙니다.
```

활성 Brush가 없는 파일에서 Stencil 연결이 실패하는 문제도 함께 확인됐다. 구조변경 완료 기준이 실제 Painting 재진입이므로 이 두 오류를 같은 배포 범위에서 수정하되, Modifier 지연 복원과는 서로 다른 원인으로 관리한다.

## 4. 목표

1. `.blend` 로드 Handler에서 대용량 Modifier Runtime을 생성하지 않는다.
2. READY PP Snapshot이 있으면 Modifier Runtime 없이 Projection Painting Workspace와 Painting Mode에 진입한다.
3. Modifier Runtime은 Modifier Workspace 진입 또는 `COLMAP 데이터 업데이트` 요청 시에만 만든다.
4. 지연 복원은 진행률, 취소, Source 재검증과 실패 상태를 제공한다.
5. `COLMAP 데이터 업데이트`의 진행창은 Modifier 원본 복원 단계부터 표시한다.
6. Modifier Runtime 복원 중에도 기존 READY PP Snapshot과 Paint 자산을 변경하지 않는다.
7. Modifier와 PP는 Blender Object, Camera Data, Mesh와 Runtime Pointer를 공유하지 않는다.
8. Modifier Provider는 중립 `shared/colmap_transfer` 계약을 통해서만 값을 전달한다.
9. Modifier Workspace 재진입 시 저장된 편집 History, Camera Review와 Pending 상태를 복원한다.
10. Source가 없거나 변경됐으면 기존 PP Snapshot을 유지하고 업데이트만 명확히 실패시킨다.
11. Painting 진입 UI는 실제 Texture Paint Session이 ACTIVE일 때만 활성 상태로 표시한다.
12. Painting 진입 실패는 모든 상태를 Rollback하고 최초 원인을 사용자에게 유지해 표시한다.

## 5. 제외 범위

- Modifier 변경의 Projection Painting 자동 동기화
- Modifier와 PP의 공용 Camera·Point Blender Resource
- Projection Painting의 COLMAP 파일 직접 Import 복원
- 별도 Ctrl+Z Stack 또는 Undo Domain Router 추가
- Paint Image 해상도와 `Use Base Resolution` 의미 변경
- COLMAP Parser 결과나 편집 알고리듬 변경
- Align Runtime, Project Restore, OBJ와 Small GICP 변경
- Python Background Thread에서 `bpy` 데이터 접근
- Source 오류를 숨기기 위한 캐시 우회 또는 검증 생략

## 6. 목표 상태 모델

### 6.1 Modifier 영구 상태와 Runtime 분리

`.blend`에 저장되는 Modifier 권위 정보는 유지한다.

```text
Scene.colmap_import
├─ dataset_uuid
├─ source_fingerprint
├─ active_model_directory
├─ active_images_directory
├─ input_format·layout
├─ edit_revision·history_hash·edit_history_json
├─ Camera Review Manifest
└─ 저장된 Blender Point·Camera Resource
```

다음 값은 파일 로드 뒤 자동으로 만들지 않는 재생성 가능 Runtime이다.

```text
DatasetRuntimeState
├─ baseline_reconstruction
├─ current_reconstruction
├─ RelationshipIndexes
├─ 역직렬화된 편집 Operation
└─ ImagePathManifest
```

### 6.2 Modifier Runtime 상태

Scene의 영구 데이터와 별도로 Scene별 Runtime 복원 상태를 관리한다.

| 상태 | 의미 |
| --- | --- |
| `COLD` | 영구 Metadata와 Blender 표시 데이터는 있으나 Python Reconstruction은 없음 |
| `RESTORING` | 원본 읽기·검증·History Replay를 증분 실행 중 |
| `READY` | Scene Token과 일치하는 `DatasetRuntimeState`가 설치됨 |
| `STALE` | Source나 Scene Revision이 복원 시작 Token과 달라짐 |
| `ERROR` | 원본 누락·변경·무결성 오류로 복원하지 못함 |

이 상태는 대형 Reconstruction을 `.blend`에 중복 저장하기 위한 값이 아니다. 현재 실행 중 복원 수명주기를 표현하는 가벼운 Runtime 상태다.

### 6.3 Projection Painting Snapshot 상태

기존 `NONE`, `UPDATING`, `READY`, `STALE`, `ERROR` 계약을 유지한다.

- READY 판정은 PP 자기 Manifest와 PP 소유 Blender ID만 사용한다.
- Modifier Runtime의 `COLD` 또는 `ERROR` 상태는 기존 READY PP Snapshot을 무효화하지 않는다.
- Modifier Metadata Revision이 달라지면 `업데이트 가능`만 표시한다.
- 자동 업데이트나 자동 Runtime 복원은 실행하지 않는다.

## 7. Runtime 복원 트리거

| 사용자 상황 | Modifier Runtime 동작 | PP 동작 |
| --- | --- | --- |
| 일반 Workspace에서 파일 열기 | `COLD` 유지 | 변경 없음 |
| Align Workspace에서 파일 열기 | `COLD` 유지 | 변경 없음 |
| Projection Paint Workspace에서 파일 열기 | `COLD` 유지 | READY Snapshot으로 재진입 |
| Modifier Workspace에서 파일 열기 | Workspace 준비 뒤 지연 복원 시작 | 변경 없음 |
| Modifier Workspace 직접 진입 | 지연 복원 시작 | 변경 없음 |
| PP에서 업데이트 버튼 실행 | Provider 지연 복원 시작 | 기존 Snapshot 유지 |
| PP에서 Painting·Layer·Undo 실행 | 복원하지 않음 | PP 독립 Runtime만 사용 |
| Modifier 편집·Export 실행 | READY가 아니면 공통 복원 요청 경계 사용 | 변경 없음 |

`poll()`과 Panel Draw에서는 Dataset UUID, 경로 존재 여부와 상태 문자열만 확인한다. 대용량 파일 해시, 파싱과 `ensure_runtime()`을 호출하지 않는다.

## 8. 증분 Modifier Runtime 복원 Job

### 8.1 새 Core Job

`tools/modifier/core/runtime_restore_job.py`에 Blender ID를 사용하지 않는 증분 Job을 둔다.

```text
ModifierRuntimeRestoreJob
├─ VALIDATE_SAVED_TOKEN
├─ LOAD_SOURCE
│  └─ 기존 ImportLoadJob 재사용
├─ DESERIALIZE_HISTORY
├─ BUILD_BASE_INDEXES
├─ REPLAY_HISTORY
├─ VALIDATE_RESULT
└─ BUILD_RUNTIME_STATE
```

`ImportLoadJob`의 기존 증분 Source Hash, TXT/BIN Read, 무결성 검사와 Image 경로 검증을 재사용한다. 새 복원 경로에서 동기 `load_reconstruction()`을 다시 구현하지 않는다.

### 8.2 History Replay 증분화

현재 `_make_state()`는 `replay_history()`와 관계 Index 생성을 한 번에 실행한다. 대형 Point3D에서 이 구간도 Main Thread를 오래 점유할 수 있으므로 다음을 안전 경계로 나눈다.

- 초기 Relationship Index를 Camera, Image, Point Track 단위로 증분 구성
- 저장된 Operation을 Revision 순서대로 한 개씩 검증
- Operation 내부 대형 Point·관측 갱신을 제한된 Chunk로 처리
- 각 Operation 결과 수량과 저장 결과를 기존과 동일하게 검증
- 마지막에 `edit_revision`, `index_revision`, `history_hash` 일치 확인

기존 `replay_history()`의 결과 의미와 오류 문구는 유지한다. 증분 Job과 기존 동기 호출이 같은 Fixture에서 동일한 Reconstruction과 Index를 생성하는 계약 테스트를 추가한다.

### 8.3 시간 예산

GUI에서는 한 Tick의 작업 예산을 제한한다.

```text
권장 시작값
├─ Event Timer 간격: 0.01초
├─ Core 작업 예산: Tick당 20~25ms
└─ 긴 내부 반복: 레코드 Chunk 상한 병행
```

시간 예산만 검사하고 내부 단일 반복이 수초 동안 끝나지 않는 구조는 허용하지 않는다. 709,048 Point3D Fixture에서 각 Phase의 최대 Tick 지연을 기록한다.

### 8.4 원자 설치

복원 Job은 완료 전까지 전역 `_STATES`에 부분 Runtime을 넣지 않는다.

```text
1. 시작할 때 Scene의 UUID·Revision·Fingerprint·파일 경로 Token 캡처
2. 별도 Job 내부에서 Source와 History 복원
3. 완료 직전 현재 Scene Token 재검증
4. 모두 같을 때만 install_state()
5. 다르면 결과 폐기 후 STALE
```

취소, 파일 교체, Undo, Source 변경과 Workspace 이탈 시 부분 결과를 폐기한다.

## 9. 파일 Load 수명주기 변경

### 9.1 `load_post` 책임 축소

`tools/modifier/blender/state_handlers.py`의 `invalidate_loaded_state()`는 다음만 수행한다.

- 이전 파일의 Import·Export·Review·Edit Runtime과 Pointer Cache 폐기
- 이전 복원 Job과 Timer 취소
- Axis Legacy Metadata Migration
- 관리 Collection의 View Layer 가시성 동기화 예약
- Scene Token을 `COLD`로 기록

다음 작업은 수행하지 않는다.

- `_schedule_load_restore()` 자동 호출
- `process_pending_load_restore()`에서 `ensure_runtime()` 호출
- 비활성 Modifier Camera Review Session 생성
- 비활성 Modifier Pending Filter 파생 Runtime 생성
- 비활성 Modifier Timeline Marker 연결

### 9.2 기존 자동 복원 Timer 제거

`_PendingLoadRestore`, `_schedule_load_restore()`와 `process_pending_load_restore()`의 책임을 공통 지연 복원 조정기로 이동한다.

단순히 Timer 이름만 바꾸고 다음 Tick에서 같은 동기 `ensure_runtime()`을 호출하는 방식은 완료로 인정하지 않는다.

### 9.3 Modifier Workspace가 현재 활성인 파일

파일이 Modifier Workspace 상태로 저장된 경우 Workspace Session 복원은 계속 수행한다. 다만 순서는 아래로 변경한다.

```text
Workspace·Screen 재연결
→ N 패널과 전용 View Layer 준비
→ Session UI 활성
→ Modifier Runtime RESTORING 시작
→ 완료 후 Review·Pending·Marker 연결
```

원본이 누락되거나 변경돼도 Workspace와 저장된 Blender 표시 데이터는 유지한다. 편집·Export와 Camera Review 입력만 비활성화하고 정확한 Runtime 오류를 표시한다.

## 10. Modifier Workspace 지연 복원

`_complete_workspace_activation()`에서 동기 `_rehydrate_viewport_runtime()`을 바로 실행하지 않는다.

새 흐름은 다음과 같다.

```text
Layout·View Layer·Sidebar 완료
→ Workspace ACTIVE
→ Runtime 상태 확인
   ├─ READY: Review·Pending·Marker 즉시 연결
   ├─ COLD: 증분 복원 Job 시작
   └─ ERROR: 저장 표시 데이터만 유지하고 입력 잠금
```

N 패널은 복원 중에도 유지한다.

- 상태: `Modifier 데이터 복원 중 42.3%`
- 단계: `COLMAP Reconstruction 해석 중`
- Camera·Point 수: 저장 Metadata 값 표시
- Edit, Review, Export: Runtime READY 전까지 비활성화
- Workspace 이동: Job 취소 또는 안전한 백그라운드 중지 뒤 정상 Handoff

Runtime READY가 되면 현재 Session과 Scene Token을 다시 확인하고 다음 순서로 한 번만 연결한다.

1. Review Session 생성
2. Pending Filter 복구
3. Camera Review Background 복구
4. Modifier Marker Lease 획득과 Marker Attach
5. UI 상태 READY 전환

## 11. 중립 Snapshot Provider 확장

### 11.1 동기 Provider 한계

현재 `shared/colmap_transfer.registry.build_snapshot()`은 Provider Callback 하나를 동기 호출한다. 지연 Runtime에서는 이 호출이 Source 전체 복원을 포함할 수 있으므로 GUI 진행 상태를 제공할 수 없다.

### 11.2 목표 Provider 계약

중립 계층에 도구 의미를 모르는 증분 Snapshot Build Handle 계약을 추가한다.

```text
SnapshotBuildHandle
├─ advance(work_budget_ms)
├─ progress
├─ done
├─ result
├─ cancel()
└─ validate_source_token(context)
```

공용 계층은 Modifier Runtime Class를 Import하지 않는다. Modifier Provider가 Handle 구현을 등록하고 PP는 Registry를 통해서만 사용한다.

Provider 단계는 다음과 같다.

```text
CHECK_CACHED_RUNTIME
→ RESTORE_MODIFIER_RUNTIME
→ BUILD_CAMERA_VALUES
→ BUILD_IMAGE_VALUES
→ BUILD_POINT_VALUES
→ HASH_TRANSFER_CONTENT
→ VALIDATE_SOURCE_TOKEN
→ DONE
```

Camera, Image와 Point Tuple 생성도 Chunk로 나눠 대형 Point3D를 한 번에 Python Object로 복제하지 않는다.

### 11.3 중복 복원 방지

같은 Scene·Dataset에서 Modifier Workspace와 PP 업데이트가 동시에 Runtime을 요청하면 Scene별 복원 Job 하나만 소유한다.

- 같은 Token의 두 번째 요청은 기존 Job의 상태를 구독한다.
- 다른 Token 요청은 기존 Job을 STALE 처리하고 새 요청을 시작한다.
- 한 소비자의 취소가 다른 활성 소비자의 요청을 임의로 폐기하지 않도록 참조 소유자를 기록한다.
- 파일 Load와 Extension 해제는 모든 소비자를 취소하고 Job을 폐기한다.

## 12. `COLMAP 데이터 업데이트` 흐름 변경

### 12.1 진행 UI 시작 시점

현재 `_prepare()`의 동기 `build_snapshot()` 호출을 제거한다. 먼저 Provider Handle과 진행 UI를 만들고 이후 Timer에서 복원과 Snapshot 생성 단계를 전진시킨다.

```text
버튼 클릭
→ 기존 PP Snapshot 상태 보존
→ snapshot_state = UPDATING
→ 진행창·Modal Timer 시작
→ Modifier Runtime 지연 복원
→ Transfer Snapshot 생성
→ PP Camera·Point Staging 생성
→ Source Token 재검증
→ 원자 Commit
```

### 12.2 통합 진행률 구간

| 구간 | 전체 진행률 권장 범위 |
| --- | --- |
| Modifier Source Hash·Read·Validate | 0~45% |
| Modifier History Replay·Index | 45~60% |
| Transfer Camera·Image·Point 생성 | 60~72% |
| PP Point·Camera Staging | 72~97% |
| Source 재검증·Scene Commit | 97~100% |

실제 완료 작업량 기반 단조 증가 값을 사용한다. Phase가 바뀌어도 백분율이 뒤로 가지 않아야 한다.

### 12.3 기존 Snapshot 보존

업데이트 완료 전까지 아래 값은 변경하지 않는다.

- PP Snapshot UUID·Revision·Manifest
- PP Point·Camera·Axis Blender ID
- Paint Target·Material
- Original·Layer·Mask·Adjustment·Composite
- Paint Image Pixel
- 현재 사용 가능한 Camera Review와 Stencil Source 영구 상태

Commit 직전에만 활성 PP Marker와 Stencil 실행 연결을 분리한다. Commit 또는 재연결이 실패하면 새 Staging을 제거하고 기존 Snapshot 실행 연결을 복구한다.

## 13. Projection Painting 독립 재진입

### 13.1 Dataset 판정

Projection Painting Workspace 진입은 다음 순서로 판단한다.

```text
PP Snapshot READY·Manifest 검증
├─ 성공: PP 소유 Resource로 Camera Runtime 구성
└─ 실패: 업데이트 또는 최초 Snapshot 생성을 안내
```

이 과정에서 Modifier `ensure_runtime()`, Source Hash와 Reconstruction Load를 호출하지 않는다.

### 13.2 Painting Mode 진입 Transaction

현재 `paint_enabled=True`가 진입 완료 전에 설정되어 버튼이 활성처럼 보이는 상태를 없앤다.

```text
IDLE
→ ENTERING
→ Workspace·Target·Material 검증
→ TEXTURE_PAINT Mode 진입
→ 유효 Brush 보장
→ Stencil Session 연결
→ ACTIVE
```

- UI의 활성 표시는 `paint_state == ACTIVE`와 실제 Runtime 소유 조건을 함께 사용한다.
- `ENTERING` 중 Reconnect·Project·Stroke 입력을 비활성화한다.
- 성공 마지막 단계에서만 `paint_enabled=True`를 확정한다.
- 실패하면 Target Mode, 활성 Object, Overlay, Brush·Stencil과 Scene 상태를 진입 전 값으로 복구한다.

### 13.3 Properties Context 안전 설정

`paint_workspace.py`는 Layout이 이미 `COMPLETE`인 재진입마다 Properties Context를 `OBJECT`로 강제 설정하지 않는다.

- Properties Area를 처음 구성하거나 복구할 때만 설정한다.
- 정확한 Window·Screen·Area Context Override에서 실행한다.
- 현재 동적 Enum에 `OBJECT`가 있을 때만 사용한다.
- 사용할 수 없으면 현재 Context를 유지하거나 `VIEW_LAYER`로 안전하게 대체한다.
- 이 보조 UI Context 실패가 실제 Paint Target과 Layer 데이터를 변경하지 않게 한다.

### 13.4 Brush 없음 복구

Texture Paint Mode 진입 뒤 활성 Brush가 없으면 Blender의 현재 Paint Tool 자산에서 기본 `Paint Hard` Brush를 명시적으로 활성화한다.

- 기존 사용자가 선택한 유효 Brush가 있으면 변경하지 않는다.
- Add-on 소유 Legacy Brush만 기존 Migration 규칙을 적용한다.
- Brush 활성화에 실패하면 Stencil만 우회하지 않고 Paint 진입 Transaction 전체를 실패시킨다.

### 13.5 예외 Rollback

Paint 진입 Timer는 `PaintSessionError`, `RuntimeError`, `ValueError`뿐 아니라 Blender RNA 대입에서 발생하는 `TypeError`, `AttributeError`, `ReferenceError`도 정식 실패로 처리한다.

Lifecycle Registry가 예외를 기록하기 전에 Paint Transaction이 먼저 Rollback하고, 최초 오류 메시지를 유지한다. Frame Source 또는 Brush 후속 Callback이 최초 오류를 덮어쓰지 않게 한다.

## 14. Undo·Save·Load 계약

### 14.1 Undo

- Painting Undo는 Modifier Runtime을 복원하지 않는다.
- Modifier가 비활성 상태면 Pointer Cache와 복원 Job Token만 무효화한다.
- PP가 활성 상태면 PP Snapshot Runtime만 한 번 복구한다.
- Modifier Workspace가 활성 상태면 Modifier Runtime READY 여부에 따라 현재 Job을 취소하고 새 Scene Token으로 재복원한다.
- 별도 Ctrl+Z Stack을 만들지 않는다.

### 14.2 Save

- PP Snapshot과 Paint 자산은 기존처럼 `.blend`에 저장한다.
- Modifier Runtime Python 객체는 저장하지 않는다.
- 저장 중 Runtime이 `COLD`여도 오류가 아니다.
- 진행 중 복원·업데이트가 있으면 기존 장기 작업 저장 제한 계약에 따라 완료 또는 취소를 요구한다.

### 14.3 Load

- 모든 이전 Blender ID Pointer Cache와 Job을 폐기한다.
- PP READY Snapshot은 현재 Blender ID에서 재해석한다.
- Modifier는 영구 Token만 검증하고 `COLD`로 둔다.
- 활성 Workspace에 필요한 도구만 다음 UI Tick부터 복원한다.

Blender 공식 문서는 파일 Load 뒤 Add-on Handler를 유지하려면 `persistent` Handler가 필요하다고 설명하며, Undo나 데이터 교체 뒤 오래된 Blender ID Python 참조를 유지하지 말 것을 안내한다. 이번 구조는 Handler를 유지하되 대용량 데이터 변경은 사용자 트리거의 증분 Job으로 분리한다.

## 15. 실패와 Rollback

| 상황 | 처리 |
| --- | --- |
| Modifier Source 파일 없음 | Runtime `ERROR`, 기존 PP Snapshot 사용 유지 |
| Source Fingerprint 변경 | 편집 History 적용 금지, 업데이트 실패와 재Import 안내 |
| 복원 중 Scene Revision 변경 | Job `STALE`, 부분 결과 폐기 |
| 복원 중 다른 파일 Load | Generation 불일치로 즉시 취소 |
| 복원 중 Workspace 이탈 | 해당 소비자 요청 취소, 설치 전 부분 결과 폐기 |
| PP 업데이트 취소 | Provider·PP Staging 정리, 기존 READY Snapshot 복구 |
| PP Commit 실패 | 새 PP Blender ID만 제거, 기존 Snapshot 유지 |
| Marker·Stencil 재연결 실패 | Commit Rollback 후 기존 실행 연결 복구 |
| Paint Properties Context 오류 | Paint 진입 전체 Rollback, 정확한 오류 표시 |
| 활성 Brush 없음·생성 실패 | Paint 진입 실패, 가짜 활성 상태 금지 |
| 메모리 사전 한도 초과 | 기존 Snapshot·Runtime 유지, 예상 Peak와 해결 방법 표시 |

## 16. 예상 변경 파일

| 경로 | 변경 내용 |
| --- | --- |
| `tools/modifier/core/runtime_restore_job.py` | 증분 Source Load·History Replay·Index·Runtime 결과 Job 추가 |
| `tools/modifier/core/edit_replay.py` | 기존 결과와 동일한 증분 Replay 경계 분리 |
| `tools/modifier/core/indexes.py` | 대형 관계 Index의 증분 Build 경계 추가 |
| `tools/modifier/blender/runtime_state.py` | 동기 `ensure_runtime`과 지연 Job 설치 계약 분리 |
| `tools/modifier/blender/state_handlers.py` | Load 직후 자동 전체 복원 제거, `COLD` 상태와 Job 취소 연결 |
| `tools/modifier/blender/modifier_workspace.py` | Workspace 준비 뒤 복원 Job 시작·상태·Review 연결 |
| `tools/modifier/blender/projection_snapshot_provider.py` | 증분 Provider Handle과 Source Token 검증 구현 |
| `tools/modifier/blender/lifecycle.py` | Scene별 복원 조정기 등록·해제와 실패 Rollback |
| `shared/colmap_transfer/contracts.py` | 도구 독립 Snapshot Build Progress·Handle 계약 추가 |
| `shared/colmap_transfer/registry.py` | 증분 Provider 등록·시작·취소·결과 조회 추가 |
| `tools/projection_paint/blender/snapshot_update.py` | Provider 복원부터 PP Commit까지 하나의 Modal 진행률로 연결 |
| `tools/projection_paint/blender/paint_workspace.py` | Properties Context 재진입 안전 설정 |
| `tools/projection_paint/blender/paint_session.py` | 실제 ACTIVE 기반 UI, 넓은 RNA 예외 Rollback과 최초 오류 보존 |
| `tools/projection_paint/blender/stencil_session.py` | 활성 Brush 없음 시 기본 Brush 활성화와 명확한 실패 |
| `tools/projection_paint/blender/panels.py` | ENTERING 입력 잠금과 실제 Runtime 기반 Reconnect 활성 조건 |
| `tests/modifier/core/` | 증분 Runtime 복원과 동기 결과 동등성 회귀 |
| `tests/modifier/blender_background/` | 비활성 Load 무복원, 활성 Modifier 지연 복원 회귀 |
| `tests/projection_paint/blender_background/` | PP 독립 재진입, 업데이트 통합 진행률·Rollback 회귀 |
| `tests/blender_gui/` | 실파일 Load·Painting·Modifier·업데이트 성능 회귀 |
| `docs/` | ADR, 테스트 명령, 구현 현황과 Release Note 갱신 |

Align Source, Small GICP Runtime, Align Workspace와 OBJ 입출력은 수정하지 않는다.

## 17. 단계별 구현 계획

### 단계 0. 기준선과 실패 회귀 고정

- `Untitled.blend`의 파일 로드, Painting 진입과 Main Thread Stack을 기준선으로 기록한다.
- `process_pending_load_restore → ensure_runtime` 호출과 약 29.7초 Timer 정지를 실패 회귀로 고정한다.
- PP 준비 단계 약 0.031초와 Modifier 593 Camera·709,048 Point3D 수치를 기록한다.
- Properties Context `OBJECT` TypeError와 활성 Brush 없음 상태를 독립 실패 테스트로 고정한다.

완료 조건: 느림과 Painting 실패가 서로 다른 원인임을 자동 검증할 수 있어야 한다.

### 단계 1. Modifier Core 증분 Runtime 복원

- 기존 `ImportLoadJob`을 저장 Source 복원에 재사용한다.
- 증분 Index Build와 History Replay Job을 구현한다.
- 기존 `ensure_runtime()` 결과와 값·Revision·History Hash가 같은지 검증한다.
- 취소·Stale·Source 변경에서 `_STATES`가 바뀌지 않는지 확인한다.

완료 조건: 대형 Source를 제한된 Tick 예산으로 복원하고 마지막에만 Runtime을 설치해야 한다.

### 단계 2. Load Handler의 자동 전체 복원 제거

- `load_post`에서 `_schedule_load_restore()`를 제거한다.
- 비활성 Scene은 `COLD` 상태와 영구 Metadata만 유지한다.
- 가시성, Workspace Message Bus와 Marker 비활성 계약은 유지한다.
- 파일을 연 직후 Modifier Core Reader·Index·Replay 호출이 0회인지 검증한다.

완료 조건: 일반·Align·PP Workspace로 파일을 열 때 Modifier 전체 복원이 실행되지 않아야 한다.

### 단계 3. Modifier Workspace 지연 복원

- Layout 완료와 Runtime READY를 별도 상태로 분리한다.
- N 패널 진행률과 입력 잠금을 추가한다.
- READY 뒤 Review·Pending·Background·Marker를 한 번만 연결한다.
- Source 오류 상태에서도 Workspace와 저장 표시 데이터를 유지한다.

완료 조건: Modifier 진입은 UI를 멈추지 않고 진행률을 표시하며 기존 편집 결과를 복원해야 한다.

### 단계 4. 증분 Transfer Provider와 PP 업데이트

- 중립 Registry에 Snapshot Build Handle을 추가한다.
- Modifier Restore, Transfer 값 생성과 PP Staging 진행률을 하나로 연결한다.
- 업데이트 버튼 클릭 직후 진행 UI가 먼저 표시되는지 확인한다.
- 취소·Source 변경·Commit 실패에서 기존 PP Snapshot과 Paint Pixel을 보존한다.

완료 조건: `COLMAP 데이터 업데이트` 중 약 30초 복원이 필요해도 Blender가 멈춘 것처럼 보이지 않아야 한다.

### 단계 5. Projection Painting 독립 재진입 안정화

- PP READY Snapshot만으로 Camera Runtime과 Workspace를 복구한다.
- Properties Context 설정을 현재 Area Enum에 맞게 안전화한다.
- Paint 진입 상태와 UI Toggle을 실제 ACTIVE 조건으로 통일한다.
- Brush 없음 복구와 모든 진입 오류 Rollback을 구현한다.
- Reconnect·Project 버튼의 Poll을 실제 Paint Runtime 조건과 맞춘다.

완료 조건: Modifier Runtime이 `COLD`인 상태에서도 Painting Mode 진입과 Stencil 연결이 정상이어야 한다.

### 단계 6. Undo·Load·다중 Window·Legacy 회귀

- Painting Undo의 Modifier 복원 0회를 검증한다.
- Modifier Undo 중 진행 Job 취소와 새 Token 복원을 검증한다.
- 같은 Scene·다른 Window의 중복 복원 요청을 단일 Job으로 병합한다.
- 기존 `0.3.0~0.3.8` PP Snapshot과 Modifier 편집 History를 손실 없이 읽는다.
- Source 누락·변경 Legacy 파일을 Fail Closed로 유지한다.

완료 조건: 저장 파일과 Undo 경계에서 자동 동기화나 교차 데이터 손상이 없어야 한다.

### 단계 7. 전체 QC·패키징·실제 설치

- 순수 Python, Blender Background와 GUI 회귀를 실행한다.
- `Untitled.blend`를 실제 Blender 5.2로 다시 열어 재현 순서를 검증한다.
- `SmallGicp` Production ZIP과 격리 설치본 검사를 실행한다.
- 실제 `user_default` Extension에 새 패키지를 설치한다.
- 임시 Probe, 성능 Stack Dump와 Debug 로그를 삭제한다.

완료 조건: 아래 승인 기준과 실제 설치본 검증을 모두 통과해야 한다.

## 18. 테스트 계획

### 18.1 Pure Python Core

- TXT·BIN, Legacy·Modern Layout Runtime 복원
- 비연속 Camera·Image·Point3D ID
- 편집 이력 없음·Point 삭제·Image 삭제·혼합 History
- 동기 `ensure_runtime()`과 증분 Job의 Reconstruction 값 동등성
- Relationship Index, Revision과 History Hash 동등성
- Source Hash 변경 전·중·후 감지
- Tick 시간 예산과 취소 후 열린 파일 Handle 없음
- 709,048 Point3D급 Fixture의 최대 단일 Step 시간 기록

### 18.2 Blender Background

- PP Workspace 상태 Load에서 Modifier `ensure_runtime()` 호출 0회
- 일반·Align Workspace 상태 Load에서 Modifier `ensure_runtime()` 호출 0회
- Modifier Workspace 상태 Load에서만 복원 Job 시작
- READY PP Snapshot 판정이 Modifier Runtime 상태와 무관함
- 업데이트 Provider COLD → READY → PP Commit 순서
- Provider 취소·Stale·오류에서 기존 PP Pointer와 Pixel 불변
- Undo 뒤 비활성 Modifier 복원 0회
- Extension 해제 뒤 Timer·Handle·Runtime Registry 잔여 0개

### 18.3 Painting 재진입

- Layout `COMPLETE` 상태의 Properties Context 재설정 없음
- `OBJECT`가 현재 Enum에 없을 때 `VIEW_LAYER` 또는 기존값 유지
- Paint 진입 중 TypeError의 전 상태 Rollback
- 활성 Brush가 없는 파일에서 `Paint Hard` 활성화
- `ENTERING` 중 Reconnect·Project·Stroke 차단
- `ACTIVE` 뒤 Reconnect와 Stencil 정상 연결
- 실패 뒤 `paint_enabled=False`, Target `OBJECT`, Runtime 없음
- 최초 진입 오류가 후속 Frame Source 오류로 덮이지 않음

### 18.4 실제 GUI 회귀

`Untitled.blend`에서 다음 순서로 검증한다.

```text
1. Blender 5.2로 파일 열기
2. PP Snapshot READY와 Modifier Runtime COLD 확인
3. 파일 로드 직후 Modifier load_reconstruction·replay_history 0회 확인
4. Projection Paint Workspace 재진입
5. Painting Mode가 실제 TEXTURE_PAINT·ACTIVE인지 확인
6. Reconnect Stencil Texture 성공 확인
7. Painting Stroke와 Undo·Redo 확인
8. Modifier Runtime이 계속 COLD인지 확인
9. COLMAP 데이터 업데이트 실행
10. Modifier 복원 단계부터 진행률 표시 확인
11. Camera 593개·Point3D 수와 새 PP Snapshot 확인
12. 기존 Layer·Original·Mask·Composite·Pixel 불변 확인
13. Modifier Workspace 진입과 편집 History·Review 복원 확인
14. Workspace 왕복 뒤 PP Snapshot과 Painting 재진입 확인
```

### 18.5 성능 승인

절대 시간만으로 통과시키지 않고 호출 횟수와 UI Tick 지연을 함께 본다.

- PP 재진입 전 Modifier 전체 Load: 0회
- PP Painting Undo 중 Modifier 전체 Load: 0회
- PP 업데이트 전 사용자 요청 없는 Modifier Source Hash: 0회
- 업데이트 진행 UI 등록 전 전체 Load: 0회
- Runtime Job 설치: 성공 요청당 최대 1회
- PP Snapshot Commit: 성공 요청당 1회
- PP Camera Runtime 재연결: 성공 요청당 최대 1회
- 최대 단일 Main Thread Job Step: 목표 50ms 이하, 초과 Phase는 추가 Chunk 분리
- 기존 약 29.7초 연속 Main Thread 점유: 제거

## 19. 호환성과 Migration

- `Scene.colmap_import`의 Dataset UUID, Source 경로, 편집 History Schema를 변경하지 않는다.
- `Scene.projection_paint`의 Snapshot UUID·Revision과 PP Resource 소유 태그를 유지한다.
- 기존 READY PP Snapshot은 재Import 없이 그대로 사용한다.
- 기존 Modifier Blender Point·Camera Resource는 삭제하지 않는다.
- Runtime 상태 `COLD`는 데이터 손실이나 Stale을 의미하지 않는다.
- `shared/colmap_dataset`은 기존 `0.2.0` 파일의 읽기 전용 Migration 입력으로 유지한다.
- 기존 파일의 Source가 없으면 PP Snapshot 사용은 허용하되 Modifier 편집·업데이트·Export는 차단한다.
- 새 영구 Property가 꼭 필요하지 않으면 Runtime 복원 상태는 저장하지 않는다.

## 20. 위험과 대응

| 위험 | 대응 |
| --- | --- |
| 지연 복원 뒤 Modifier 첫 진입이 느려짐 | 진행률, 취소와 제한 Tick을 제공하고 사용하지 않는 시점의 비용 제거 |
| PP 업데이트에서 Source 복원과 PP Staging이 겹쳐 Peak Memory 증가 | Phase 직렬화, 사전 메모리 추정과 중간 Buffer 즉시 해제 |
| History Replay 내부 단일 연산이 여전히 오래 걸림 | Operation 내부 Point·관측·Index 작업도 Chunk Job으로 분리 |
| 두 소비자가 같은 Runtime을 동시에 복원 | Scene·Dataset Token별 단일 Job과 소비자 소유권 조정기 사용 |
| 파일 Load 뒤 오래된 Blender ID Pointer 사용 | Generation·UUID·Revision 값만 저장하고 매 단계 현재 ID 재조회 |
| Source 변경을 캐시로 놓침 | 시작·완료 Fingerprint와 Scene Token을 모두 재검증 |
| Paint 진입 UI가 다시 가짜 활성 상태가 됨 | `paint_state == ACTIVE`와 실제 Runtime 조건을 UI Poll에 고정 |
| Brush 자동 선택이 사용자 설정을 덮음 | 유효 Brush가 없을 때만 기본 Brush를 활성화 |
| Python Thread로 Blender 데이터 충돌 | Thread를 사용하지 않고 Main Thread 증분 Job 또는 별도 Process의 값 연산만 허용 |
| Runtime 오류가 기존 PP 작업을 막음 | PP READY 판정과 Painting 경로에서 Modifier Runtime 의존 제거 |

## 21. 공식 Blender 기준

- [Application Handlers](https://docs.blender.org/api/5.2/bpy.app.handlers.html): Add-on의 파일 Load Handler는 `persistent`로 유지할 수 있지만 Handler 안의 데이터 변경은 신중해야 한다.
- [Troubleshooting Errors & Crashes](https://docs.blender.org/api/5.2/info_gotchas_crashes.html): Undo·데이터 교체 뒤 오래된 Blender 내부 객체 참조를 유지하지 않고 값 식별자로 다시 조회해야 한다.
- [Internal Data & Python Objects](https://docs.blender.org/api/5.2/info_gotchas_internal_data_and_python_objects.html): 오래 걸리는 Script는 Blender UI와 redraw를 막으므로 Modal Operator, Handler 또는 App Timer의 지연 실행 경계를 사용해야 한다.
- [Python Threads are Not Supported](https://docs.blender.org/api/5.0/info_gotchas_threading.html): Blender Python의 장기 Background Thread는 안정성이 보장되지 않으므로 `bpy` 데이터 작업에 사용하지 않는다.

## 22. 완료 조건

다음을 모두 만족해야 구조변경 완료로 판단한다.

1. `.blend` Load Handler가 비활성 Modifier의 `ensure_runtime()`을 호출하지 않는다.
2. 일반·Align·PP Workspace 파일 로드에서 Modifier Source 전체 Parse·Index·Replay가 0회다.
3. READY PP Snapshot만으로 Projection Paint Workspace와 Painting Mode에 진입한다.
4. Modifier Runtime `COLD`가 PP Snapshot을 `STALE` 또는 `ERROR`로 바꾸지 않는다.
5. Modifier Runtime은 Modifier 진입 또는 PP 업데이트의 명시적 요청으로만 복원된다.
6. Modifier Runtime 복원이 제한된 Tick 예산의 증분 Job으로 실행된다.
7. Modifier Workspace는 복원 중 N 패널과 진행률을 표시하고 편집 입력만 잠근다.
8. PP 업데이트는 Modifier Source 복원 시작부터 100% Commit까지 진행률을 표시한다.
9. 진행 UI가 표시되기 전에 동기 전체 `build_snapshot()`이 실행되지 않는다.
10. 업데이트 실패·취소 시 기존 PP Snapshot과 Paint 자산이 유지된다.
11. Modifier와 PP가 Camera·Point Blender ID를 공유하지 않는다.
12. Modifier 편집은 업데이트 전 PP Snapshot에 자동 반영되지 않는다.
13. Painting Undo가 Modifier Runtime 복원을 실행하지 않는다.
14. Paint UI는 실제 Texture Paint Runtime이 ACTIVE일 때만 활성 상태다.
15. Properties Context TypeError가 재현되지 않고 실패 시 전체 Rollback한다.
16. Brush가 없는 저장 파일에서도 기본 Brush 활성화 뒤 Stencil을 연결할 수 있다.
17. `Reconnect Stencil Texture`는 실제 Paint Session에서만 활성화된다.
18. Source 누락·변경 시 기존 PP Snapshot 사용은 유지되고 Modifier 작업만 명확히 차단된다.
19. Modifier 편집 History, Review, Pending과 PP Layer·Texture·Mask·Composite가 보존된다.
20. Align 코드와 Runtime은 변경되지 않는다.
21. Pure Python, Blender Background, 실제 GUI와 격리 설치본 회귀가 통과한다.
22. `Untitled.blend`의 기존 약 29.7초 Main Thread 연속 점유가 제거된다.
23. Production ZIP을 실제 `user_default`에 설치하고 같은 결과를 확인한다.
24. Debug 로그, 임시 성능 Probe, Stack Dump와 Staging Resource가 배포물에 남지 않는다.

## 23. 최종 사용자 흐름

```text
Blender 다시 실행
→ Projection Painting Workspace 진입
→ 저장된 PP 독립 Snapshot으로 즉시 Camera Review·Painting 사용

최신 COLMAP 데이터가 필요함
→ COLMAP 데이터 업데이트 클릭
→ Modifier 원본 복원과 PP Snapshot 생성 진행률 확인
→ 성공 시에만 새 PP Camera·Point로 교체

Modifier 편집이 필요함
→ Modifier Workspace 진입
→ 진행률과 함께 Runtime 복원
→ 기존 편집 History·Review 상태로 작업
```

이 구조에서 Modifier는 계속 COLMAP 원본과 편집 결과의 권위 도구다. Projection Painting은 사용자가 선택한 시점의 값을 복사해 독립적으로 사용하며, 파일 로드와 Painting 작업을 위해 Modifier 전체 Runtime을 자동으로 깨우지 않는다.
