# COLMAP Projection Paint 가시 Layer Texture Export UX 구조변경 계획서

## 1. 문서 목적

현재 `Export Texture`는 저장된 Composite Layer가 먼저 존재해야 버튼이 활성화되고, 활성 Composite Image를 기준으로 파일을 내보낸다. 이 구조를 다음 사용자 경험으로 변경한다.

> Projection Paint 데이터가 준비되면 `Export Texture`를 사용할 수 있다. 실행 시점에 실제 합성 Graph에서 눈이 켜진 Layer만 임시 이미지로 합성해 내보내고, 영구 Composite Layer와 Layer 가시성은 변경하지 않는다.

이 문서는 UX, 데이터 경계, Transaction, 실패 처리, 테스트와 구현 순서를 고정한다. 이 단계에서는 제품 코드를 수정하지 않는다.

### 후속 결정: 0.3.30 Export 입력 차단

이 계획서의 실행 중 Layer Snapshot 재검증 계약은 0.3.30에서 다음 계약으로 대체한다.

- 파일 선택을 마친 뒤 Export Operator를 `BLOCKING` Modal로 실행한다.
- `Esc` 취소를 제외한 사용자 입력은 Export가 끝날 때까지 Blender로 통과시키지 않는다.
- 시작·확정 경계의 전체 Image Pixel SHA-256 계산과 Snapshot 비교를 제거한다.
- 임시 폴더 Staging, 기존 출력 Backup, 전체 파일 원자 확정과 실패 Rollback은 그대로 유지한다.

따라서 아래 Snapshot 관련 절은 0.3.28 구현 당시의 설계 기록이며, 현재 실행 계약은 [0.3.30 Release Note](migration/TEXTURE_EXPORT_INPUT_BARRIER_RELEASE_NOTE.md)를 따른다.

## 2. 현재 구조와 문제

### 2.1 버튼 활성 조건

현재 `texture_export.py`의 Export Operator는 다음 조건을 모두 요구한다.

1. `composite_state == ACTIVE`
2. 활성 Composite UUID 존재
3. 해당 UUID의 Composite Layer가 정확히 하나 존재
4. Composite Image 존재
5. 다른 Texture Export가 실행 중이 아님

따라서 일반 Layer와 Original Layer가 정상적으로 보이더라도 Composite Layer를 먼저 생성하지 않으면 버튼이 비활성화된다.

### 2.2 Export Source 고정

현재 `capture_export_snapshot()`은 `validate_composite_session()`을 호출하고 활성 Composite Image를 Export Source로 고정한다. Layer 또는 Mask 단독 보기가 활성화된 경우에도 Export를 시작하지 못한다.

`texture_export_steps()`는 활성 Composite Image만으로 결과를 표현할 수 없다고 판정한 경우에만 임시 Bake Image를 만든다. 즉 일반 Layer Stack 자체가 Export의 기본 입력이 아니라 활성 Composite가 기본 입력이다.

### 2.3 현재 화면과 Export 결과의 차이

활성 Layer Graph는 Composite 상태에 따라 다음처럼 달라질 수 있다.

- Composite가 없으면 일반 Layer와 Original Layer 전체
- Composite가 있으면 활성 Composite와 Composite 밖에 존재하는 일반 Layer
- Composite Source 자식은 이미 Composite Image에 포함되므로 활성 Graph에서 별도 입력으로 사용하지 않음

현재 임시 Export Bake 경로는 이 전체 Graph가 아니라 활성 Composite 중심의 Layer 집합을 사용한다. 따라서 Composite 밖의 Layer, 눈 상태, Mask와 Adjustment 조합을 실제 최종 화면과 동일하게 고정하는 계약이 불명확하다.

### 2.4 가시성 오해

현재 Export 코드에는 `layer.visible`을 직접 끄는 대입이 없다. Source Layer의 눈을 끄는 동작은 영구 Composite 생성 계약에 속한다. 하지만 Export가 영구 Composite를 선행 조건으로 요구하기 때문에 사용자는 Export가 눈을 껐다고 인식하게 된다.

새 구조는 영구 Composite 생성을 요구하지 않으며, Export 전후의 모든 Layer 눈 상태가 동일함을 자동 검증한다.

## 3. 목표 사용자 경험

### 3.1 버튼 표시와 활성화

`Export Texture` 버튼은 다음 평상시 조건에서 활성화한다.

- Projection Paint 설정 존재
- Paint Target과 관리 Material 존재
- 논리 Layer가 하나 이상 존재
- Layer Graph가 손상 상태가 아님
- Composite 생성·재합성·해제 또는 다른 Export가 실행 중이 아님

영구 Composite Layer의 존재와 `composite_state == ACTIVE`는 활성 조건에서 제거한다.

처리 중 상태인 `CREATING`, `RECOMPOSITING`, `RELEASING`, `STALE`에서는 동시 데이터 변경을 막기 위해 비활성화한다. 이는 Composite 선행 조건이 아니라 Transaction 충돌 방지 조건이다.

### 3.2 실행 흐름

```text
Export Texture 클릭
→ 현재 실제 Layer Graph 검증
→ Graph에서 눈이 켜진 Layer를 UI 순서대로 고정
→ 임시 RGBA Export Image 생성
→ Mask·Adjustment·Alpha를 포함해 임시 합성
→ PNG·JPEG·UDIM·Atlas 파일 기록
→ 파일 확정 전 Layer 상태 재검증
→ 출력 파일 원자 확정
→ 임시 Image·Node·Bake 상태 제거
→ 기존 Layer Tree와 Paint 상태 그대로 유지
```

### 3.3 눈이 켜진 Layer의 의미

Export 입력은 단순히 `settings.layers`에서 `visible == True`인 모든 항목이 아니다. 현재 Material Base Color에 실제로 참여하는 활성 Layer Graph를 먼저 계산한 뒤 그 안에서 눈이 켜진 Layer만 선택한다.

- Composite가 없는 경우: 눈이 켜진 일반 Layer와 Original Layer
- Composite가 있는 경우: 눈이 켜진 활성 Composite와 Composite 밖의 일반 Layer
- 활성 Composite의 Source 자식: Composite Image에 이미 포함되므로 중복 합성하지 않음
- Layer Mask: 해당 Layer에 연결된 상태 그대로 적용
- Adjustment와 Adjustment Mask: 활성 상태와 강도를 그대로 적용
- Layer Alpha: 현재 Shader Graph와 같은 Factor로 적용

### 3.4 단독 보기와 Export

Layer·Mask 단독 보기는 화면 확인용 상태이며 Export Source 선택 기능이 아니다. 단독 보기가 켜져 있어도 버튼을 활성화하고, Export는 눈이 켜진 실제 Layer Graph 전체를 사용한다.

Export는 단독 보기 UUID, 활성 Layer, 활성 Mask와 Paint Target을 변경하지 않는다.

### 3.5 입력이 없는 경우

활성 Layer Graph에 눈이 켜진 Layer가 하나도 없으면 파일 탐색기를 열기 전에 다음 오류를 표시한다.

```text
내보낼 Layer가 없습니다. 하나 이상의 Layer 눈을 켜십시오.
```

빈 검정 또는 투명 이미지를 정상 Export로 처리하지 않는다.

### 3.6 사용자 검토용 UX 경험 단계

다음 시나리오는 구현 전에 사용자가 원하는 조작 흐름인지 검토하고, 구현 뒤 같은 순서로 실제 Blender에서 승인하기 위한 기준이다.

#### 시나리오 A. Composite가 없는 일반 Export

| 단계 | 사용자 조작 | 화면의 예상 반응 |
| --- | --- | --- |
| 1 | Projection Paint 작업공간에서 Painting Mode 진입 | 기존 Layer Tree와 Paint Target이 표시됨 |
| 2 | Composite Layer를 생성하지 않은 상태 확인 | `Layer 1`, `Original Layer` 같은 일반 Layer만 존재함 |
| 3 | 하나 이상의 Layer 눈을 켬 | 해당 Layer들이 Viewport 합성 결과에 표시됨 |
| 4 | `Export Texture` 확인 | Composite가 없어도 버튼이 활성화되어 있음 |
| 5 | `Export Texture` 클릭 | 즉시 파일 탐색기가 열리고 출력 형식과 경로를 선택할 수 있음 |
| 6 | `Export` 실행 | 진행 Overlay에 검증·임시 합성·기록·확정 단계가 순서대로 표시됨 |
| 7 | 완료 | 성공 문구와 출력 경로가 표시되며 Layer Tree에는 새 Composite가 생기지 않음 |
| 8 | Viewport와 Layer Tree 확인 | Export 전과 동일한 화면, 눈 상태, 선택과 활성 Layer가 유지됨 |

사용자가 확인할 핵심은 **Export를 위해 Composite Layer 생성 버튼을 먼저 누르지 않는 것**이다.

#### 시나리오 B. 눈 상태로 Export 범위 선택

| 단계 | 사용자 조작 | 화면의 예상 반응 |
| --- | --- | --- |
| 1 | `Original Layer`와 `Layer 1`의 눈을 켬 | 두 Layer가 합성된 결과가 Viewport에 표시됨 |
| 2 | `Layer 1`의 눈을 끔 | Viewport에는 Original 결과만 남음 |
| 3 | Export 실행 | 출력 Texture에도 Original 결과만 포함됨 |
| 4 | Export 완료 후 Layer Tree 확인 | `Layer 1`은 계속 눈이 꺼져 있고 Original은 계속 켜져 있음 |
| 5 | `Layer 1` 눈을 다시 켜고 Export | 새 출력에는 Layer 1이 다시 포함됨 |

눈 아이콘은 Export 포함 여부를 직접 결정한다. Export가 눈을 자동으로 켜거나 끄지 않는다.

#### 시나리오 C. 기존 Composite가 있는 프로젝트

| 단계 | 사용자 조작 | 화면의 예상 반응 |
| --- | --- | --- |
| 1 | 기존 Composite Layer가 눈이 켜진 상태 확인 | Composite 결과가 현재 Graph 입력 하나로 표시됨 |
| 2 | Composite 밖에 일반 Layer를 추가하고 눈을 켬 | 일반 Layer가 Composite 결과와 함께 Viewport에 합성됨 |
| 3 | Export 실행 | Composite Image와 외부 일반 Layer가 현재 Graph 순서대로 합성됨 |
| 4 | 완료 후 확인 | Composite·외부 Layer·Source 자식의 구조와 눈 상태가 그대로 유지됨 |

Composite 아래의 Source 자식은 Composite Image에 이미 포함되어 있으므로 Export에서 다시 합성하지 않는다.

#### 시나리오 D. Layer 또는 Mask 단독 보기 중 Export

| 단계 | 사용자 조작 | 화면의 예상 반응 |
| --- | --- | --- |
| 1 | 특정 Layer 또는 Mask 단독 보기 활성화 | Viewport는 선택한 단독 보기 결과를 표시함 |
| 2 | `Export Texture` 확인 | 버튼은 계속 활성화되어 있음 |
| 3 | Export 실행 | 단독 보기 화면이 아니라 눈이 켜진 전체 Graph 결과를 출력함 |
| 4 | 완료 후 확인 | 기존 단독 보기가 계속 활성화되어 있고 Viewport도 이전 단독 보기로 유지됨 |

이 시나리오는 **단독 보기를 Export 범위로 사용할지 여부**를 사용자가 명확히 검토해야 하는 지점이다. 본 계획의 기본안은 단독 보기를 화면 확인 전용으로 보고 Export 범위에는 사용하지 않는 것이다.

#### 시나리오 E. 모든 Layer 눈이 꺼진 경우

| 단계 | 사용자 조작 | 화면의 예상 반응 |
| --- | --- | --- |
| 1 | 실제 Graph에 참여하는 모든 Layer의 눈을 끔 | Viewport에 합성 결과가 없음 |
| 2 | `Export Texture` 클릭 | 파일 탐색기를 열지 않고 오류를 표시함 |
| 3 | 오류 확인 | `내보낼 Layer가 없습니다. 하나 이상의 Layer 눈을 켜십시오.` 표시 |
| 4 | Layer Tree 확인 | 애드온이 임의로 어떤 Layer의 눈도 켜지 않음 |

버튼은 Layer 데이터가 준비된 동안 활성 상태를 유지하고, 실행 시점에 가시 Layer 0개를 구체적인 문구로 안내한다.

#### 시나리오 F. 다른 합성 작업과 충돌하는 경우

| 단계 | 사용자 조작 | 화면의 예상 반응 |
| --- | --- | --- |
| 1 | Composite 생성·재합성·해제 또는 다른 Export 실행 | 해당 작업의 진행 상태가 표시됨 |
| 2 | `Export Texture` 확인 | 충돌 작업이 끝날 때까지 버튼이 일시 비활성화됨 |
| 3 | 작업 완료 | Layer Graph가 정상 상태가 되면 버튼이 다시 활성화됨 |

이 비활성화는 Composite 선행 생성 요구가 아니라 동시에 같은 Image와 Graph를 변경하지 않기 위한 안전 제한이다.

#### 시나리오 G. Export 완료 후 최종 확인

Export가 성공하면 사용자는 다음 항목을 화면에서 확인할 수 있어야 한다.

- Layer Tree에 임시 Layer나 임시 Composite가 남지 않음
- 모든 눈 아이콘이 Export 전과 동일함
- 선택된 행과 실제 활성 Layer 기호가 동일함
- Layer·Mask 단독 보기 상태가 동일함
- Painting Mode와 현재 Brush가 유지됨
- 기존 영구 Composite 상태가 변경되지 않음
- 출력 파일을 Blender Image Editor에서 열면 눈이 켜졌던 Layer 결과만 보임
- 상태 영역에는 출력 파일 수와 폴더가 한 줄로 표시됨

#### 사용자 승인 체크리스트

- [ ] Composite를 만들지 않아도 Export 버튼이 활성화되는가?
- [ ] 눈 아이콘만으로 포함·제외를 결정하는 흐름이 자연스러운가?
- [ ] 단독 보기는 화면 확인용이고 Export는 전체 가시 Graph를 사용하는 것이 맞는가?
- [ ] 기존 Composite는 하나의 가시 Layer 결과로 취급하는 것이 맞는가?
- [ ] 모든 눈이 꺼졌을 때 버튼을 숨기거나 끄지 않고 클릭 시 오류로 안내하는 것이 맞는가?
- [ ] Export 후 Layer Tree와 Painting 상태가 완전히 유지되는가?
- [ ] 진행 Overlay의 다섯 단계가 작업 대기 상황을 이해하기에 충분한가?

## 4. 비목표

이번 변경에서 다음 기능은 수정하지 않는다.

- 영구 Composite Layer 생성·재합성·해제 UX
- Composite Source 소비와 복구 규칙
- Layer 선택·활성화·삭제 UX
- Paint Stroke, Blender Undo와 Brush 도구
- Original Layer 외부 이미지 참조
- Camera Stencil과 COLMAP Snapshot
- Modifier와 Align
- PNG·JPEG·UDIM·Atlas의 파일명과 원자 확정 규칙

## 5. 데이터 계약

### 5.1 저장 데이터

새 Scene Property, Layer Property와 Schema Version을 추가하지 않는다. Export는 일시 작업이며 `.blend`에 새 영구 상태를 기록하지 않는다.

자동 마이그레이션도 추가하지 않는다.

### 5.2 Export 입력 Snapshot

기존 Composite 중심 `ExportSceneSnapshot`을 가시 Graph 중심 Snapshot으로 변경한다.

```text
VisibleLayerExportSnapshot
├─ Scene Pointer
├─ Paint Target Pointer
├─ 관리 Material Pointer
├─ 출력 Image Backend 계약
│  ├─ SINGLE 또는 UDIM
│  ├─ Tile 번호
│  └─ Tile별 해상도
├─ 활성 Graph Layer UUID 순서
├─ Export 포함 Layer UUID 순서
├─ 전체 Layer 서명
│  ├─ Role
│  ├─ Visible
│  ├─ Image Pointer
│  ├─ Mask UUID·Enabled·Image Pointer
│  └─ Adjustment UUID·Type·Enabled·Mask 서명
├─ Composite 상태와 활성 Composite UUID
├─ Layer·Mask 단독 보기 상태
└─ 활성 Layer·Mask·Paint Image 상태
```

Snapshot은 출력 파일 확정 전 다시 계산한다. Scene, Graph 순서, 눈 상태, Image, Mask 또는 Adjustment가 바뀌면 기존 출력 파일을 건드리지 않고 중단한다.

### 5.3 출력 Image 계약

임시 Export Image는 관리 Layer의 기준 Image와 같은 Backend, Tile 번호와 Tile별 해상도를 사용한다.

- SINGLE 입력은 SINGLE 한 파일만 허용
- UDIM 입력은 UDIM 또는 Atlas 출력 허용
- 서로 다른 Backend·Tile·해상도가 Layer Graph에 섞이면 Export 전에 실패
- 임시 Image는 영구 Layer UUID나 Composite 세션에 연결하지 않음
- 출력 완료·실패·취소 뒤 임시 Image는 제거

## 6. Layer Graph 경계

### 6.1 읽기 전용 Graph 조회 함수

`layer_shader.py`에 현재 Material Base Color에 참여하는 Layer 순서를 반환하는 공개 읽기 전용 함수를 둔다.

예상 역할:

```python
def active_layer_graph_layers(scene: Any) -> tuple[Any, ...]:
    """현재 Base Color 출력에 참여하는 Layer를 UI 순서로 반환합니다."""
```

이 함수는 기존 내부 Graph 선택 규칙을 재사용하고 다음을 보장한다.

- Graph를 재연결하지 않음
- `visible`을 변경하지 않음
- Composite Source를 중복 반환하지 않음
- `STALE` 또는 모순된 Composite Metadata는 실패
- 현재 Material Graph 검증과 같은 Layer 집합 반환

Export는 반환된 Layer를 `visible == True`로 필터링한다.

### 6.2 Graph와 눈 상태의 분리

Layer가 눈이 켜져 있어도 현재 Graph에서 제외된 Composite Source 자식이면 Export 입력이 아니다. 반대로 활성 Graph에 포함됐지만 눈이 꺼진 Layer는 Export 입력에서 제외한다.

이 규칙으로 화면의 최종 Base Color와 Export 입력을 동일하게 유지하면서 Composite Source의 이중 합성을 막는다.

## 7. 임시 합성 구조

### 7.1 기존 Bake 자원 재사용

다음 검증된 기존 기능은 유지한다.

- 임시 Image Texture Bake Target 생성
- Layer 색상·Alpha·Mask Factor 연결
- Adjustment 결과 연결
- Cycles 장치 선택과 Sample 1 설정
- Render Engine, Film Transparent, Bake Margin 복구
- Object 선택·Mode와 Material Node 선택 복구
- PNG·JPEG 저장과 UDIM·Atlas 생성
- Staging 후 원자 파일 확정

### 7.2 Composite 필수 검증 제거

Export 진입 경로에서는 다음 의존성을 제거한다.

- `validate_composite_session()` 필수 호출
- 활성 Composite UUID 필수 조건
- Composite Image를 기본 Source로 직접 저장하는 분기
- `final_result_requires_bake()`를 통한 조건부 임시 합성

Export는 항상 실행 시점의 가시 Graph를 임시 이미지로 합성한다. 이로써 Composite 유무에 따른 결과 경로 차이를 없앤다.

### 7.3 영구 Composite와의 관계

영구 Composite가 활성이고 눈이 켜져 있으면 그 Composite Image는 일반 Graph 입력 하나로 사용한다. 해당 Composite의 Source 자식은 다시 Bake하지 않는다.

영구 Composite가 없어도 일반 Layer와 Original Layer만으로 같은 Export 경로를 사용한다.

## 8. Transaction과 불변성

### 8.1 Export가 변경할 수 있는 항목

- 임시 Export Image
- 임시 Bake Node
- 임시 Bake Surface 연결
- 임시 Render Engine·Cycles 장치·Bake 설정
- 임시 Object Mode·선택
- Staging 출력 파일

이 항목은 성공·실패·취소에서 모두 복구하거나 제거한다.

### 8.2 Export가 변경하면 안 되는 항목

- `layer.visible`
- Layer 순서·UUID·Role·Image Pointer
- Mask와 Adjustment 상태
- Composite 상태·세션 UUID·Source Metadata
- `ui_selected`
- 활성 Layer Index와 활성 Mask UUID
- Layer·Mask 단독 보기 상태
- Paint Material과 Paint Target
- Overlay Image와 활성 Paint Image
- 사용자 Brush·Stencil·Clone 상태
- Blender Undo Stack에 기록되는 Layer 데이터

제품 코드에서 Export 경로의 `layer.visible` 대입은 0회여야 한다.

### 8.3 사용자 동시 변경

Modal Export 도중 사용자가 Layer 눈, 순서, Mask 또는 Adjustment를 바꾸면 그 변경을 덮어써 복구하지 않는다. 대신 Snapshot 불일치로 Export를 중단하고 Staging 파일을 제거한다.

즉 애드온이 바꾼 임시 상태만 복구하고 사용자가 바꾼 영구 Layer 상태는 덮어쓰지 않는다.

### 8.4 파일 확정

```text
임시 합성 성공
→ Staging 파일 기록
→ Scene Snapshot 재검증
→ 기존 출력 Backup
→ 새 출력 전체 확정
→ Backup 정리
```

Snapshot 검증 전에는 기존 출력 파일을 이동하거나 덮어쓰지 않는다. 여러 UDIM 파일 중 일부만 새 파일로 남기는 부분 성공을 허용하지 않는다.

## 9. UI와 상태 문구

### 9.1 버튼

버튼 위치와 이름 `Export Texture`는 유지한다. Composite Layer 생성 버튼과의 선행 관계는 제거한다.

### 9.2 진행 상태

진행 Overlay를 다음 단계로 정리한다.

1. `Preparing` — 가시 Layer와 출력 계약 검증
2. `Compositing` — 가시 Layer 임시 합성
3. `Writing` — Texture 파일을 Staging 경로에 기록
4. `Committing` — Layer 상태 재검증과 출력 파일 원자 확정
5. `Complete` — 임시 자원 정리 완료

실제 단계 수와 `total_steps`를 일치시킨다.

### 9.3 오류 문구

- 가시 Layer 없음: `내보낼 Layer가 없습니다. 하나 이상의 Layer 눈을 켜십시오.`
- Graph 손상: `현재 Layer 합성 상태가 유효하지 않아 Texture를 내보낼 수 없습니다.`
- 처리 충돌: `Layer 합성 작업이 끝난 뒤 다시 시도하십시오.`
- 실행 중 변경: `내보내는 동안 Layer 상태가 변경되어 출력을 취소했습니다.`
- 복구 실패: 변경된 임시 Blender 상태의 정확한 항목을 함께 표시

## 10. 파일별 변경 계획

### `tools/projection_paint/blender/layer_shader.py`

- 현재 Base Color Graph Layer를 반환하는 읽기 전용 함수 추가
- 기존 내부 Graph 선택 규칙과 단일 구현 공유
- Composite Source 중복 제외와 `STALE` 검증 유지
- 새 함수를 공개 목록에 추가

### `tools/projection_paint/blender/texture_export.py`

- 모듈 설명을 활성 Composite Export에서 가시 Layer Export로 변경
- Composite 중심 Snapshot을 가시 Graph Snapshot으로 교체
- Operator Poll에서 활성 Composite 필수 조건 제거
- 파일 탐색기 진입 전 가시 Layer 0개 검증
- 단독 보기 차단 제거
- 조건부 Direct Composite 저장 분기 제거
- 모든 Export를 임시 가시 Layer 합성 경로로 통일
- 진행 단계와 오류 문구 갱신
- Export 전후 Layer 가시성 불변 검증 추가

### `tools/projection_paint/blender/composite_layer.py`

- `create_final_texture_export_image()`가 영구 Composite 세션이 아니라 명시된 가시 Graph Layer를 Bake하도록 변경
- 기존 함수 이름과 호출 형태를 유지할 수 있으면 동작 의미만 확장해 불필요한 호환 Wrapper를 만들지 않음
- 임시 Image에 영구 Composite 세션 Metadata를 기록하지 않음
- 성공·실패·취소의 Node·Image·Render·Mode 복구 계약 유지

### `tools/projection_paint/blender/panels.py`

- 버튼 배치는 유지
- 별도의 Composite 선행 안내가 있다면 제거
- Poll 결과만 새 Operator 계약을 따르게 함

### 테스트

- `tests/projection_paint/blender_background/test_texture_export.py`
- `tests/projection_paint/blender_background/test_single_image_composite_export.py`
- 필요 시 설치본 또는 실제 GUI Export 전용 회귀 추가

### 문서

- `docs/TESTING.md`
- `docs/migration/IMPLEMENTATION_STATUS.md`
- 구현 버전 Release Note

## 11. 테스트 계획

### 11.1 버튼 활성 조건

1. Composite 상태 `NONE`, 일반 Layer 존재 → 활성
2. Composite 상태 `ACTIVE`, 유효 Graph → 활성
3. Export 실행 중 → 비활성
4. Composite 생성·재합성·해제 중 → 비활성
5. Graph `STALE` → 비활성
6. Paint Target 또는 관리 Material 없음 → 비활성

### 11.2 Composite 없는 Export

1. Original Layer만 눈 켬 → 원본 Pixel 출력
2. 일반 Layer와 Original 눈 켬 → Alpha 순서 합성 Pixel 출력
3. 일부 Layer 눈 끔 → 해당 Layer Pixel 제외
4. 모든 눈 끔 → 오류, 출력 0개
5. Layer Mask 적용 → Mask 영역별 기대 Pixel 출력
6. Adjustment와 Adjustment Mask 적용 → 기대 색상 출력

파일 존재 여부만 보지 않고 고정 UV 위치의 실제 Pixel 값을 비교한다.

### 11.3 Composite가 있는 Export

1. 활성 Composite만 눈 켬 → Composite Pixel 출력
2. Composite와 외부 일반 Layer 눈 켬 → Graph 순서대로 합성
3. Composite Source 자식이 Metadata상 존재 → 이중 합성되지 않음
4. Composite 눈 끔, 외부 일반 Layer 눈 켬 → 외부 Layer만 출력
5. Composite와 모든 외부 Layer 눈 끔 → 명확한 가시 Layer 없음 오류

### 11.4 단독 보기

1. Layer 단독 보기 활성 상태에서도 버튼 활성
2. Mask 단독 보기 활성 상태에서도 버튼 활성
3. Export 결과는 단독 보기 화면이 아니라 눈이 켜진 Graph 결과
4. Export 뒤 단독 보기 UUID와 Material Preview 태그 불변

### 11.5 상태 불변성

성공·실패·취소 각각에서 다음 Tuple이 Export 전후 동일해야 한다.

- Layer Pointer·UUID·순서·Role
- Layer `visible`
- Layer와 Mask Image Pointer·Pixel Hash·Dirty 상태
- Mask·Adjustment 값
- Composite 상태와 세션 Metadata
- 선택·활성 Layer·Mask·단독 보기
- Material Node Pointer와 연결
- Object Mode·선택·활성 Material Slot
- Render Engine·Cycles 장치·Film Transparent·Bake 설정
- Paint Runtime과 Stencil 상태

### 11.6 동시 변경과 파일 원자성

1. Bake 뒤 Layer 눈 변경 → Snapshot 불일치, 기존 출력 유지
2. Staging 뒤 Layer 순서 변경 → Snapshot 불일치, 기존 출력 유지
3. 두 번째 UDIM 확정 실패 → 모든 기존 타일 복구
4. 임시 Image 제거 실패 → 출력 성공과 정리 실패를 분리 보고
5. `Esc` 취소 → 출력·Layer·임시 자원 없음

### 11.7 실제 GUI 검증

실제 설치본에서 다음 순서로 확인한다.

1. Composite를 만들지 않은 새 Painting 데이터 준비
2. Original과 일반 Layer 눈을 켠 상태에서 `Export Texture` 활성 확인
3. Export 전 Layer Tree Screenshot과 눈 상태 기록
4. PNG Export 실행과 진행 Overlay 확인
5. 출력 이미지를 Blender에서 열어 실제 Pixel 확인
6. Export 후 Layer Tree Screenshot과 눈 상태가 동일한지 확인
7. 일부 Layer 눈을 끈 뒤 재Export하여 결과 제외 확인
8. 기존 Composite가 있는 프로젝트에서도 동일 회귀

## 12. 구현 순서

### 단계 1. 실패 회귀 고정

- Composite가 없을 때 버튼이 비활성인 현재 동작을 실패 테스트로 고정
- Composite 없는 일반 Layer Stack Export 실패를 재현
- Export 전후 가시성 Tuple을 기록

완료 조건: 기존 Composite 의존성이 자동 테스트에서 명확히 실패한다.

### 단계 2. 읽기 전용 Graph Source 도입

- Layer Shader의 활성 Graph 선택 규칙을 공개 읽기 전용 함수로 분리
- Material Graph 검증과 Export가 같은 Layer 집합을 사용하도록 통합
- Composite Source 이중 합성 방지 테스트 추가

완료 조건: Composite 유무에 관계없이 실제 Base Color Graph 순서를 한 함수에서 얻는다.

### 단계 3. Export Poll과 Snapshot 변경

- Composite 필수 Poll 제거
- 가시 Graph Layer 0개 검증 추가
- 단독 보기 차단 제거
- 가시 Graph 중심 Snapshot과 실행 중 변경 검증 구현

완료 조건: Composite가 없어도 버튼이 활성화되고 유효한 Export Snapshot이 생성된다.

### 단계 4. 임시 합성 경로 통일

- 모든 Export를 가시 Graph 임시 RGBA Image Bake로 통일
- SINGLE·UDIM Backend와 Tile 해상도 유지
- 기존 저장·Atlas·원자 확정 코드를 재사용

완료 조건: 눈이 켜진 Layer의 실제 Pixel 합성 결과가 파일에 기록된다.

### 단계 5. 불변성과 실패 복구

- 성공·실패·취소에서 임시 Image·Node·Render·Mode 정리
- Layer 가시성 직접 쓰기 0회 확인
- Snapshot 불일치와 파일 확정 실패 Rollback 검증

완료 조건: 모든 경로에서 Layer Tree와 눈 상태가 Export 전후 동일하다.

### 단계 6. 전체 회귀와 설치

- Texture Export와 Composite Background 회귀 실행
- Projection Paint 전체 회귀 실행
- Production 검사와 격리 설치본 검사 실행
- 새 패키지를 실제 Blender `user_default`에 설치
- 실데이터 GUI에서 버튼·Pixel·눈 상태 최종 확인

완료 조건: 설치본에서 Composite 선행 생성 없이 Export되고 Layer 상태가 바뀌지 않는다.

## 13. 위험과 대응

| 위험 | 대응 |
| --- | --- |
| 활성 Composite Source가 다시 합성됨 | 실제 Material Graph Layer 함수로 Source 자식을 제외하고 Composite Image만 입력 |
| 단독 보기 결과가 Export에 섞임 | 단독 보기 Shader 우회가 아닌 눈이 켜진 실제 Graph Layer를 명시적으로 Bake |
| 8K·UDIM Export가 느려짐 | 기존 Cycles 장치 선택과 Progress Overlay 유지, 결과 정확성을 우선하고 이후 동일 결과 Direct Copy 최적화는 별도 검증 |
| Export 중 Layer 변경 결과가 섞임 | 시작·확정 전 Snapshot 비교, 불일치 시 Staging 폐기 |
| Bake가 Layer 눈을 변경함 | 가시성 쓰기 금지와 전후 Tuple 회귀, 임시 Shader 연결만 사용 |
| 사용자 변경을 Rollback이 덮어씀 | 애드온 임시 상태만 복구하고 Snapshot 불일치는 사용자 상태를 유지한 채 Export 중단 |
| 원본 외부 Texture 경로 유실 | Original Image Pointer와 filepath를 읽기 전용으로 유지하고 Export 전후 비교 |
| Alpha 경계가 검게 변함 | 투명 Base·Straight Alpha·Color/Alpha 결합 Pixel 회귀 유지 |
| 일부 UDIM만 교체됨 | 기존 Staging·Backup·전체 묶음 원자 확정 유지 |
| 기존 Composite 편집 기능이 변함 | Export 진입점만 분리하고 영구 Composite 생성·해제 코드는 비목표로 고정 |

## 14. 완료 기준

1. Composite Layer가 없어도 `Export Texture`가 활성화된다.
2. Export는 활성 Material Graph에서 눈이 켜진 Layer만 사용한다.
3. Original Layer, 일반 Layer와 활성 Composite를 같은 Graph 규칙으로 처리한다.
4. Composite Source 자식을 이중 합성하지 않는다.
5. Mask, Adjustment, Adjustment Mask와 Alpha 결과가 파일 Pixel에 반영된다.
6. Layer·Mask 단독 보기는 Export 활성 여부와 결과 Source를 바꾸지 않는다.
7. 눈이 켜진 Layer가 없으면 파일을 만들지 않고 정확한 오류를 표시한다.
8. 성공·실패·취소 뒤 모든 Layer 눈 상태가 이전과 동일하다.
9. Export 경로에서 `layer.visible` 직접 대입이 없다.
10. Layer UUID·순서·Role·Image·Pixel과 Composite Metadata가 변경되지 않는다.
11. Blender Mode·선택·Material Node·Render·Cycles 설정이 복구된다.
12. Export 도중 사용자 변경을 감지하면 기존 출력 파일을 유지하고 중단한다.
13. SINGLE·UDIM·Atlas·PNG·JPEG 출력 계약과 원자 확정이 유지된다.
14. 새 저장 Property와 자동 마이그레이션 코드를 추가하지 않는다.
15. Modifier, Align, Camera Stencil과 Blender Brush 도구에 변경이 없다.
16. Background, 실제 GUI, Production 빌드, 격리 설치본과 실제 설치본 검증이 통과한다.

## 15. 최종 구조

```text
Projection Paint Layer Tree
        │
        ▼
현재 Material Base Color Graph 계산
        │
        ▼
눈이 켜진 Graph Layer UUID Snapshot
        │
        ▼
임시 RGBA Export Image에 합성
        │
        ├─ Mask
        ├─ Adjustment
        ├─ Alpha
        └─ Composite Image를 일반 입력으로 처리
        │
        ▼
PNG / JPEG / UDIM / Atlas Staging
        │
        ▼
Layer Snapshot 재검증
        │
        ▼
출력 파일 원자 확정
        │
        ▼
임시 자원 제거

영구 Layer Tree·눈 상태·Composite 상태: 변경 없음
```
