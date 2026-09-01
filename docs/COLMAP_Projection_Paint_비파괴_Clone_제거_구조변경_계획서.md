# COLMAP Projection Paint 비파괴 Clone 제거 구조변경 계획서

> 0.3.16 정책 변경: 이 문서의 Legacy 자동 이전·Direct Edit Schema·저장 차단 계획은 폐기했습니다. 제품 코드는 기존 데이터셋을 자동 변경하거나 중단시키지 않습니다. 데이터셋 초기화 필요 여부는 검사 결과를 사용자에게 먼저 알리고, 명시적 승인을 받은 경우에만 별도 작업으로 처리합니다. 나머지 Composite 직접 편집 구조는 유지합니다.

## 1. 문서 정보

- 작성일: 2026-08-31
- 대상 제품: `COLMAP Tools` Blender Extension
- 대상 Blender: 5.2 LTS 이상
- 기준 버전: `0.3.14`
- 목표 버전: `0.3.15`
- 대상 기능: Projection Paint의 Composite 기반 비파괴 Clone Source·별도 Target Layer 구조
- 문서 상태: 구현 전 최종 계획
- 제외 도구: `COLMAP Modifier`, `COLMAP Align`

## 2. 쉬운 결론

Projection Paint가 Blender Clone의 Source Image, Clone Layer 사용 여부와 Clone Slot을 대신 설정하는 기능을 제거한다.

사용자는 Composite Layer 자체를 편집 대상으로 선택하고 Blender 기본 Clone 도구를 그대로 사용한다. 별도 Target Layer를 자동으로 비파괴 Clone 대상으로 묶지 않으며, `Ctrl` 기반 Source 위치 지정과 Stroke·Undo는 Blender가 직접 처리한다.

```text
Composite Layer 선택
→ 실제 Paint Target도 Composite Image
→ Blender Clone 도구 선택
→ 사용자가 Blender 기본 방식으로 Source 위치 지정
→ Composite Image에 직접 Clone Stroke 기록
```

## 3. 현재 구조와 제거 이유

현재 비파괴 Clone은 다음 세 설정을 애드온이 함께 바꾼다.

```text
ImagePaint.clone_image = Composite Image
ImagePaint.use_clone_layer = True
Material.paint_clone_slot = Composite Image Slot
```

Composite 활성 중 새 일반 Layer를 만들면 `composite_dependency_session_uuid`를 기록한다. 이 Layer는 Composite를 Source로 사용하고 자기 Image를 Paint Target으로 사용하므로 Composite 원본은 바뀌지 않는다. Composite 해제 시에는 같은 세션의 의존 Layer도 함께 삭제한다.

Blender 5.2의 `ImagePaint.use_clone_layer` 계약은 다른 UV Map·Paint Slot을 Clone Source로 사용하는 경로와 같은 이미지 안에서 Source 위치를 지정하는 경로를 나눈다. 현재 애드온이 `True`를 강제하므로 Clone Brush는 활성화돼도 사용자가 원하는 위치 지정 Clone이 동작하지 않는다.

이번 변경에서는 비파괴 Source·Target 분리를 제품 기능에서 제거하고 Composite Image 직접 편집을 단일 계약으로 확정한다.

## 4. 변경 원칙

1. Blender Clone 알고리듬, 입력과 Undo를 재구현하지 않는다.
2. Projection Paint는 새 Composite 생성 뒤 Clone 설정을 쓰지 않는다.
3. Composite Layer는 기존처럼 직접 선택·Paint 가능한 Image로 유지한다.
4. Composite 활성 중 추가한 일반 Layer는 Clone 전용 의존 Layer가 아니라 평범한 독립 Layer다.
5. Composite 해제 시 Composite만 삭제하고 이후 추가한 일반 Layer는 보존한다.
6. 기존 파일의 의존·파생 Layer는 삭제하지 않고 일반 Layer로 승격한다.
7. Camera Stencil, 실제 Clone Brush 활성화, Layer·Mask·Adjustment와 Composite Bake는 현재 계약을 유지한다.
8. 애드온이 관리하지 않는 사용자의 외부 Clone 설정은 변경하지 않는다.
9. 실패·취소·Undo·Redo·Load에서 기존 Image와 Pixel을 추측으로 삭제하지 않는다.
10. 호환 이전이 끝나기 전에 저장 필드를 제거해 기존 파일의 복구 정보를 잃지 않는다.
11. 특정 중간 버전 설치를 전제로 하지 않고 지원 대상 구버전 파일을 직접 이전한다.
12. 불가역 Image 삭제 뒤에는 거짓 전체 Rollback을 시도하지 않고 정상 Graph와 고아 자원을 분리한다.

## 5. 목표 사용자 흐름

### 5.1 직접 Clone

```text
Composite Layer 생성 또는 선택
→ active_paint_image = Composite Image
→ Material.paint_active_slot = Composite Image Slot
→ Blender Clone 도구 선택
→ 실제 CLONE Brush 활성화
→ Blender 기본 Source 위치 지정
→ Composite Image에 직접 Stroke
```

- 애드온은 `clone_image`, `use_clone_layer`, `paint_clone_slot`을 설정하지 않는다.
- 새 파일에서는 Blender 기본 Clone 상태를 그대로 사용한다.
- 구버전 파일에서 애드온 소유 Composite Clone 연결로 판정된 경우에만 한 번 해제한다.
- Source 위치, Offset 표시, Stroke Sampling과 Undo는 Blender 기본 동작을 사용한다.

### 5.2 일반 Layer 추가

Composite 활성 중 `+`로 만든 Layer는 다음처럼 처리한다.

- `layer_role=NORMAL`
- `composite_dependency_session_uuid`를 새로 기록하지 않음
- Composite를 Clone Source로 자동 연결하지 않음
- Layer의 Mask·Adjustment·단독 보기와 일반 Paint는 기존과 동일
- 재합성 없이 Composite를 해제하면 Image와 Pixel을 포함해 유지

이 계약은 Layer 추가 기능을 막지 않으면서 비파괴 Clone 전용 수명주기만 제거한다.

### 5.3 Composite 다시 생성

- 기존 Composite의 직접 Paint·Clone 결과는 현재 평면 Composite Pixel에 포함된다.
- 기존 재합성의 재사용 또는 평면화 Bake 판정은 유지한다.
- 사용자가 선택한 연속 범위와 눈이 켜진 Layer만 Bake하는 규칙을 유지한다.
- 별도 일반 Layer를 Composite와 함께 재합성 Source로 명시 선택하면 기존 계약대로 새 Composite에 Bake된 뒤 별도 Layer는 소비된다.
- 새 Composite 확정 뒤에도 Clone Source를 자동 활성화하지 않는다.
- 재합성 실패 시 기존 Composite Pixel, 일반 Layer와 선택 상태를 보존한다.

### 5.4 Composite 해제

- 원본 Source Layer의 저장된 눈 상태를 복구한다.
- 현재 Composite Layer와 그 소유 Image·Mask·Adjustment만 삭제한다.
- Composite 활성 이후 추가한 일반 Layer는 삭제하지 않는다.
- 일반 Layer의 현재 순서, Image Pixel, Mask와 Adjustment를 유지한다.
- Composite에 직접 기록한 Paint·Clone과 Composite 자체의 Mask·Adjustment 결과는 Composite와 함께 제거된다. 이것은 `Composite 해제`의 기존 의미와 일치하며 확인창에 명시한다.

## 6. 제거 대상

### 6.1 활성 Clone Source 연결

다음 함수와 호출 경로를 제거한다.

- `layer_shader._configure_clone_source_image()`
- `layer_shader.configure_composite_clone_source()`
- `composite_layer._activate_composite_clone_source()`
- Layer 선택 시 Composite Clone Source 재연결
- Composite 생성·재생성 Commit 뒤 Clone Source 활성화
- Object Mode 지연 Clone Slot 연결

### 6.2 Composite Clone 설정 저장·복구

새 Composite Transaction에서 다음 상태를 더는 캡처·변경·복구하지 않는다.

- `composite_saved_clone_image`
- `composite_saved_use_clone_layer`
- `composite_saved_paint_clone_slot`
- `CompositeCreateJob.clone_restore_*`
- `_capture_clone_settings()`
- `_apply_clone_settings()`
- `_restore_clone_settings()`
- Composite Release Snapshot의 Clone 필드

단, 0.3.12~0.3.14 저장 파일의 안전한 이전을 위해 위 저장 필드의 RNA 정의와 읽기 전용 Migration 접근은 호환 Reader로 유지한다. 활성 기능은 이 값을 새로 쓰지 않는다. 사용자가 `0.3.14`에서 후속 버전으로 바로 건너뛰어도 이전할 수 있어야 하므로 `0.3.16` 같은 고정 릴리스 번호만으로 제거하지 않는다. 최소 지원 저장 버전이 0.3.14보다 높아지고, untouched 0.3.12~0.3.14 Fixture의 직접 업그레이드 지원 종료가 명시된 때에만 별도 변경으로 제거한다.

### 6.3 의존 Layer 수명주기

활성 동작에서 다음 계약을 제거한다.

- 새 Layer의 `composite_dependency_session_uuid` 자동 기록
- `CompositeSession.dependents`를 실제 삭제·검증 대상으로 채우고 `derived`로 사용하는 활성 의미
- 의존 Layer Image 소유권을 Composite 세션 검증 조건으로 사용
- Composite 해제 시 의존 Layer 묶음 삭제
- 재합성 시 의존 UUID를 새 세션으로 이전
- 해제 확인창의 의존 Layer 삭제 경고
- `COMPOSITE_DERIVED`를 새 Layer 역할로 생성·이전하는 활성 분기

`composite_dependency_session_uuid`와 `COMPOSITE_DERIVED` 값은 Legacy 입력으로만 읽고 일반 Layer로 정규화한다. 이 Enum 식별자와 RNA 필드 역시 건너뛰기 업그레이드 Reader이므로 Clone 저장 필드와 같은 제거 기준을 적용한다.

`layer_shader.py`의 공개 Export에 포함된 `configure_composite_clone_source()`는 0.3.15에서 Clone RNA를 전혀 쓰지 않는 Deprecated no-op 호환 함수로 남긴다. 내부 호출은 모두 제거하고, 외부 사용 여부와 지원 기간을 확인한 별도 호환성 변경에서 Export를 제거한다. `CompositeSession`은 기존 생성자 형태와 `dependents`, `derived` 접근을 한 릴리스 동안 유지하되 값은 항상 빈 Tuple로 고정한다. 이 Deprecated 호환 Shim으로 내부 기능 제거와 외부 API 파손을 분리한다.

## 7. 유지 대상과 비대상 코드

이름에 Clone이 들어가더라도 다음 코드는 비파괴 Composite Clone 기능이 아니므로 제거하지 않는다.

- `paint_tool_sync.py`의 실제 Blender `CLONE` Brush 활성화
- `paint_runtime.py`의 Reconnect 전후 일반 Blender Clone 상태 불변 검증
- `ImagePaint.clone_offset` 보존과 Rollback 검증
- `overlay_migration.py`의 외부 `clone_image` 참조 보호
- Blender가 소유한 `clone_image`, `use_clone_layer`, `paint_clone_slot` RNA 자체

이 코드는 Projection Paint가 외부 Blender Clone 설정을 침범하지 않게 하는 보호 장치다.

다음 기능도 변경하지 않는다.

- Composite Color·Alpha Bake와 Source 구간 선택
- Composite 재합성의 Image 재사용·평면화·원자 Rollback
- Layer Mask, Adjustment, Layer·Mask 개인 보기
- Texture Export와 Original Layer 외부 참조
- Camera Stencil 수동 Reconnect와 Camera View 복구
- COLMAP Snapshot 업데이트, Modifier와 Align
- Projection Paint 독립 Undo·Workspace·세션 경계

## 8. Legacy 저장 파일 이전

### 8.1 이전 Schema

`COMPOSITE_DIRECT_EDIT_SCHEMA_VERSION`을 추가한다. RNA 기본값은 반드시 `0`으로 두어 아직 이전하지 않은 구버전 파일을 식별하고, 새 Target을 생성하거나 초기화하는 경로만 명시적으로 `1`을 기록한다.

```text
0: 비파괴 Composite Clone Source·의존 Layer 가능
1: Composite 직접 편집·일반 Layer 보존
```

이전은 `ensure_composite_direct_edit_schema(scene)` 단일 진입점으로 구현하며 여러 번 호출돼도 같은 결과인 멱등 함수로 수행한다.

호출 경계는 다음과 같다.

1. 애드온 등록 직후 이미 열려 있는 모든 Scene
2. `load_post`의 모든 Scene
3. 활성·지연 `undo_post`, `redo_post` 복구
4. `validate_composite_session()`, `reconcile_layer_graph()`와 생성·재합성·해제 진입 전
5. `save_pre`의 모든 Scene

`CREATING`, `RECOMPOSITING`, `RELEASING` 상태에서는 먼저 기존 중단 Transaction을 복구하거나 `ACTIVE`·`STALE`로 확정하고 그 뒤 이전을 다시 호출한다. 이전 실패 Scene은 Schema 0과 Legacy 필드를 그대로 유지하고 `STALE`과 구체적인 오류를 기록한다. `save_pre` 감사에서 이전하지 못한 PP Scene이 하나라도 있으면 부분 이전 상태를 저장하지 않도록 저장을 명시적으로 실패시킨다.

### 8.2 애드온 소유 Clone 연결 판정

현재 Clone 설정을 세 상태로 분류한다.

| 판정 | 조건 | 이전 동작 |
| --- | --- | --- |
| `FULL_OWNED` | 유효한 활성 Composite, `use_clone_layer=True`, `clone_image=현재 Composite`, 유효한 Clone Slot도 같은 Composite | 캡처된 외부 Clone 설정을 필드별 검증 후 복구 |
| `DEFERRED_OWNED` | 유효한 활성 Composite, `use_clone_layer=True`, `clone_image=현재 Composite`이며 Slot 목록이 비었거나, Schema 0과 명시적으로 저장된 Legacy Snapshot·동일 세션 Metadata가 Object Mode 지연 연결을 입증 | 현재 Composite를 가리키는 Clone Image와 애드온이 켠 Flag만 해제하고 다른 유효 Slot은 변경하지 않음 |
| `FOREIGN` | 위 소유 증거가 없거나 사용자가 다른 Clone Image·Slot로 바꿈 | 아무 값도 변경하지 않음 |

구버전 `_configure_clone_source_image()`는 Object Mode에서 `texture_paint_images`가 비어 있으면 `clone_image`와 `use_clone_layer`만 기록하고 Slot 설정을 건너뛰었다. 따라서 Slot 일치만 필수로 보는 판정은 사용하지 않는다. 반대로 유효한 다른 Slot은 소유 증거 없이 절대 덮어쓰지 않는다.

Legacy Snapshot은 “모두 유효”라는 묶음 조건이 아니라 다음 결정표로 복구한다.

| 필드 | 유효성 | 복구 규칙 |
| --- | --- | --- |
| `composite_saved_clone_image` | 명시 저장 여부를 확인하며 `None`도 정상값 | 유효 Pointer 또는 명시 저장된 `None`만 복구 |
| `composite_saved_use_clone_layer` | 명시 저장 여부를 확인하며 `False`도 정상값 | 명시 저장된 Bool만 복구 |
| `composite_saved_paint_clone_slot` | `0 <= 값 < len(texture_paint_images)` | 범위가 유효할 때만 복구하고 `-1` 또는 범위 밖 값은 현재 Slot 유지 |
| `clone_offset` | 비파괴 Clone 기능이 캡처한 적 없음 | 읽거나 쓰지 않고 이전 전후 불변 검증 |

기존 PropertyGroup의 `is_property_set()` 여부, Schema 0, 활성 Composite 세션과 저장 Metadata를 함께 사용해 “기본값”과 “명시 저장된 None·False”를 구분한다. 판정 근거가 불충분하면 `FOREIGN`으로 두어 사용자 설정을 우선한다.

### 8.3 의존·파생 Layer 승격

- 유효한 활성 세션의 `COMPOSITE_DERIVED` Layer를 `NORMAL`로 바꾼다.
- 유효한 활성 세션에서는 모든 동일 세션 `composite_dependency_session_uuid`를 빈 값으로 정규화한다.
- Layer 이름, UUID, Image, Pixel, 위치, 가시성, Mask와 Adjustment는 유지한다.
- 승격한 Layer는 활성 Composite Graph에 그대로 남고 Composite 해제 뒤에도 유지된다.
- `composite_state=NONE`인데 고아 `COMPOSITE_DERIVED`만 남은 경우에는 PP Layer Image 소유권을 검증한 뒤 일반 Layer로 승격하고 Graph를 재검증한다.
- 활성 Composite 세션과 Derived·Dependency UUID가 충돌하거나 Image 소유권이 불완전하면 추측으로 수정하지 않고 Schema 0·Legacy Metadata를 유지한 채 `STALE`로 중단한다.
- Schema 1을 기록한 Scene에는 `COMPOSITE_DERIVED` Role이나 Dependency UUID가 하나도 남아 있지 않아야 한다.

### 8.4 이전 원자성

Migration 전에 다음 값을 Snapshot한다.

- Layer Role·Dependency UUID·순서·활성 선택
- Clone Image·사용 여부·Clone Slot
- Composite 저장 Clone 필드
- Composite 상태와 Active Image·Paint Slot

Graph 검증까지 성공한 뒤 Schema 1을 기록한다. 실패하면 모든 값을 되돌리고 Image와 Layer는 삭제하지 않는다. 다중 Scene에서는 Scene별 Snapshot과 결과를 분리하며, 하나의 실패가 다른 Scene의 Legacy 값을 지우지 않는다.

## 9. Composite Transaction 변경

### 9.1 생성 시작

- Clone 설정 Capture를 제거한다.
- 기존 Composite Source Metadata·가시성·선택 Snapshot만 유지한다.
- 생성 실패 Rollback에서 Clone 설정 쓰기를 제거한다.

### 9.2 Commit

- Composite Image와 Layer를 생성하고 현재 Paint Target으로 활성화하는 기존 경로를 유지한다.
- Clone Source 활성화 호출을 제거한다.
- Commit 전후 외부 Clone 설정이 동일한지 테스트로 확인한다.

### 9.3 재합성

- `previous_state`와 `CompositeCreateJob`에서 Clone 전용 필드를 제거한다.
- 기존 Composite 직접 편집 Pixel의 평면 Source 재사용 계약을 유지한다.
- 일반 Layer에 Dependency UUID를 전파하지 않는다.

### 9.4 해제

- `CompositeSession`은 Composite와 Source만 보관한다.
- 삭제 대상은 Composite 묶음 하나로 제한한다.
- 새 일반 Layer를 원본 복구 Graph에 포함한다.
- Clone 복구 함수 호출을 제거한다.
- 해제를 `Prepare → 복구 Graph 검증 → 논리 Commit → 고아 자원 Cleanup`으로 나눈다.
- Prepare와 복구 Graph 검증 실패는 Layer·Graph·선택·Active Paint Slot을 완전히 Rollback한다.
- 논리 Commit 전에 Source Metadata 정리와 활성 Layer 전환에 필요한 모든 실패 가능 작업을 끝낸다.
- 논리 Commit 뒤에는 정상 사용자 Graph를 되돌리지 않는다. Image·Node Cleanup 실패 자원은 삭제를 재시도할 수 있는 명시적 고아 목록으로 남기고 경고한다.
- 전 단계 원자성을 주장하지 않으며, 불가역 삭제가 시작된 뒤 삭제한 Pixel을 되살리는 거짓 Rollback은 구현하지 않는다.

## 10. UI 변경

- `Composite 해제` 확인창에서 “의존 형제 Layer 삭제” 문구를 제거한다.
- 새 안내는 “Composite 자체의 Paint·Clone·Mask·Adjustment 결과는 해제 시 함께 제거됩니다. 별도로 추가한 일반 Layer는 유지되지만, 재합성 Source로 선택하면 기존 규칙대로 Bake 후 소비됩니다.”로 통일한다.
- Composite 생성 완료 상태의 “Clone 소유권 확정” 문구를 “Composite Paint Target 확정”으로 바꾼다.
- 별도 Clone Session, Clone Source 버튼 또는 애드온 전용 Source 위치 UI는 추가하지 않는다.
- Blender Clone 툴바와 기본 Source 위치 지정 UX를 그대로 사용한다.

## 11. 수정 대상

| 파일 | 변경 범위 |
| --- | --- |
| `tools/projection_paint/blender/layer_shader.py` | Composite Clone Source 설정 함수·Layer 선택 호출 제거, 새 Layer Dependency 기록 제거, Legacy 파생 Layer 일반 승격 |
| `tools/projection_paint/blender/composite_layer.py` | Clone Capture·Restore·Job 필드 제거, Session dependents 활성 의미 제거·빈 호환값 유지, 생성·재합성·해제 Transaction 정리 |
| `tools/projection_paint/blender/layers.py` | 해제 확인창·설명·상태 문구 변경 |
| `tools/projection_paint/blender/properties.py` | Direct Edit Schema 추가, Legacy RNA·Enum을 지원 업그레이드 Reader로 표시 |
| `tools/projection_paint/blender/paint_session.py` | 새 Target의 Direct Edit Schema=1 초기화, 새 Clone Snapshot 쓰기 제거·Legacy 기본값 유지 |
| `tools/projection_paint/blender/layer_reset.py` | 새 Reset의 Direct Edit Schema 유지와 Legacy 입력 Snapshot·Rollback 보존 |
| `tools/projection_paint/blender/state_handlers.py` | 등록·모든 Scene Load·활성/지연 Undo/Redo·모든 Scene Save Migration 호출 |
| `tests/projection_paint/blender_background/test_composite_layer.py` | 직접 Clone 계약, 일반 Layer 보존, 생성·해제·재합성 원자성 회귀로 교체 |
| `tests/projection_paint/blender_background/test_layer_shader.py` | Layer 선택이 Clone Source를 바꾸지 않는지와 Dependency 미생성 회귀 추가 |
| `tests/projection_paint/blender_background/test_layer_ui.py` | Derived 표시·들여쓰기·해제 경고 회귀를 일반 Layer·직접 편집 계약으로 교체 |
| `tests/projection_paint/blender_background/test_sessionless_state.py` | 저장·재열기 Legacy Migration과 Direct Edit Schema 회귀 추가 |
| `tests/projection_paint/gui_stencil_tool_independence.py` | 실제 Clone Brush·Stencil 독립 계약 유지 확인 |
| 새 실제 Clone GUI 회귀 | 실제 Essentials Clone, Source 위치 지정, Stroke·Undo Pixel 검증 |
| `__init__.py`·`blender_manifest.toml` | 검증 완료 뒤 제품 버전을 0.3.15로 일치 |
| `docs/README.md`·`docs/migration/IMPLEMENTATION_STATUS.md`·새 Release Note | 직접 Composite Clone UX, 호환 Reader와 검증 결과 기록 |
| `docs/TESTING.md`·구조 문서 | 실제 Clone GUI 절차와 Legacy 이전 기준 기록 |

`layer_shader.py`와 `composite_layer.py`의 `__all__`도 호환 정책에 맞춰 검토한다. 활성 내부 호출은 제거하되 Deprecated no-op과 빈 호환 속성은 제거 시점까지 API 회귀로 고정한다.

`paint_runtime.py`, `paint_tool_sync.py`, Stencil 모듈은 원칙적으로 수정하지 않는다. 일반 Clone 상태 불변 검증이 새 직접 Clone 계약과 충돌하는 경우에만 테스트 문구를 조정하고 기능은 유지한다.

## 12. 테스트 계획

### 12.1 정적·단위 계약

1. Production 코드가 `use_clone_layer=True`를 기록하지 않는다.
2. Production 코드가 Composite Image를 `clone_image` 또는 `paint_clone_slot`으로 지정하지 않는다.
3. 새 Layer가 Dependency UUID를 기록하지 않는다.
4. Composite 해제 삭제 집합에 일반 Layer가 포함되지 않는다.
5. `paint_tool_sync`의 실제 `CLONE` Brush 매핑은 유지된다.
6. GUI 회귀가 `clone_offset` 직접 대입이나 자체 Clone Pixel 연산을 사용하지 않는다.
7. Schema 1 Scene에 `COMPOSITE_DERIVED`와 Dependency UUID가 남지 않는다.

### 12.2 Blender Background

1. Composite 생성
   - Composite가 `active_paint_image`와 `paint_active_slot`이 됨
   - 외부 `clone_image`, `use_clone_layer`, `paint_clone_slot` 불변
2. Composite 선택
   - 재선택해도 Clone 설정 불변
   - Mask·Adjustment·단독 보기 기존 동작 유지
3. 일반 Layer 추가
   - Dependency UUID 없음
   - 일반 Paint·Mask·Adjustment 가능
   - 재합성 없이 Composite 해제 뒤 Image Hash와 Layer UUID 유지
   - Composite와 함께 재합성 Source로 명시 선택한 경우 기존 규칙대로 Bake 후 소비
4. 재합성
   - 직접 수정한 Composite Pixel이 새 결과 Source에 포함됨
   - 성공·취소·실패에서 외부 Clone 설정 불변
5. 해제
   - Composite만 삭제
   - Source 가시성 복구
   - 이후 추가 일반 Layer 보존
   - Prepare·Graph 검증 실패 주입 시 전체 Layer·Pixel·Graph Rollback
   - 논리 Commit 후 Cleanup 실패 주입 시 정상 Graph 유지와 고아 자원 경고
6. Legacy Migration
   - 완전한 0.3.12 Clone Snapshot 복구
   - Object Mode·Texture Slot 0개의 지연 Clone 연결 해제
   - 유효한 다른 Clone Slot 보존
   - `None` Clone Image·`False` use flag·`-1` Slot의 필드별 복구
   - 사용자가 변경한 외부 Clone 설정 불변
   - Derived·Dependency Layer 일반 승격과 Pixel Hash 유지
   - 세션 불일치·Composite 없음·Dependency만 남은 Fixture의 보존 또는 명시적 STALE
   - 두 번 실행해도 변화 없음
7. 저장·재열기
   - Direct Edit Schema 유지
   - 활성 Composite, 일반 Layer와 Pixel 유지
   - 구버전 Fixture 자동 이전 후 저장·재열기 동일
   - 0.3.15를 거치지 않은 untouched 0.3.12~0.3.14 Fixture 직접 이전
   - 다중 Scene·비활성 Scene·등록 후 무Load·지연 Undo 직후 Save 감사

### 12.3 실제 Blender GUI

결정적인 Plane·UV와 비대칭 Source Pixel 패턴을 가진 `Untitled.blend` 복사본에서 다음을 검증한다.

```text
1. Projection Paint 진입
2. Composite Layer 생성 또는 기존 Composite 선택
3. 실제 Blender Clone 도구 선택
4. Tool ID=builtin_brush.clone 확인
5. Brush.image_brush_type=CLONE 확인
6. 현재 Paint Target=Composite Image 확인
7. Blender 5.2의 Event Simulation을 활성화하고 `Window.event_simulate`로 실제 Ctrl+LMB press/release를 보내 Source 위치 지정
8. 다른 위치에 Clone Stroke
9. 목적지 Pixel이 선택 Source 패턴과 일치하는지 확인
10. 동일 UV 위치 복사가 아님을 확인
11. Undo로 목적지 Pixel Hash 복구 확인
12. 일반 Layer 추가 후 Composite 해제
13. 일반 Layer UUID·Image·Pixel 보존 확인
14. Draw 복귀 뒤 Camera Stencil은 명시적 Reconnect로만 연결 확인
```

검증은 테스트용 Brush나 자체 Clone 알고리듬을 만들지 않는다. Blender Essentials `Clone` 자산과 실제 Viewport 입력만 사용한다. 고정된 Viewport 영역·투영 좌표·Source/Destination 좌표를 기록하며 `use_clone_layer=False`, `clone_offset` 직접 대입 0회와 실제 이벤트 수신을 자동 판정한다.

### 12.4 전체 회귀

- Composite 생성·재합성·해제·Alpha·UDIM·Native Bake
- Layer 추가·삭제·이동·Mask·Adjustment·단독 보기
- Original 외부 참조와 Texture Export
- Paint 진입·종료·저장·재열기·Undo·Redo
- Camera Stencil·Clone Tool·다른 Brush 독립성
- Projection Paint Snapshot, Workspace, Modifier와 Align 독립성
- 공개 `__all__` 호환 Shim과 `CompositeSession` 호환 속성
- Production 220개 검사, 격리 설치본, Workspace GUI와 실제 `user_default` 설치본

## 13. 구현 순서

### 단계 1. 실패 재현과 계약 고정

- 현재 `use_clone_layer=True`에서 위치 지정이 적용되지 않는 실제 GUI 실패를 기록한다.
- 기존 Composite 생성·재합성·해제와 외부 Clone 설정 불변값을 Snapshot한다.
- 새 계약의 실패 테스트를 먼저 추가한다.

완료 조건: 현재 비파괴 연결과 목표 직접 편집의 차이가 자동·GUI 회귀로 재현된다.

### 단계 2. Active Clone 연결 제거

- Clone Source 설정 함수와 호출부를 제거한다.
- Composite 생성·선택·재합성이 Clone 설정을 쓰지 않게 한다.
- 실제 Composite Image의 Paint Target 활성화는 유지한다.

완료 조건: 새 Composite 작업 중 애드온의 Clone RNA 쓰기가 0회다.

### 단계 3. 의존 Layer를 일반 Layer로 전환

- 새 Layer Dependency 기록을 제거한다.
- Session 검증과 Graph·해제 삭제 집합에서 Dependents를 제거한다.
- 일반 Layer를 Composite 해제 뒤 보존한다.
- 재합성 Source로 명시 선택한 일반 Layer는 기존 Bake 후 소비 계약을 유지한다.

완료 조건: Composite 활성 중 추가한 Layer가 독립 일반 Layer가 되며, 재합성 Source로 선택하지 않고 해제하면 끝까지 유지된다.

### 단계 4. Legacy Migration

- Direct Edit Schema와 멱등 Migration을 구현한다.
- `FULL_OWNED`·`DEFERRED_OWNED`·`FOREIGN` 판정으로 기존 Clone 연결을 소유권 범위만큼 해제한다.
- 기존 Derived·Dependency Layer를 비파괴 승격한다.
- 등록·모든 Scene Load·활성/지연 Undo/Redo·공개 Composite 작업·모든 Scene Save 경계에 단일 Migration 진입점을 연결한다.
- Legacy 저장 필드와 Enum은 지원 구버전의 건너뛰기 업그레이드 Reader로 유지한다.

완료 조건: 기존 저장 파일의 Layer·Image·Pixel과 사용자 외부 Clone 설정을 잃지 않는다.

### 단계 5. Transaction·UI 정리

- Clone 전용 Job·Snapshot·Rollback 필드를 제거한다.
- 해제를 Prepare·검증·논리 Commit·고아 Cleanup 단계로 분리한다.
- 해제 확인창과 상태 문구를 직접 편집 계약에 맞춘다.
- 공개 Clone 설정 함수와 Session 속성은 활성 기능이 없는 Deprecated 호환 Shim으로 제한한다.
- 해결된 Debug Probe와 임시 로그를 제거한다.

완료 조건: 활성 코드에 비파괴 Composite Clone Source·Target 분기가 남지 않는다.

### 단계 6. 전체 검증·패키징

- Background·실데이터 GUI·전체 회귀를 실행한다.
- Production ZIP을 빌드하고 격리 설치본을 검증한다.
- 실제 `user_default`에 설치하고 Blender 재시작 뒤 최종 QC를 실행한다.

완료 조건: 실제 Clone 위치 지정·Stroke·Undo가 Blender 기본 기능으로 동작하고 다른 PP 기능 회귀가 없다.

## 14. 위험과 대응

| 위험 | 대응 |
| --- | --- |
| 구버전 의존 Layer가 Composite 해제 때 삭제됨 | Migration에서 먼저 일반 Layer로 승격하고 삭제 집합을 Composite 하나로 제한 |
| Object Mode 지연 연결을 외부 설정으로 오판 | `FULL_OWNED`와 Slot 없는 `DEFERRED_OWNED`를 분리하고 다른 유효 Slot은 보존 |
| 사용자의 외부 Clone 설정을 애드온 연결로 오판 | Schema 0·현재 Composite·명시 저장 Snapshot·세션 Metadata를 함께 확인하고 불충분하면 `FOREIGN` 유지 |
| Clone 설정 Snapshot이 불완전함 | None·False를 정상값으로 인정하는 필드별 결정표와 Slot 범위 검증, 임의 Slot 선택 금지 |
| 새 일반 Layer가 Source 복구 Graph에서 사라짐 | Release Graph에 Source와 비Source 일반 Layer를 모두 포함하고 UUID·Pixel Hash 검증 |
| 새 일반 Layer 보존 안내와 재합성 소비 규칙 혼동 | 직접 해제 시 보존, 재합성 Source 명시 선택 시 Bake 후 소비를 UI·테스트에 구분 |
| 직접 Clone 결과가 재합성에서 사라짐 | Dirty Composite 재사용·평면화 두 경로의 Pixel 회귀 |
| Composite 해제로 직접 Clone 결과가 사라지는 UX 혼동 | 확인창에 Composite 직접 편집 결과 삭제를 명시 |
| Stencil Reconnect가 Clone 상태를 바꿈 | `paint_runtime`의 일반 Clone 상태 불변 Snapshot·검증 유지 |
| 위치 지정 테스트가 단순 Draw Stroke를 Clone으로 오판 | Source 패턴과 목적지 Pixel의 정확 일치, 동일 UV 불일치, Undo Hash까지 검증 |
| 중간 버전을 건너뛴 파일이 Legacy Reader를 잃음 | 최소 지원 저장 버전 기준으로 Reader 제거, untouched 구버전 직접 업그레이드 Fixture 유지 |
| 비활성 Scene이 이전 없이 저장됨 | 등록·Load·Undo/Redo·Save의 모든 Scene 감사와 실패 시 저장 차단 |
| 불가역 삭제 뒤 전체 Rollback을 가장함 | 논리 Commit 전만 완전 Rollback, 이후 정상 Graph 유지와 고아 Cleanup 경고 |
| 공개 내부 API 제거로 외부 스크립트가 파손됨 | 기능 없는 Deprecated no-op·빈 Tuple Shim을 한 릴리스 이상 유지하고 사용 현황 검증 |

## 15. 완료 기준

1. 애드온이 새 Composite 세션에서 `clone_image`, `use_clone_layer`, `paint_clone_slot`을 변경하지 않는다.
2. Composite Layer가 실제 Texture Paint Target으로 직접 선택된다.
3. 실제 Blender Clone 도구와 `CLONE` Brush가 활성화된다.
4. Blender 기본 Source 위치 지정 뒤 선택 위치의 Pixel이 목적지로 복사된다.
5. Clone Undo가 Stroke 전 Pixel을 복구한다.
6. 새 일반 Layer에 Dependency UUID가 기록되지 않는다.
7. 재합성 Source로 선택하지 않은 새 일반 Layer는 Composite 해제 뒤 UUID·Image·Pixel이 유지된다.
8. Composite 해제는 Composite 소유 자원만 삭제한다.
9. 기존 Derived·Dependency Layer는 삭제 없이 일반 Layer로 이전된다.
10. `FULL_OWNED`와 Object Mode `DEFERRED_OWNED` 연결만 안전하게 해제된다.
11. 사용자가 바꾼 외부 Clone 설정은 유지된다.
12. Composite 생성·재합성·실패 Rollback이 외부 Clone 설정을 변경하지 않는다.
13. Alpha·UDIM·Mask·Adjustment·단독 보기와 Texture Export가 유지된다.
14. Camera Stencil은 Clone 전환과 독립적으로 동작한다.
15. Modifier, Align과 COLMAP Snapshot 코드는 변경되지 않는다.
16. Background, 실제 GUI, Production 빌드와 설치본 회귀가 모두 통과한다.
17. 0.3.15 활성 코드에는 비파괴 Clone Source·의존 Target 기능이 남지 않는다.
18. 새 Direct Edit Schema의 RNA 기본값은 0이고 새 Target만 1로 초기화된다.
19. 등록·Load·Undo·Redo·Save와 공개 Composite 작업에서 모든 Scene의 Migration이 멱등 실행된다.
20. 해제는 논리 Commit 전 실패만 완전 Rollback하고 이후 Cleanup 실패는 정상 Graph와 고아 경고를 유지한다.
21. untouched 0.3.12~0.3.14 파일을 중간 버전 없이 직접 이전하는 Fixture가 유지된다.
22. 최소 지원 저장 버전 상향 전에는 Legacy RNA·Enum Reader를 제거하지 않는다.

## 16. 최종 데이터 흐름

```text
Source Layer Stack
→ Composite Bake
→ Composite Image가 직접 Paint Target
→ Blender 기본 Clone Source 위치 지정
→ Composite Image 직접 수정
→ Blender Texture Paint Undo

일반 Layer 추가
→ 독립 NORMAL Layer
→ Composite Clone Source 자동 연결 없음
→ 재합성 Source로 선택하지 않으면 Composite 해제 뒤에도 유지
```

이 구조에서 Projection Paint는 Composite Image와 Layer Graph만 관리한다. Clone 도구, Source 위치, Stroke와 Undo는 Blender가 관리한다.

## 17. 구현 결과

0.3.15에서 계획한 직접 편집 구조를 구현했습니다. Blender Background 대상 회귀, Production 검사 220개, Small GICP 내장 Runtime, 격리 설치본과 실제 `user_default` 설치를 통과했습니다. 실제 Blender Essentials Clone GUI 입력으로 Source 지정·Destination 복제·Undo Pixel Hash 원복을 확인했고 애드온의 `clone_offset` 직접 쓰기와 가짜 Brush 생성은 모두 0회였습니다.
