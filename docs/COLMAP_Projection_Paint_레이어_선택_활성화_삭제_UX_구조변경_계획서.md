# COLMAP Tools Projection Paint 레이어 선택·활성화·삭제 UX 구조변경 계획서

## 1. 문서 목적

이 문서는 Projection Paint의 레이어 트리에서 `선택`, `활성화`, `삭제`를 서로 다른 동작으로 분리하기 위한 구조변경 계획이다.

현재는 레이어 트리의 행을 한 번 클릭하면 UI 선택과 실제 Paint Image 활성화가 동시에 일어난다. 또한 파란 배경은 실제 활성 상태가 아니라 `ui_selected` 선택 상태만 표시하고, `-` 버튼은 선택 상태가 아니라 실제 활성 상태를 기준으로 삭제한다. 이 때문에 사용자가 화면에서 보고 있는 선택 상태와 실제 페인트 대상·삭제 대상이 서로 다르게 해석될 수 있다.

변경 후에는 다음 원칙을 사용한다.

- 한 번 클릭은 선택만 변경한다.
- 같은 항목을 두 번 클릭하면 해당 항목이 실제 편집 대상으로 활성화된다.
- 파란 배경은 선택 상태만 표시한다.
- 실제 활성 편집 대상은 별도의 기호로 표시한다.
- `-`와 `Del`은 실제 활성 항목이 아니라 선택된 항목을 삭제한다.
- 레이어 트리 밖을 클릭하면 선택이 해제되는 현재 동작은 유지한다.

이 변경의 목표 버전은 `0.3.23`으로 가정한다. 구현 시점의 실제 버전 정책이 바뀌면 버전 번호만 조정하고 아래 동작 계약은 변경하지 않는다.

## 2. 사용자 요구사항

1. 일반 클릭으로 한 개의 레이어 트리 항목을 선택한다.
2. 실제 Paint Layer·Mask·Adjustment 활성화는 같은 항목의 더블클릭으로만 변경한다.
3. 실제 활성 항목은 파란 배경이 아닌 별도의 기호로 표시한다.
4. `-` 버튼과 `Del` 키는 활성 항목이 아니라 선택된 항목을 삭제한다.
5. 레이어 트리 외부 클릭에 의한 선택 해제 동작은 유지한다.
6. 더블클릭은 첫 번째 클릭의 선택을 포함해 실제 두 번의 클릭으로 완료한다.
7. 첫 클릭 선택을 확정하기 위해 세 번째 클릭을 요구하거나, 단일클릭 결과를 지연해서는 안 된다.

## 3. 용어와 상태 구분

### 3.1 레이어 트리 항목

이 문서에서 레이어 트리 항목은 다음 네 종류를 모두 포함한다.

- Paint Layer
- Layer Mask
- Adjustment
- Adjustment Mask

네 종류는 현재 하나의 선택 모델을 공유하므로 선택·활성화·삭제 UX도 같은 규칙을 적용한다.

### 3.2 선택 상태

선택 상태는 사용자가 다음 UI 작업의 대상으로 지정한 항목 집합이다.

- 일반 클릭의 단일 선택
- `Ctrl+클릭`의 개별 추가·해제
- `Shift+클릭`의 범위 선택
- `-`·`Del` 삭제 대상

현재의 `PP_PG_layer.ui_selected`, `PP_PG_mask.ui_selected`, `PP_PG_layer_adjustment.ui_selected`를 그대로 사용한다. 새 저장 속성이나 데이터 마이그레이션은 추가하지 않는다.

### 3.3 활성 상태

활성 상태는 실제 Texture Paint 편집 대상을 의미한다.

- `settings.active_layer_index`
- `layer.active_adjustment_index`
- `settings.active_mask_uuid`
- 실제 활성 Layer·Mask Image와 Texture Paint Slot

활성 상태는 선택 상태와 독립적이다. 선택이 모두 해제돼도 활성 Paint Image는 유지되고, 활성 기호도 계속 표시되어야 한다.

### 3.4 표시 계약

- 파란 배경: 선택된 항목
- `RADIOBUT_ON` 계열 기호: 실제 활성 편집 대상
- 눈 아이콘: 합성 표시 여부
- 자물쇠 아이콘: 읽기 전용 또는 편집 제한
- 펼침 삼각형: 자식 행 펼침 여부

하나의 시각 표시가 두 상태를 동시에 뜻하지 않도록 한다.

## 4. 현재 구현 분석

### 4.1 선택과 활성화가 한 함수에 묶여 있음

`tools/projection_paint/blender/layers.py`의 `_select_and_activate_layer_tree_item()`은 다음 두 작업을 한 번에 수행한다.

1. `_select_layer_tree_item()`으로 `ui_selected` 변경
2. `_activate_layer_tree_item()`으로 실제 Paint Image 변경

`PP_OT_layer_select`, `PP_OT_layer_mask_select`, `PP_OT_layer_adjustment_select`, `PP_OT_adjustment_mask_select`의 UI `invoke()`가 모두 이 결합 함수를 호출하므로 현재는 일반 클릭 한 번만으로 Paint Target이 바뀐다.

### 4.2 파란 배경은 활성 상태를 표시하지 않음

`tools/projection_paint/blender/panels.py`의 각 행은 다음 선택 함수 결과를 `depress`와 `emboss`에 사용한다.

- `layer_row_selected()`
- `layer_mask_row_selected()`
- `adjustment_row_selected()`
- `adjustment_mask_row_selected()`

이 값들은 `ui_selected`만 확인한다. 실제 활성 값과 연결되지 않으므로 외부 클릭으로 선택만 해제되면 Paint Image가 계속 활성 상태여도 파란 배경은 사라진다.

### 4.3 선택 해제는 전역 포인터 추적기가 담당함

`PP_OT_layer_ui_pointer_tracker`는 실제 `LEFTMOUSE` 입력을 기록한다. 레이어 선택 버튼이 소비하지 않은 클릭이 확정되면 `_clear_pending_layer_ui_selection()`이 전체 `ui_selected`와 선택 앵커를 지운다.

이 동작은 이번 변경에서도 유지한다. 단, 선택 항목을 사용하는 `-` 버튼은 삭제 대상을 읽기 전에 포인터 스냅샷을 소비하고 선택을 복원해야 한다.

### 4.4 삭제는 현재 활성 트리 항목을 사용함

`PP_OT_layer_remove`는 `active_layer_tree_item(settings)`를 호출해 삭제 대상을 결정한다. 따라서 파란 배경으로 보이는 선택 항목과 실제 삭제 대상이 다를 수 있다.

삭제 버튼 활성 여부도 `settings.active_layer_index`로 계산한 `active_layer`를 기준으로 한다. 이번 변경에서는 선택 집합의 존재와 삭제 가능 여부를 기준으로 교체해야 한다.

### 4.5 현재 `Del`은 선택 삭제 단축키가 아님

`tools/projection_paint/blender/lifecycle.py`는 `Window` Keymap의 `Del`을 `projection_paint.layer_adjustment_remove_hovered`에 연결한다. 이 연산자는 마우스 아래의 Adjustment 보조 항목만 삭제하며 선택된 Paint Layer 전체를 처리하지 않는다.

전역 `Window` Keymap에서 선택 존재 여부만 검사하도록 바꾸면 Projection Paint 선택이 남아 있는 동안 Blender의 다른 Delete 입력을 가로챌 수 있다. 따라서 `Del`은 마우스가 Projection Paint 레이어 트리 영역에 있을 때만 소비해야 한다.

### 4.6 기존 문서·테스트 계약과 충돌함

다음 문서는 현재의 단일클릭 즉시 활성화 계약을 명시하고 있다.

- `docs/migration/LAYER_SELECTION_RELEASE_NOTE.md`
- `docs/migration/IMPLEMENTATION_STATUS.md`
- `docs/TESTING.md`
- `docs/COLMAP_Projection_Paint_Blender_기본도구_완전호환_구조변경_계획서.md`

다음 테스트도 같은 계약을 고정한다.

- `tests/projection_paint/blender_background/test_layer_ui.py`
- `tests/projection_paint/gui_layer_selection_actual.py`
- `tests/projection_paint/gui_layer_panel.py`
- `tests/projection_paint/blender_background/test_lifecycle_registry.py`

구현 시 제품 코드만 바꾸지 않고 이 계약을 새 UX에 맞게 함께 갱신해야 한다.

## 5. 목표

### 5.1 기능 목표

- 일반 클릭과 `Ctrl`·`Shift` 클릭은 선택 상태만 변경한다.
- 수정키 없는 같은 항목의 두 번째 클릭만 실제 활성화를 수행한다.
- 첫 번째 클릭 결과는 즉시 파란 배경으로 표시한다.
- 두 번째 클릭에서 실제 활성 기호와 Paint Image가 함께 변경된다.
- `-`와 `Del`은 동일한 선택 기반 삭제 서비스를 사용한다.
- 선택되지 않은 활성 항목은 삭제하지 않는다.
- 외부 클릭으로 선택이 해제돼도 활성 상태와 기호는 유지한다.
- 복수 선택 중 Paint Stroke 차단 계약은 유지한다.
- 읽기 전용 Original, Composite 편집 잠금, 마지막 Paint Layer 교체 계약을 유지한다.

### 5.2 안정성 목표

- `Del`이 다른 Blender 작업공간·Editor·3D Viewport의 삭제 기능을 가로채지 않는다.
- 삭제는 한 번의 Blender Undo 단계로 처리한다.
- 복수 선택 삭제 중 일부만 삭제되는 부분 성공을 만들지 않는다.
- 부모와 자식이 함께 선택돼도 같은 자원을 두 번 삭제하지 않는다.
- 삭제 실패 시 Layer Property, Image, Shader Node, Paint Target과 선택 상태를 호출 전 상태로 유지한다.
- 클릭 판정용 Runtime 상태가 `.blend`에 저장되지 않는다.
- 파일 로드·Undo·Redo·Extension 해제 뒤 오래된 더블클릭 후보가 남지 않는다.

## 6. 비목표

- Layer 합성 순서와 Shader 계산 방식 변경
- Composite 생성·해제 동작 변경
- Layer 개인 보기·Mask 단독 보기 변경
- Paint Brush, Clone, Stencil, Camera Frame Source 변경
- Modifier·Align 또는 공용 Snapshot 구조 변경
- `+` 버튼의 Layer 생성 방식 변경
- 상·하 이동 버튼의 대상 정책 변경
- 기존 `.blend` 데이터 자동 마이그레이션
- 기존 레이어 Pixel 또는 Image 재생성

상·하 이동 버튼은 이번 요구 범위에 포함하지 않는다. 현재처럼 활성 Paint Layer 기준으로 유지하며, 선택 기반 이동이 필요하면 별도 UX 변경으로 검토한다.

## 7. 변경 후 사용자 동작 계약

| 입력 | 선택 결과 | 활성 결과 | Paint 대상 |
|---|---|---|---|
| 항목 일반 클릭 1회 | 해당 항목만 선택 | 변경 없음 | 기존 활성 대상 유지 |
| 같은 항목 일반 클릭 2회 | 해당 항목 단일 선택 | 해당 항목 활성화 | 클릭한 항목으로 변경 |
| 다른 항목 일반 클릭 | 기존 선택 해제 후 새 항목 선택 | 변경 없음 | 기존 활성 대상 유지 |
| `Ctrl+클릭` | 개별 선택 토글 | 변경 없음 | 기존 활성 대상 유지 |
| `Shift+클릭` | 앵커 기준 범위 선택 | 변경 없음 | 기존 활성 대상 유지 |
| 레이어 트리 외부 클릭 | 모든 선택 해제 | 변경 없음 | 기존 활성 대상 유지 |
| `-` | 선택 항목 삭제 | 활성 항목이 삭제된 경우에만 대체 대상 활성화 | 검증된 활성 대상 유지 |
| 레이어 트리 위 `Del` | 선택 항목 삭제 | `-`와 동일 | `-`와 동일 |
| 다른 Editor·Viewport의 `Del` | 변경 없음 | 변경 없음 | Blender 기본 Delete로 전달 |

### 7.1 선택과 Paint 대상이 다른 상태

일반 클릭으로 다른 항목을 선택해도 실제 Paint Target은 기존 활성 기호가 표시된 항목이다. 사용자가 더블클릭하기 전까지 Viewport Stroke는 기존 활성 Paint Target에 적용된다.

이는 선택과 활성화를 분리한 결과이며 오류가 아니다. 활성 기호를 항상 표시해 실제 Stroke 대상을 알 수 있게 한다.

### 7.2 수정키 더블클릭

`Ctrl` 또는 `Shift`가 눌린 클릭은 선택 편집 전용으로 처리한다. 수정키가 포함된 연속 클릭은 활성화를 발생시키지 않는다. 실제 활성화는 수정키 없는 같은 항목의 더블클릭만 허용한다.

## 8. 선택 처리 구조

### 8.1 선택 전용 진입점

현재 `_select_and_activate_layer_tree_item()`의 책임을 다음 두 경로로 분리한다.

1. UI 선택 경로
   - `_select_layer_tree_item()`만 호출
   - `ui_selected`와 `layer_selection_anchor_uuid`만 변경
   - Paint Image와 Mask Mode를 변경하지 않음
2. 활성화 경로
   - 검증된 트리 항목에 `_activate_layer_tree_item()` 호출
   - 실제 Paint Image, Mask Mode, Adjustment 상태를 변경
   - 선택 집합은 해당 항목 하나로 정규화

결합 함수는 제품 코드에서 제거하거나 이름과 계약을 명확히 바꾼다. UI `invoke()`가 다시 결합 함수를 호출하지 못하도록 Background 회귀로 고정한다.

### 8.2 스크립트 호출 호환성

`bpy.ops.projection_paint.layer_select("EXEC_DEFAULT")` 같은 직접 실행은 기존 자동화와 내부 초기화 경로의 호환성을 위해 즉시 선택·활성화 동작을 유지한다.

- 실제 UI `invoke()`: 한 번 클릭 선택, 더블클릭 활성화
- 명시적 `execute()`: 대상 하나를 선택하고 즉시 활성화

Mask와 Adjustment 선택 연산자도 같은 원칙을 적용한다. UI와 스크립트 경로를 혼동하지 않도록 각 함수의 한국어 문서화 문자열에 차이를 명시한다.

## 9. 더블클릭 판정 구조

### 9.1 핵심 원칙

첫 클릭 결과를 지연하는 Timer 방식은 사용하지 않는다.

1. 첫 번째 실제 클릭 완료 시 해당 항목을 즉시 선택한다.
2. Window·Scene·항목 키·시각을 Runtime 후보로 기록한다.
3. 같은 항목의 두 번째 수정키 없는 클릭이 Blender 사용자 설정의 더블클릭 제한 시간 안에 들어오면 활성화한다.
4. 두 번째 클릭도 같은 호출 안에서 선택을 단일 항목으로 확정한 뒤 활성화한다.

따라서 첫 클릭 선택과 두 번째 클릭 활성화가 합쳐져 실제 두 번의 클릭으로 완료된다.

### 9.2 Runtime 후보 데이터

저장하지 않는 Runtime 구조에 다음 값만 보관한다.

- Window Pointer 정수 키
- Scene Pointer 정수 키
- 레이어 트리 항목 키
- 마지막 클릭 완료 시각
- 수정키 없는 일반 클릭 여부

Blender ID 객체를 강하게 참조하지 않는다. Pointer 정수와 문자열 키만 사용한다.

### 9.3 판정 시간

고정된 임의 시간값을 추가하지 않는다. `PreferencesInput.mouse_double_click_time`을 읽어 사용자의 Blender 설정과 동일한 시간 범위를 적용한다.

이벤트가 `DOUBLE_CLICK`으로 직접 전달되면 즉시 같은 항목 여부를 확인한다. Blender UI Operator가 두 번의 `RELEASE`로 전달하는 환경도 지원하기 위해 Runtime 후보 기반 판정을 함께 둔다.

### 9.4 후보 무효화 조건

다음 조건에서는 직전 후보를 폐기하고 현재 클릭을 새 첫 클릭으로 처리한다.

- 다른 Window
- 다른 Scene
- 다른 레이어 트리 항목
- 더블클릭 제한 시간 초과
- `Ctrl` 또는 `Shift`가 포함된 클릭
- 레이어 트리 외부 클릭
- Workspace 전환
- 파일 로드
- Undo·Redo 재구성
- Extension 등록 해제
- 대상 UUID가 더 이상 존재하지 않음

### 9.5 좌표 판정

같은 항목 키가 가장 중요한 기준이다. 포인터 좌표는 현재 UI 배율 기반 허용 오차를 보조 조건으로 사용한다. 같은 항목 이름 버튼 안에서 발생한 자연스러운 손떨림은 더블클릭을 방해하지 않아야 한다.

## 10. 활성 기호 UI

### 10.1 기호 위치

레이어 트리의 펼침·들여쓰기 영역과 항목 이름 사이에 고정 폭의 활성 표시 열을 둔다.

- 활성 항목: Blender 내장 `RADIOBUT_ON` 기호
- 비활성 항목: 같은 폭의 빈 자리

모든 행에 같은 폭을 예약해 활성 항목이 바뀔 때 이름 정렬이 흔들리지 않게 한다.

### 10.2 활성 판정

기호는 `ui_selected`를 읽지 않는다. `active_layer_tree_item(settings)`와 각 행의 UUID를 비교해 정확한 활성 행 하나에만 표시한다.

- 일반 Layer 활성: Layer 행에 표시
- Layer Mask 활성: 해당 Mask 행에 표시
- Adjustment 활성: 해당 Adjustment 행에 표시
- Adjustment Mask 활성: 해당 Mask 행에 표시

외부 클릭으로 선택이 해제돼도 기호는 유지한다.

### 10.3 파란 배경

파란 배경은 현재와 같이 `ui_selected`를 기준으로 표시한다.

- 단일 선택: 한 행만 파란 배경
- 복수 선택: 선택된 모든 행이 파란 배경
- 선택 없음: 파란 배경 없음
- 활성과 선택이 같은 행: 기호와 파란 배경을 함께 표시
- 활성과 선택이 다른 행: 활성 행에는 기호만, 선택 행에는 파란 배경만 표시

## 11. 선택 기반 삭제 구조

### 11.1 공통 삭제 서비스

`-` 버튼과 `Del` 키가 동일한 삭제 수집·검증·실행 함수를 사용하도록 한다.

공통 서비스의 처리 순서는 다음과 같다.

1. 선택된 모든 트리 항목 키 수집
2. 현재 Scene에서 UUID를 다시 해석
3. 부모·자식 중복 삭제 대상 정규화
4. 전체 대상 삭제 가능 여부 사전 검증
5. 필요한 확인창 한 번 표시
6. 하나의 Undo 단위로 삭제 수행
7. Shader Graph와 Paint Target 한 번 재조정
8. 선택·활성 상태 후처리

`-`와 `Del`이 서로 다른 삭제 규칙을 갖지 않게 한다.

### 11.2 선택 대상 수집

삭제 대상은 `active_layer_tree_item()`이 아니라 각 PropertyGroup의 `ui_selected=True` 상태에서 수집한다.

- 선택 없음: `-` 비활성, `Del` 통과
- 선택 1개: 해당 항목만 삭제
- 복수 선택: 선택된 삭제 가능 항목을 한 작업으로 삭제
- 활성 항목이 선택되지 않음: 활성 항목 보존

### 11.3 부모·자식 정규화

다음 중복은 상위 항목 한 번만 삭제한다.

- Layer와 그 Layer Mask가 함께 선택됨: Layer만 삭제 대상
- Layer와 그 Adjustment가 함께 선택됨: Layer만 삭제 대상
- Layer와 그 Adjustment Mask가 함께 선택됨: Layer만 삭제 대상
- Adjustment와 그 Adjustment Mask가 함께 선택됨: Adjustment만 삭제 대상

독립된 다른 Layer·Mask·Adjustment는 모두 삭제 대상으로 유지한다.

### 11.4 삭제 순서

Collection Index 변경으로 다른 대상을 잘못 지우지 않도록 UUID를 권위로 사용한다.

- 서로 독립된 Mask·Adjustment 자식은 깊은 항목부터 처리
- 같은 Layer의 Adjustment는 현재 Index의 내림차순으로 처리
- 최상위 Layer는 현재 Index의 내림차순으로 처리
- 모든 논리 삭제 후 Shader Graph를 한 번 재조정

삭제 도중 Index를 장기 보관하지 않고 각 단계에서 UUID를 다시 확인한다.

### 11.5 삭제 사전 검증

다음 기존 보호 계약을 유지한다.

- Composite Layer는 `Composite 해제`로만 제거
- Composite 처리·잠금 상태의 Source Layer는 제거 금지
- 존재하지 않거나 소유권이 불명확한 Image·Node는 추측 삭제 금지
- 읽기 전용 Original 관련 기존 삭제 정책 유지
- 마지막 쓰기 가능한 Paint Layer는 완전 제거 대신 새 빈 `Layer 1` 교체 계약 유지

복수 선택 중 하나라도 삭제 불가능하면 전체 작업을 중단하고 이유를 표시한다. 삭제 가능한 일부만 조용히 지우는 부분 성공은 허용하지 않는다.

### 11.6 확인창

- Mask·Adjustment만 선택: 기존처럼 즉시 삭제
- Paint Layer가 하나 이상 포함: 확인창 한 번 표시
- 마지막 쓰기 가능한 Paint Layer가 포함: 새 해상도의 `Layer 1` 교체 내용을 명확히 표시
- 복수 Layer: 삭제할 Layer·Mask·Adjustment 개수와 활성 대상 포함 여부 표시

확인 취소 시 선택과 활성 상태를 그대로 유지한다.

### 11.7 `-` 버튼의 포인터 스냅샷

`-` 버튼을 누르는 입력은 레이어 이름 버튼이 아니므로 현재 선택 해제 Timer가 먼저 선택을 지울 수 있다.

`PP_OT_layer_remove.invoke()` 시작에서 `_restore_layer_ui_pointer_selection(context, event)`와 같은 검증된 소비 경로를 사용해 버튼 클릭 직전 선택 스냅샷을 복원한다. 복원된 선택을 삭제 대상 Snapshot으로 확정한 뒤 외부 클릭 해제 Timer가 같은 입력을 다시 처리하지 못하게 한다.

삭제 확인창이 열려 있는 동안 새 전역 선택을 다시 읽지 않는다. 확인 시점에는 invoke 단계에서 고정한 UUID 대상만 재검증한다.

## 12. 삭제 후 활성 상태

### 12.1 활성 항목이 삭제되지 않은 경우

- 기존 활성 Index·UUID·Paint Image 유지
- 활성 기호 위치 유지
- 삭제된 선택 항목의 `ui_selected`만 사라짐
- 성공 후 남은 선택은 없음

### 12.2 활성 자식이 삭제된 경우

- Layer Mask 삭제: 소유 Layer Image를 활성화
- Adjustment Mask 삭제: 소유 Adjustment 또는 소유 Layer Image를 활성화
- Adjustment 삭제: 소유 Layer Image를 활성화

실제 Texture Paint Slot과 활성 기호를 같은 호출에서 함께 갱신한다.

### 12.3 활성 Layer가 삭제된 경우

현재 표시 순서에서 삭제 위치에 가장 가까운 남은 편집 가능 Layer를 선택한다.

1. 삭제된 위치에 새로 들어온 다음 Layer
2. 없으면 바로 이전 Layer
3. 마지막 쓰기 가능 Layer 교체가 발생하면 새 `Layer 1`

대체 대상이 검증되지 않으면 추측 활성화하지 않고 명확한 오류로 Transaction을 실패시킨다.

### 12.4 선택 상태 후처리

삭제 성공 뒤에는 선택 집합과 앵커를 비운다. 활성 기호만 남기므로 사용자가 다음 항목을 명시적으로 다시 선택하게 한다.

## 13. `Del` 키 입력 범위

### 13.1 기본 원칙

`Del`은 Projection Paint 레이어 트리에서만 선택 삭제를 수행한다. 다음 위치에서는 반드시 Blender 기본 입력으로 전달한다.

- 3D Viewport `WINDOW` 영역
- Outliner
- Shader Editor
- Image Editor
- 다른 Workspace
- 다른 N 패널 Category
- Projection Paint Layers 패널 밖

### 13.2 전용 단축키 연산자

기존 `projection_paint.layer_adjustment_remove_hovered`의 전역 Hover 삭제 계약을 선택 삭제 계약으로 교체한다. 필요하면 다음처럼 얇은 전용 연산자를 둔다.

- 역할: 현재 마우스 문맥이 Projection Paint 레이어 트리인지 확인
- `poll`: 선택이 있고 현재 트리 문맥이 정확히 확인될 때만 참
- 실행: 공통 선택 삭제 서비스 호출
- `invoke`: 호출 직전 문맥을 다시 검증하고 달라졌으면 `PASS_THROUGH`

선택 존재 여부만으로 `Window` 전역 `Del`을 소비하지 않는다.

### 13.3 문맥 판정

다음 증거 중 하나로 마우스가 관리 레이어 트리 위에 있음을 확인한다.

- `context.button_operator`가 Projection Paint 레이어 트리의 선택·펼침·미리보기 연산자
- `context.button_pointer`가 현재 Scene의 Layer·Mask·Adjustment PropertyGroup
- 현재 `VIEW_3D`의 `UI` Region이며 검증된 Projection Paint 레이어 행 Context Pointer가 존재

패널의 사각형 좌표를 추측하거나 화면 해상도·UI 배율에 고정된 좌표를 사용하지 않는다.

## 14. 복수 선택과 Paint 입력

현재 `layer_tree_selection_count(settings) > 1`이면 Projection Paint 작업공간의 Pixel·Mask Stroke를 차단한다. 이 계약은 유지한다.

- 복수 선택: Stroke 차단
- 단일 선택이 활성과 다름: 기존 활성 대상에 Stroke 허용
- 선택 없음: 기존 활성 대상에 Stroke 허용
- 읽기 전용 Original 활성: 기존 읽기 전용 차단 유지
- Clone `Ctrl+LMB` Source 지정과 Industry Compatible Navigation: 기존 통과 계약 유지

선택과 활성이 다르다는 이유만으로 새 Stroke 차단 규칙을 추가하지 않는다. 활성 기호가 실제 Paint Target을 알리는 권위가 된다.

## 15. Runtime과 저장 경계

### 15.1 저장 상태

다음 기존 값만 `.blend`에 저장한다.

- `ui_selected`
- `active_layer_index`
- `active_adjustment_index`
- `active_mask_uuid`
- 기존 Layer·Mask·Image·Preview 상태

새 Schema Version과 자동 마이그레이션은 추가하지 않는다.

### 15.2 저장하지 않는 상태

다음 값은 Runtime에서만 관리한다.

- 마지막 클릭 Window·Scene·항목 키
- 마지막 클릭 시각
- 삭제 버튼 invoke 단계의 임시 선택 UUID Snapshot
- 현재 포인터 입력 후보

이 값은 저장·Undo 대상이 아니며 파일 로드와 Extension 해제에서 반드시 비운다.

### 15.3 Undo·Redo

- 선택 변경과 활성화는 Pixel 편집 Undo를 만들지 않는다.
- 삭제는 한 번의 Blender Undo 단계로 기록한다.
- 삭제 Undo 후 Layer·Mask·Adjustment, Image, Shader Node, 선택·활성 기호가 호출 전 상태로 복구돼야 한다.
- Undo·Redo 직후 더블클릭 후보는 폐기한다.
- Undo로 복구된 `ui_selected`와 활성 값은 저장 상태를 그대로 표시하며 자동 마이그레이션하지 않는다.

## 16. 파일별 변경 계획

### 16.1 `tools/projection_paint/blender/layers.py`

- 선택 전용과 활성화 전용 진입점 분리
- 실제 두 클릭을 판정하는 Window별 Runtime 후보 추가
- 후보 등록·소비·무효화 함수 추가
- 네 종류 선택 연산자의 UI `invoke()`를 단일클릭 선택·더블클릭 활성화로 변경
- 네 종류 선택 연산자의 `execute()` 즉시 활성화 호환 유지
- 활성 트리 항목 키 판정 Helper 추가
- 선택 항목 수집·부모 자식 정규화 Helper 추가
- 선택 기반 삭제 사전 검증·실행 Transaction 추가
- `PP_OT_layer_remove`를 선택 기반 공통 삭제 경로로 변경
- `-` 클릭 전 선택 Snapshot 복원
- `Del` 전용 문맥 검사 연산자 추가 또는 기존 Hover 연산자 교체
- 모든 수정·추가 함수에 한 줄 내외 한국어 문서화 문자열 작성
- 해결 후 임시 디버그 로그 제거

### 16.2 `tools/projection_paint/blender/panels.py`

- 각 트리 행에 고정 폭 활성 기호 열 추가
- 파란 배경은 기존 `ui_selected` 기준 유지
- `-` 버튼 활성 조건을 삭제 가능한 선택 집합 기준으로 변경
- 선택 없음·보호 항목 선택·혼합 선택 상태의 버튼 활성 조건 반영
- 활성 기호와 들여쓰기·펼침 삼각형 정렬 유지

### 16.3 `tools/projection_paint/blender/properties.py`

새 영구 속성을 추가하지 않는다. 기존 `ui_selected`와 활성 상태 속성의 문서화가 실제 새 계약과 다르면 설명만 수정한다.

### 16.4 `tools/projection_paint/blender/lifecycle.py`

- 기존 Adjustment Hover 전용 `Del` 등록 교체
- 선택 삭제 전용 `Del` Keymap을 정확히 한 번 등록
- Extension 해제 시 Keymap과 더블클릭 Runtime 후보 제거
- 제한 등록 문맥과 재등록에서 중복 Keymap 방지

### 16.5 `tests/projection_paint/blender_background/test_layer_ui.py`

- 일반 클릭은 선택만 변경하고 활성은 유지하는 계약
- 더블클릭 두 번째 입력에서만 활성화하는 계약
- 수정키 클릭은 활성화하지 않는 계약
- 선택과 활성 기호 판정 독립성
- 선택 기반 단일·복수 삭제 정규화
- 활성 포함·미포함 삭제 후 대체 대상
- 마지막 Layer 교체와 보호 대상 원자 중단
- 스크립트 `EXEC_DEFAULT` 즉시 활성화 호환

### 16.6 `tests/projection_paint/gui_layer_selection_actual.py`

실제 `Window.event_simulate`로 다음 순서를 검증한다.

1. 기준 활성 Layer와 기호 확인
2. 다른 Layer 첫 클릭
3. 파란 배경만 이동하고 활성 기호·Paint Image는 유지되는지 확인
4. 같은 행 두 번째 클릭
5. 정확히 두 번째 입력에서 활성 기호·Paint Image가 이동하는지 확인
6. 세 번째 클릭 없이 활성화 완료 확인
7. `Ctrl`·`Shift` 복수 선택 중 활성 기호 유지
8. 외부 클릭 뒤 파란 배경만 제거되고 활성 기호 유지
9. 선택된 비활성 Layer의 `-` 삭제
10. 선택된 비활성 Layer의 `Del` 삭제
11. Viewport `Del`이 Projection Paint 삭제를 실행하지 않는지 확인

### 16.7 `tests/projection_paint/gui_layer_panel.py`

- Layer·Mask·Adjustment·Adjustment Mask의 활성 기호 위치
- 파란 선택 배경과 활성 기호 동시·분리 표시
- 외부 클릭 뒤 활성 기호 지속
- 들여쓰기와 Composite 자식 표시 정렬

### 16.8 `tests/projection_paint/blender_background/test_lifecycle_registry.py`

- `Del` 선택 삭제 Keymap 중복 없음
- 다른 Projection Paint Keymap과 충돌 없음
- Extension 해제 시 Keymap 제거
- 파일 로드·Undo·Redo·해제 시 클릭 후보 제거

### 16.9 문서

- `docs/TESTING.md`의 단일클릭 즉시 활성화 설명 교체
- `docs/migration/IMPLEMENTATION_STATUS.md`에 새 버전 계약 추가
- 새 Release Note에 선택·활성·삭제 변경 기록
- `docs/README.md`에 Release Note 링크 추가
- Blender 기본 도구 완전 호환 계획서의 “단일 Layer 선택 시 Target 활성” 표현을 “더블클릭 활성화 완료 시”로 정정

기존 Release Note는 당시 구현 기록이므로 내용을 덮어쓰지 않는다. 새 Release Note에서 0.3.2 계약이 0.3.23에서 교체됐음을 명시한다.

## 17. 구현 단계

### 단계 1. 실패 회귀 고정

- 기존 한 번 클릭 즉시 활성화가 새 기대값에서 실패하는 Background 테스트 작성
- 실제 두 번 클릭 전에는 활성 Paint Image가 바뀌지 않는 GUI 테스트 작성
- 선택된 비활성 Layer 대신 활성 Layer가 삭제되는 현재 실패 재현
- 현재 `Del`이 Hover Adjustment만 처리하는 상태와 Viewport 전역 충돌 경계 고정

완료 조건: 변경 전 코드에서 새 선택·활성·삭제 계약이 명확히 실패해야 한다.

### 단계 2. 선택과 활성화 분리

- 네 트리 항목의 UI `invoke()`를 선택 전용으로 변경
- 스크립트 `execute()` 호환 경로 유지
- 단일·Ctrl·Shift 선택과 외부 클릭 해제 회귀 통과
- Paint Target이 첫 클릭에서 바뀌지 않는지 확인

완료 조건: 일반 클릭은 `ui_selected`만 바꾸고 실제 Paint Image는 유지해야 한다.

### 단계 3. 두 클릭 활성화

- Window별 Runtime 클릭 후보 구현
- Blender 더블클릭 설정 시간 사용
- 같은 항목 두 번째 클릭에서만 활성화
- 수정키·다른 행·시간 초과·외부 클릭 후보 무효화
- Load·Undo·Redo·해제 정리 연결

완료 조건: 실제 두 번의 클릭으로 활성화되고 한 번이나 세 번으로 오판하지 않아야 한다.

### 단계 4. 활성 기호 UI

- 정확한 활성 트리 키 Helper 구현
- 네 종류 행에 고정 폭 기호 열 적용
- 파란 배경 선택 표시 유지
- 선택 없음·선택과 활성 불일치·Mask 활성 상태 화면 검증

완료 조건: 외부 클릭으로 파란 배경이 사라져도 실제 Paint 대상 기호는 남아야 한다.

### 단계 5. 선택 기반 삭제

- 선택 UUID 수집과 부모·자식 정규화 구현
- 전체 사전 검증과 단일 Undo Transaction 구현
- `-` 입력 Snapshot 복원
- 삭제 확인·취소·실패 Rollback
- 활성 대상 삭제 후 결정적 대체 활성화

완료 조건: 선택되지 않은 활성 Layer를 삭제하지 않고 선택된 항목만 정확히 삭제해야 한다.

### 단계 6. `Del` 문맥 격리

- Hover Adjustment 전용 경로를 선택 삭제 경로로 교체
- Projection Paint 레이어 트리 문맥에서만 실행
- 다른 N 패널·Editor·Workspace·Viewport 입력 통과
- 등록·해제·재등록 중복 회귀 통과

완료 조건: 레이어 트리 위 `Del`과 Blender 기본 Delete가 서로 간섭하지 않아야 한다.

### 단계 7. 전체 회귀와 문서

- Projection Paint Layer·Shader·Mask·Composite·Paint Tool 회귀 실행
- 실제 GUI 클릭·삭제·Undo·Redo 실행
- 문서와 Release Note 갱신
- 임시 디버그 출력 제거

완료 조건: 기존 Paint·Clone·Stencil·Composite 동작과 새 Layer UX가 함께 통과해야 한다.

### 단계 8. 패키징과 설치

- 버전과 Manifest 일치 확인
- Production ZIP 빌드
- 격리 설치본 등록·해제·Keymap 검증
- 실제 `user_default`에 새 패키지 설치
- 기존에 열려 있던 Blender는 재시작 안내
- 설치본에서 최종 GUI 회귀

완료 조건: Source 테스트뿐 아니라 실제 설치된 Extension에서도 같은 동작을 확인해야 한다.

## 18. 자동 회귀 행렬

### 18.1 선택·활성 입력

| 기준 상태 | 입력 | 기대 선택 | 기대 활성 |
|---|---|---|---|
| Layer 1 활성 | Layer 2 클릭 1회 | Layer 2 | Layer 1 |
| Layer 1 활성 | Layer 2 클릭 2회 | Layer 2 | Layer 2 |
| Layer 1 활성 | Layer 2 클릭 후 시간 초과 클릭 | Layer 2 | Layer 1 |
| Layer 1 활성 | Layer 2 클릭 후 Layer 3 클릭 | Layer 3 | Layer 1 |
| Layer 1 활성 | Ctrl+Layer 2·3 | Layer 2·3 | Layer 1 |
| Layer 1 활성 | Shift 범위 선택 | 범위 전체 | Layer 1 |
| Layer 2 활성·선택 | 외부 클릭 | 없음 | Layer 2 |
| Mask 활성 | 외부 클릭 | 없음 | 동일 Mask |

### 18.2 삭제

| 선택 | 활성 | 동작 | 기대 결과 |
|---|---|---|---|
| Layer 2 | Layer 1 | `-` | Layer 2만 삭제, Layer 1 유지 |
| Layer 2 | Layer 1 | `Del` | Layer 2만 삭제, Layer 1 유지 |
| Layer 1 | Layer 1 | `-` | Layer 1 삭제, 인접 Layer 활성 |
| Layer Mask | 소유 Layer | `Del` | Mask만 삭제, Layer 활성 |
| Adjustment | 다른 Layer | `-` | Adjustment만 삭제, 활성 유지 |
| Layer+자기 Mask | 다른 Layer | `Del` | Layer Bundle 한 번 삭제 |
| Adjustment+자기 Mask | 다른 Layer | `Del` | Adjustment Bundle 한 번 삭제 |
| 여러 독립 Layer | 그중 하나 | `Del` | 모두 삭제, 인접 Layer 활성 |
| 마지막 쓰기 가능 Layer | 동일 Layer | `-` | 새 Layer 1 교체·활성 |
| Composite+일반 Layer | 다른 Layer | `Del` | 전체 중단, 아무것도 삭제 안 됨 |
| 선택 없음 | 임의 활성 | `Del` | Projection Paint 무변경, 입력 통과 |

### 18.3 키맵 격리

- Projection Paint Layers 트리의 행 위 `Del`: 선택 삭제
- Projection Paint 3D Viewport `Del`: Blender 기본 동작
- Outliner `Del`: Blender 기본 동작
- Shader Editor `Del`: Blender 기본 동작
- 일반 Blender Workspace `Del`: Blender 기본 동작
- Modifier·Align Workspace `Del`: 각 도구 또는 Blender 기본 동작
- Extension 해제 후 `Del`: Projection Paint 연산자 없음

### 18.4 Undo·Redo

- 단일 Layer 삭제 → Undo → UUID·Image Pointer·Pixel Hash·활성 기호 복구
- 복수 선택 삭제 → Undo 한 번 → 전체 복구
- Redo 한 번 → 전체 재삭제
- Mask·Adjustment 삭제 → Undo·Redo 후 Shader 연결 복구
- 마지막 Layer 교체 → Undo·Redo 후 해상도와 Paint Target 일치
- Undo 직후 같은 위치 클릭 1회가 오래된 더블클릭 후보로 활성화되지 않음

## 19. 수동 GUI 검증

1. Projection Paint 작업공간 진입
2. 현재 활성 행의 기호 확인
3. 다른 Layer를 한 번 클릭
4. 파란 배경만 이동하고 활성 기호는 기존 행에 남는지 확인
5. Viewport에서 Stroke 후 기존 활성 Layer에만 Pixel이 바뀌는지 확인
6. 선택한 Layer를 한 번 더 클릭
7. 두 번째 클릭에서 기호와 Paint Target이 이동하는지 확인
8. 세 번째 클릭 없이 Paint 가능한지 확인
9. `Ctrl`·`Shift` 복수 선택 후 Stroke 차단 확인
10. 외부 클릭 후 파란 배경만 사라지고 기호 유지 확인
11. 비활성 Layer 선택 후 `-`로 선택 Layer만 삭제
12. 다른 비활성 Layer 선택 후 레이어 트리 위 `Del`로 선택 Layer만 삭제
13. Viewport·Outliner에서 `Del`이 Projection Paint Layer를 삭제하지 않는지 확인
14. Mask·Adjustment·Adjustment Mask에 같은 선택·더블클릭·삭제 규칙 확인
15. Undo·Redo와 저장·재열기 후 활성 기호와 선택 상태 확인

사용자 실데이터는 원본을 직접 변경하지 않고 복사본에서 검증한다.

## 20. 위험 요소와 대응

| 위험 | 영향 | 대응 |
|---|---|---|
| 첫 클릭과 두 번째 클릭을 각각 독립 단일클릭으로만 인식 | 활성화 불가 | Blender 설정 시간과 Runtime 같은 항목 후보를 함께 사용 |
| 첫 클릭 처리를 지연 | 선택 반응이 느리고 세 번째 클릭 필요 | 첫 클릭 즉시 선택, 두 번째에서만 활성화 |
| 선택 해제 Timer가 `-`보다 먼저 실행 | 삭제 대상 없음 또는 잘못된 대상 | invoke 직전 선택 Snapshot 소비·복원 |
| `Del` 전역 가로채기 | Object·Node 삭제 불가 | 레이어 트리 문맥 확인 실패 시 입력 통과 |
| 부모·자식 중복 선택 | 같은 Image·Node 이중 삭제 | UUID 기반 부모 우선 정규화 |
| 복수 삭제 중 Index 이동 | 다른 Layer 삭제 | UUID 재해석과 내림차순 처리 |
| 삭제 중 일부만 성공 | Layer Graph 손상 | 전체 사전 검증·단일 Transaction·Rollback |
| 선택과 활성이 다름 | 사용자가 다른 Layer에 칠함 | 고정 활성 기호와 명확한 Tooltip·회귀 |
| 파일 로드 후 클릭 후보 잔존 | 첫 클릭이 더블클릭으로 오판 | Load·Undo·Redo·해제에서 Runtime 후보 제거 |
| 자동 마이그레이션 추가 | 업데이트마다 데이터 변경 누적 | 기존 속성 재사용, 새 Schema·마이그레이션 금지 |

## 21. 완료 기준

다음 조건을 모두 만족해야 구현 완료로 판단한다.

1. 일반 클릭 한 번은 선택만 변경한다.
2. 같은 항목의 두 번째 수정키 없는 클릭에서만 실제 활성화된다.
3. 활성화에 세 번째 클릭이 필요하지 않다.
4. 활성 항목은 정확히 하나의 기호로 표시된다.
5. 파란 배경은 선택 상태만 표시한다.
6. 외부 클릭으로 선택이 해제돼도 활성 기호와 Paint Target은 유지된다.
7. `-`와 레이어 트리 위 `Del`은 선택 항목을 삭제한다.
8. 선택되지 않은 활성 항목은 삭제되지 않는다.
9. 부모·자식 복수 선택은 자원을 한 번만 삭제한다.
10. 보호 항목이 섞인 복수 삭제는 아무것도 변경하지 않고 중단한다.
11. 활성 항목 삭제 시 인접한 유효 항목이 결정적으로 활성화된다.
12. 마지막 Paint Layer 교체 계약이 유지된다.
13. `Del`이 다른 Blender Editor·Workspace의 삭제 입력을 가로채지 않는다.
14. 복수 선택 Stroke 차단, Clone Source 지정, Industry Compatible Navigation이 유지된다.
15. 선택·활성 전환은 Pixel Undo를 만들지 않고 삭제는 한 번의 Undo 단계가 된다.
16. 새 저장 속성, 자동 데이터 마이그레이션, 임시 우회 코드가 없다.
17. Background·GUI·격리 설치본·실제 설치본 회귀가 모두 통과한다.

## 22. 최종 사용자 경험

변경 완료 후 사용자는 레이어 트리를 다음처럼 해석할 수 있다.

- 파란 배경은 지금 선택한 작업 대상이다.
- 원형 기호는 실제로 페인트가 들어갈 대상이다.
- 한 번 클릭하면 삭제·복수 선택 대상을 고른다.
- 같은 항목을 한 번 더 클릭하면 실제 Paint Target이 된다.
- 트리 밖을 클릭하면 선택만 사라지고 Paint Target은 유지된다.
- `-`와 트리 위 `Del`은 파란 배경으로 선택된 항목만 삭제한다.

선택과 활성화의 의미가 UI에서 분명하게 분리되므로, 파란 배경이 사라져도 실제 활성 대상을 잃어버린 것으로 오해하지 않고 삭제 대상도 눈에 보이는 선택 상태와 일치하게 된다.
