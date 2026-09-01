# COLMAP Projection Paint Texture Export Native Bake·진행률 UI 구조변경 계획서

## 1. 문서 목적

현재 `Export Texture`의 Color·Alpha Bake는 Blender 메인 스레드에서 동기식 `EXEC_DEFAULT`로 실행된다. 따라서 Bake가 끝날 때까지 Texture Export의 Modal Timer와 진행률 Overlay가 갱신되지 않으며, 사용자는 `Preparing 0.0%` 화면에서 Blender가 멈춘 것처럼 보게 된다.

이 계획은 Texture Export의 Bake를 Composite Layer 생성과 같은 Blender Native Bake Job 상태 머신으로 전환하고, 진행률 표시도 Composite 생성과 동일한 공통 UI 규칙으로 통일한다.

다음 기존 계약은 그대로 유지한다.

- Export 실행 중 `Esc`를 제외한 사용자 입력 차단
- 눈이 켜진 실제 Layer Graph를 Export Source로 사용
- Export 전후 Layer 눈 상태·선택·활성 Layer·Paint 상태 불변
- 임시 폴더 Staging, 기존 파일 Backup, 전체 결과 원자 확정
- 실패·취소 시 기존 출력 복구와 임시 자원 제거
- Pixel Hash와 저장 직전 전체 Snapshot 재검사 미사용

이 문서는 구현 범위와 검증 기준을 고정하는 계획서이며, 이 단계에서는 제품 코드를 수정하지 않는다.

---

## 2. 현재 구조와 확인된 원인

### 2.1 현재 Texture Export 실행 흐름

현재 `texture_export.py`의 GUI Operator는 `BLOCKING` Modal로 실행되지만, 첫 번째 유효한 Timer 처리에서 다음 함수를 호출한다.

```text
texture_export_steps()
→ create_final_texture_export_image()
→ Color Bake: bpy.ops.object.bake("EXEC_DEFAULT", ...)
→ Alpha Bake: bpy.ops.object.bake("EXEC_DEFAULT", ...)
→ Color·Alpha 결합
→ 첫 번째 yield
```

Color Bake와 Alpha Bake가 모두 끝난 뒤에야 Generator가 처음 제어권을 돌려준다. 그동안 Blender 이벤트 루프가 Texture Export Operator로 복귀하지 못하므로 다음 동작이 중단된다.

- 진행률 문구와 애니메이션 갱신
- Overlay 다시 그리기
- 경과 시간 표시
- `Esc` 취소 처리
- Blender 창의 정상적인 응답

즉 실제로 멈춘 것이 아니라, 장시간 Bake가 메인 이벤트 루프를 점유하여 화면과 입력이 갱신되지 않는 구조다.

### 2.2 실제 8K Export 기준 병목

현재 설치본과 실데이터에서 측정된 대표 결과는 다음과 같다.

| 단계 | 측정 시간 |
| --- | ---: |
| Graph 준비 | 약 0.0003초 |
| Color Bake | 약 14.61초 |
| Alpha Bake | 약 14.14초 |
| Color·Alpha 결합 | 약 0.46초 |
| 최종 Export Image 생성 전체 | 약 32.98초 |
| PNG 저장 | 약 3.86초 |
| 파일 원자 확정 | 약 0.0003초 |
| 정리 | 약 0.02초 |
| 전체 | 약 36.87초 |

전체 시간의 대부분인 약 29초가 두 번의 동기식 Bake에 사용된다. 이번 변경의 일차 목표는 같은 계산을 더 빠른 엔진으로 바꾸는 것이 아니라, 이 시간 동안 Blender와 진행률 UI가 계속 응답하도록 만드는 것이다.

### 2.3 Composite 생성과의 차이

Composite Layer 생성은 다음 구조를 이미 사용한다.

```text
Bake 준비
→ bpy.ops.object.bake("INVOKE_DEFAULT", ...)
→ Blender OBJECT_BAKE Job 실행
→ Modal Timer가 Job 상태와 Handler 결과 감시
→ Bake 종료 확인
→ 상태 복원
→ 다음 Bake 또는 후처리 진행
```

`INVOKE_DEFAULT`는 Bake를 Blender Native Job으로 시작하고 즉시 Operator의 Modal 흐름으로 제어권을 돌려준다. 따라서 Bake 중에도 Timer와 Overlay를 갱신할 수 있다.

### 2.4 진행률 UI가 다르게 보이는 이유

Composite 생성과 Texture Export는 이미 같은 `progress_overlay.py` 렌더러를 사용한다. 별도의 Texture Export 투명도 상수가 원인은 아니다.

체감 차이는 표시 상태의 차이에서 발생한다.

- Composite 생성: Native Bake 활동형 화면, `Process.` 애니메이션, 장치·해상도·경과 시간 표시
- Texture Export: 오래된 `Preparing 0.0%` 확정형 화면이 Bake가 끝날 때까지 고정

따라서 Texture Export 전용 알파 값을 추가하지 않는다. 공통 렌더러를 유지하고, Texture Export의 Bake 구간을 Composite와 같은 활동형 View Model로 바꾼다.

---

## 3. 목표

### 3.1 기능 목표

1. GUI Texture Export의 Color·Alpha Bake를 `INVOKE_DEFAULT` Native Bake Job으로 실행한다.
2. Color → Alpha → 결합 → 파일 기록 → 원자 확정을 하나의 명시적 상태 머신으로 관리한다.
3. Bake 중 Overlay 애니메이션, 단계, 경과 시간이 계속 갱신되게 한다.
4. Bake 중 `Esc` 취소를 Blender Native Bake에 전달하고, Job 종료 뒤 안전하게 정리한다.
5. Export 중 일반 사용자 입력은 계속 차단한다.
6. Composite 생성과 Texture Export가 동일한 Native Bake 준비·복원 기반 코드를 사용하게 한다.
7. 기존 Export 이미지 결과, 품질, 파일 구성과 원자적 저장 계약을 유지한다.

### 3.2 비목표

이번 변경에서 다음 항목은 다루지 않는다.

- Bake Engine, `EMIT` 유형, Sample 수, Margin, Color·Alpha 계산 변경
- Export 해상도 또는 파일 형식 변경
- PNG·JPEG 인코딩을 별도 Thread나 Process로 이동
- Layer Graph, Composite Layer 데이터 구조 또는 Paint Target 변경
- Export 중 Layer 편집 허용
- Pixel Hash 또는 저장 직전 전체 Snapshot 재검사 복구
- 공통 진행률 Overlay의 전역 색상·투명도 변경
- 기존 프로젝트 데이터 Migration 또는 Schema 변경

파일 인코딩 단계는 약 3.86초 동안 동기식으로 남을 수 있다. 이번 작업은 가장 큰 정지 원인인 Color·Alpha Bake를 우선 Native Job으로 전환하며, 파일 인코딩의 별도 비동기화는 실제 회귀 결과를 본 뒤 별도 작업으로 판단한다.

---

## 4. 목표 사용자 경험

### 4.1 정상 Export

| 단계 | 사용자 화면 | 내부 동작 |
| --- | --- | --- |
| 1 | `Export Texture` 클릭 후 경로 선택 | 입력 Layer Graph와 출력 계약 검증 |
| 2 | `Final Texture Export` 진행창 표시 | 임시 Color Image와 Bake 상태 준비 |
| 3 | `Color Bake · Process...` 표시 | Color Native Bake Job 실행 |
| 4 | 같은 진행창에서 문구와 경과 시간 갱신 | Modal Timer가 `OBJECT_BAKE` 실행 상태 감시 |
| 5 | `Alpha Bake · Process...` 표시 | Color 상태 복원 후 Alpha Native Bake Job 실행 |
| 6 | `Combining` 표시 | Color와 Alpha Image 결합 |
| 7 | `Writing` 표시 | 임시 출력 폴더에 파일 기록 |
| 8 | `Committing` 표시 | 기존 출력 Backup 후 전체 결과 원자 확정 |
| 9 | 완료 표시 후 진행창 종료 | 임시 Image·Node·Timer·예약 정리 |

Bake 구간에는 실제 완료율을 알 수 없으므로 가짜 백분율을 표시하지 않는다. 대신 Composite 생성과 같은 활동형 애니메이션과 현재 단계·경과 시간을 표시한다.

### 4.2 실행 중 입력

- 마우스 클릭, Layer 조작, Brush 변경, 단축키 등 일반 입력은 Export가 끝날 때까지 통과시키지 않는다.
- `Esc`만 취소 요청으로 허용한다.
- Native Bake 실행 중 `Esc`는 Blender Bake Job에도 전달하여 실제 계산을 중단시킨다.
- `Esc`를 누른 즉시 Image나 임시 Node를 삭제하지 않는다. `object_bake_cancel` 결과와 `OBJECT_BAKE` 종료를 확인한 뒤 복원·정리한다.

### 4.3 취소

```text
Esc 입력
→ Texture Export Job에 취소 요청 기록
→ 현재 Native Bake에 Esc 전달
→ object_bake_cancel 또는 Job 종료 확인
→ Bake 준비 상태 복원
→ 임시 출력과 임시 Image 제거
→ 기존 출력 파일 유지 또는 Backup 복구
→ 진행창 종료
```

취소 시 부분 PNG, 임시 디렉터리, 임시 Bake Node 또는 임시 Image가 남아서는 안 된다.

### 4.4 실패

오류는 실패한 단계와 원인을 함께 표시한다.

예시:

```text
Texture Export 실패: Alpha Bake가 완료되지 않았습니다.
Texture Export 실패: 출력 이미지를 기록하지 못했습니다. 원인: <운영체제 오류>
Texture Export 실패: 기존 출력 복구 중 오류가 발생했습니다. 원인: <운영체제 오류>
```

`OBJECT_BAKE`가 아직 실행 중이면 오류가 발생해도 Blender 상태를 먼저 건드리지 않고, Job 종료 뒤에 복원·정리한다.

---

## 5. 상태 모델

### 5.1 TextureExportJob 도입

GUI Export 전용 `TextureExportJob`을 두고 전체 수명을 한 객체에서 관리한다.

필수 상태는 다음과 같다.

| 구분 | 보관 정보 |
| --- | --- |
| 실행 식별 | Scene, Window, View Layer, Paint Target 포인터 |
| 입력 고정 | 시작 시점의 가시 Layer UUID와 출력 옵션 |
| Bake 자원 | Color Image, Alpha Image, Bake 준비 상태 |
| Native Job | 현재 Stage, 실행 결과, 시작·종료 여부, 취소 요청 |
| 파일 Transaction | 임시 폴더, Staged 경로, Backup 경로, 확정 경로 |
| UI | 현재 Phase, 상세 문구, 시작 시각, 오류 |
| 정리 | Timer·진행창·예약 해제 여부, 복원 완료 여부 |

`CompositeCreateJob`을 Texture Export에 직접 재사용하지 않는다. Composite Job에는 영구 Composite Layer 생성·Source 잠금·메타데이터 확정 계약이 포함되어 있어 Export의 임시 결과와 수명이 다르다.

### 5.2 상태 전이

```text
IDLE
  ↓
PREPARING
  ↓
COLOR_BAKE_PREPARED
  ↓
COLOR_BAKING
  ↓
COLOR_BAKE_FINISHED
  ↓
ALPHA_BAKE_PREPARED
  ↓
ALPHA_BAKING
  ↓
ALPHA_BAKE_FINISHED
  ↓
COMBINING
  ↓
WRITING
  ↓
STAGING
  ↓
COMMITTING
  ↓
SUCCEEDED
```

모든 실행 단계에서는 다음 종료 경로를 가질 수 있다.

```text
현재 단계
  ├─ 사용자 취소 → CANCELLING → ROLLING_BACK → CANCELLED
  └─ 오류       → FAILED_PENDING_CLEANUP → ROLLING_BACK → FAILED
```

### 5.3 단계 전환 원칙

- 한 번의 Modal Timer 처리에서 장시간 동기 작업을 연속 실행하지 않는다.
- Native Bake 시작 직후 Modal 제어권을 Blender에 반환한다.
- `OBJECT_BAKE` 실행 중에는 진행 상태 갱신만 수행한다.
- Handler 결과와 Blender Job 종료를 모두 확인해야 다음 단계로 이동한다.
- 파일 기록과 Commit은 서로 다른 단계로 구분해 오류 원인을 정확히 남긴다.
- 성공·실패·취소 어느 경로에서도 정리는 정확히 한 번만 실행한다.

---

## 6. Native Bake 공통화 설계

### 6.1 재사용할 공통 책임

현재 Composite 생성 경로에서 이미 검증된 다음 책임을 Texture Export와 공유한다.

- Source Graph를 임시 Emission 출력으로 연결
- 임시 Bake Image Node 활성화
- Render Engine·Cycles Device·Sample·Margin 준비
- 선택 Object, 활성 Object, View Layer 상태 저장
- `INVOKE_DEFAULT` Native Bake 호출
- Bake 완료 뒤 Material·Scene·Selection·UI 상태 복원
- 실패·취소 시 같은 복원 경로 사용

### 6.2 공유하지 않을 책임

다음 책임은 각 기능에 남긴다.

| Composite 생성 | Texture Export |
| --- | --- |
| 영구 Composite Image와 Layer 생성 | 임시 Color·Alpha Image 생성 |
| Source Layer 잠금·구조 확정 | Layer Tree 불변 |
| Composite Metadata 기록 | 출력 파일 Transaction |
| Composite 재합성 계약 | Color·Alpha 결합과 파일 저장 |

### 6.3 구현 방향

`composite_layer.py`에 묶여 있는 Native Bake 준비·호출·복원 중 Job과 무관한 부분만 공통 Helper로 분리하거나 공개 Wrapper로 고정한다.

예상 책임 단위는 다음과 같다.

```text
prepare_emit_bake(...)
invoke_prepared_native_emit_bake(...)
restore_bake_persistent_state(...)
restore_bake_ui_state(...)
```

기존 Composite 공개 동작과 호출 순서는 유지하고, Texture Export가 같은 하위 Helper를 사용한다. 복원 코드를 Texture Export에 복사해 두 개의 구현으로 만들지 않는다.

Background Test와 동기식 내부 API는 기존 `EXEC_DEFAULT` 경로를 유지할 수 있다. Native Job 전환 대상은 Blender GUI의 `Export Texture` Operator다.

---

## 7. Blender Bake Handler 연결

### 7.1 결과 전달

현재 `object_bake_pre`, `object_bake_complete`, `object_bake_cancel` Handler는 Composite Job에만 결과를 전달한다. 이를 다음 두 소비자에게 전달하도록 확장한다.

```text
Blender object_bake_* Handler
  ├─ Composite Job 결과 기록
  └─ Texture Export Job 결과 기록
```

Texture Export 쪽은 Scene과 Paint Target 포인터, 현재 Native Bake Stage를 비교하여 자신이 시작한 Bake 결과만 수신한다. 외부 Bake나 다른 Object의 Bake는 무시한다.

### 7.2 Handler 안전 규칙

Handler에서는 다음 작업만 한다.

- `RUNNING`, `COMPLETE`, `CANCELLED` 결과 기록
- 해당 Job의 종료 신호 갱신

Handler 안에서는 Image 제거, Material Node 변경, 파일 기록, UI 종료를 실행하지 않는다. 실제 상태 변경은 Modal Timer에서 Blender Job이 멈춘 것을 확인한 뒤 수행한다.

---

## 8. 입력 차단·취소·Lifecycle 계약

### 8.1 입력 차단 유지

Texture Export Operator의 `BLOCKING` 계약은 유지한다.

- `TIMER`: 상태 머신 진행
- `ESC`: 취소 요청, Native Bake 실행 중일 때만 Blender Bake까지 전달
- 그 밖의 이벤트: 소비하여 Layer·Brush·Workspace가 변경되지 않게 함

Composite Operator의 일반 `PASS_THROUGH` 입력 정책은 복사하지 않는다. Composite와 같은 것은 Native Bake 실행 구조와 진행률 표시이며, Texture Export의 입력 차단 정책은 기존 계약을 유지한다.

### 8.2 Lifecycle 중단

Load, Undo, Redo, Extension 해제 또는 Workspace 정리 요청이 Export 중 발생하면 다음 규칙을 적용한다.

1. 현재 Export Job에 `abort_after_bake`를 기록한다.
2. Native Bake가 실행 중이면 즉시 Node·Image·Handler를 제거하지 않는다.
3. `OBJECT_BAKE` 종료를 기다린다.
4. Bake 준비 상태를 복원한다.
5. 파일 Transaction을 Rollback한다.
6. Timer·Overlay·실행 예약을 해제한다.
7. 그 뒤 Extension 해제 또는 Lifecycle 처리를 계속한다.

`texture_export_running()`은 Bake가 끝나는 순간이 아니라 모든 복원과 정리가 완료될 때까지 `True`를 유지한다.

---

## 9. 파일 Transaction 유지

Native Bake 전환은 Export 파일 확정 구조를 바꾸지 않는다.

```text
최종 이미지 준비
→ 임시 출력 폴더에 기록
→ 모든 예상 파일 존재 확인
→ 기존 출력 Backup
→ Staged 파일 전체 확정
→ 성공 시 Backup 제거
```

실패 또는 취소 시 다음을 보장한다.

- 확정 전 실패: 임시 출력만 제거
- Backup 뒤 실패: 새 파일 제거 후 기존 출력 복구
- 성공: `OBJ/MTL/Image` 또는 선택한 Texture 출력 계약 유지
- 덮어쓰기 허용 여부는 기존 단일 권한 판정을 그대로 사용

사용자 입력이 차단되므로 Export 실행 중 Layer가 바뀌지 않는다는 현재 전제를 유지한다. Pixel Hash와 저장 직전 전체 무결성 재검사는 다시 추가하지 않는다.

---

## 10. 진행률 UI 통일

### 10.1 공통 렌더러

`progress_overlay.py`를 모든 진행률 UI의 단일 스타일 권위로 유지한다.

- 외곽·내부 패널 색상과 알파
- 글꼴 크기와 간격
- 제목·상태·상세 정보 배치
- 진행 막대와 활동형 애니메이션 표현

Texture Export 전용 투명도 값을 추가하지 않는다. 이 방식으로 Composite, COLMAP 데이터 업데이트, Texture Export가 같은 패널 스타일을 유지한다.

### 10.2 Bake 구간 표시

Bake 구간은 Composite와 동일한 활동형 표시를 사용한다.

```text
Final Texture Export
Color Bake
Process...
Device: <현재 Bake 장치>
Image: 8192 × 8192
Bake: EMIT · Samples 1
경과 시간: 00:12
```

Alpha 단계에서는 `Color Bake`만 `Alpha Bake`로 바뀐다. Blender가 실제 Bake 진행률을 제공하지 않으므로 `0%`, `50%` 같은 추정값은 표시하지 않는다.

### 10.3 후처리 구간 표시

Native Bake가 끝난 뒤에는 확정형 단계를 표시한다.

- `Combining`: Color·Alpha 결합
- `Writing`: 임시 파일 기록
- `Committing`: 기존 출력 Backup과 원자 확정
- `Complete`: 출력 완료

기존 기본 문구인 `최종 결과를 검증하는 중입니다`는 실제 동작과 맞지 않으므로 제거한다.

### 10.4 공통화 범위

Composite 전용 이름을 Texture Export에서 그대로 참조하지 않는다. 활동형 점 애니메이션과 Native Bake 상세 View 생성처럼 기능 중립적인 부분만 `progress_overlay.py` 또는 별도의 공통 Helper로 이동한다.

Composite 생성의 화면과 동작은 회귀 없이 유지한다.

---

## 11. 파일별 변경 계획

### 11.1 제품 코드

| 파일 | 변경 내용 |
| --- | --- |
| `tools/projection_paint/blender/texture_export.py` | `TextureExportJob`과 Native Bake 상태 머신 도입, GUI Export의 Color·Alpha Bake를 `INVOKE_DEFAULT`로 전환, 안전한 취소·정리 구현 |
| `tools/projection_paint/blender/composite_layer.py` | Job과 무관한 Bake 준비·호출·복원 Helper의 재사용 경계 정리, 기존 Composite API 유지 |
| `tools/projection_paint/blender/state_handlers.py` | Native Bake 결과를 Composite와 Texture Export Job에 안전하게 전달 |
| `tools/projection_paint/blender/composite_progress.py` | Composite 전용 상태와 공통 Native Bake 표시 책임 분리 |
| `tools/projection_paint/blender/progress_overlay.py` | 기능 중립적인 활동형 Native Bake View Helper를 둘 경우 단일 UI 권위로 확장 |
| `tools/projection_paint/blender/lifecycle.py` | Export Native Bake 실행 중 해제 지연과 정리 완료 대기 검증 |

실제 구현 시 현재 모듈 위치를 다시 확인하고, 같은 책임이 이미 존재하면 새 파일이나 중복 Helper를 만들지 않는다.

### 11.2 테스트

| 범위 | 변경 내용 |
| --- | --- |
| Texture Export Background Test | 상태 전이, 원자 확정, Rollback, Hash 미사용 계약 검증 |
| Native Bake Handler Test | Scene·Target 일치, 외부 Bake 무시, 완료·취소 신호 검증 |
| Composite 회귀 Test | 기존 Composite Native Bake와 진행률 UI 불변 확인 |
| Lifecycle Test | Bake 실행 중 해제·Load·Undo·Redo 요청이 안전한 경계까지 대기하는지 확인 |
| 실제 Blender GUI Test | 8K Export 중 UI 응답, 진행 문구, 입력 차단, `Esc` 취소, 최종 이미지 일치 검증 |

### 11.3 문서와 배포

구현 완료 시 다음 문서를 함께 갱신한다.

- `docs/TESTING.md`
- `docs/migration/IMPLEMENTATION_STATUS.md`
- 새 Release Note
- Extension 버전과 Manifest
- 설치 ZIP

계획서 작성 단계에서는 버전, Manifest, 패키지를 변경하지 않는다.

---

## 12. 구현 순서

### 1단계. 현재 계약 고정

- 현 설치본의 8K Export 결과와 단계별 시간을 기준값으로 저장한다.
- Export 전후 Layer·눈·선택·활성 Layer·Paint Target 상태를 기록한다.
- 성공 출력 파일의 크기와 픽셀 결과를 기준값으로 보존한다.

### 2단계. 공통 Native Bake 경계 정리

- Composite의 Bake 준비·호출·복원 코드를 Job 중립 단위로 분리한다.
- Composite의 기존 Native Bake 회귀를 먼저 통과시킨다.
- Background 동기식 Bake 경로가 유지되는지 확인한다.

### 3단계. TextureExportJob 도입

- Color Native Bake 한 단계만 먼저 연결한다.
- Handler 결과와 `OBJECT_BAKE` 종료의 이중 확인을 구현한다.
- Color Bake 성공·취소·오류 정리를 검증한다.

### 4단계. Alpha·결합·파일 단계 연결

- Color 완료 뒤 Alpha Native Bake를 시작한다.
- Alpha 완료 뒤 기존 결합 로직을 실행한다.
- 기존 Staging·Backup·Commit·Rollback을 상태 머신에 연결한다.

### 5단계. 진행률 UI 통일

- Bake 구간을 Composite와 같은 활동형 View로 표시한다.
- Color·Alpha 단계와 경과 시간 갱신을 확인한다.
- 후처리 단계 문구를 실제 작업과 맞춘다.
- 별도의 Export 투명도 상수가 생기지 않았는지 확인한다.

### 6단계. 취소·Lifecycle 회귀

- Color Bake와 Alpha Bake 각각에서 `Esc`를 검증한다.
- Native Bake 중 Extension 해제·파일 Load 경계에서 조기 정리가 없는지 확인한다.
- 임시 Image, Node, Timer, 출력 폴더, Backup 잔여물을 검사한다.

### 7단계. 실제 Blender 최종 회귀

- 실데이터 8K Export를 실행한다.
- Native Bake 중 창 응답과 Overlay 갱신을 눈으로 확인한다.
- Export 중 Layer 클릭과 단축키가 상태를 바꾸지 못하는지 확인한다.
- 기존 0.3.30 출력과 픽셀 결과가 같은지 확인한다.
- 성공·취소 후 원본 `.blend`를 저장하지 않고 종료한다.

### 8단계. 패키징·설치

- 자동 테스트 전체 통과
- Extension ZIP 빌드
- 현재 Blender 설치본 교체 설치
- 설치본으로 동일 GUI 회귀 실행
- Release Note와 구현 현황 갱신

---

## 13. 검증 계획

### 13.1 정적·Background 검증

다음을 자동 검사한다.

1. GUI Texture Export가 `INVOKE_DEFAULT`를 사용한다.
2. Background 또는 동기 API는 필요 시 `EXEC_DEFAULT`를 유지한다.
3. 상태 전이가 `Color → Alpha → Combining → Writing → Committing` 순서다.
4. Handler가 일치하지 않는 외부 Bake를 무시한다.
5. 일반 입력은 차단되고 `Esc`만 취소 경로로 전달된다.
6. `OBJECT_BAKE` 실행 중 Image·Node·Handler가 제거되지 않는다.
7. 성공·오류·취소에서 정리 함수가 정확히 한 번 실행된다.
8. 기존 원자적 덮어쓰기와 Rollback 테스트가 유지된다.
9. Pixel Hash와 저장 직전 전체 Snapshot 검사가 다시 생기지 않는다.
10. Texture Export 전용 Overlay 알파 상수가 생기지 않는다.

### 13.2 실제 GUI 검증

실제 Blender와 8K 실데이터에서 다음을 확인한다.

| 검증 | 합격 기준 |
| --- | --- |
| 진행창 표시 | 첫 Bake 전에 `Color Bake` 활동형 화면이 나타남 |
| 화면 갱신 | Bake 중 `Process.` 애니메이션과 경과 시간이 주기적으로 바뀜 |
| 창 응답 | 운영체제에서 Blender가 응답 없음 상태로 장시간 고정되지 않음 |
| 단계 전환 | Color 완료 뒤 Alpha, 결합, 기록, 확정 순서가 보임 |
| 입력 차단 | Layer 클릭·Brush 단축키·Workspace 전환이 Export 상태를 바꾸지 못함 |
| 취소 | Color·Alpha 각각에서 `Esc` 후 안전하게 종료됨 |
| 상태 보존 | 눈·선택·활성 Layer·Paint Target·Brush가 실행 전과 동일함 |
| 파일 안전 | 취소·실패 시 기존 출력 보존, 성공 시 전체 파일만 교체됨 |
| 결과 동일 | 기존 0.3.30 성공 출력과 픽셀 결과가 동일함 |
| 잔여물 없음 | 임시 Image·Node·Timer·폴더·Backup이 남지 않음 |

### 13.3 성능 판정

이번 변경의 합격 기준은 계산 시간 단축이 아니라 응답성이다.

- Color·Alpha Bake 시간은 기존과 비슷해도 허용한다.
- Native Job 관리 오버헤드는 전체 시간 대비 미미해야 한다.
- Bake 중 UI 갱신이 계속되어야 한다.
- 파일 기록 약 3.86초의 추가 정지는 별도 측정해 후속 비동기화 필요 여부를 판단한다.

---

## 14. 위험과 대응

### 14.1 외부 Bake 결과 오인

위험: Blender의 다른 Bake가 Texture Export 완료로 기록될 수 있다.

대응: Scene, Paint Target 포인터, 현재 Stage와 활성 Job 식별자를 모두 비교한다.

### 14.2 취소 중 조기 정리

위험: `OBJECT_BAKE` 실행 중 임시 Node나 Image를 제거하면 Blender 상태가 손상될 수 있다.

대응: 취소는 플래그만 기록하고, Handler 결과와 Job 종료를 확인한 뒤 정리한다.

### 14.3 Composite 회귀

위험: 공통 Helper 분리 과정에서 기존 Composite 생성·재합성이 깨질 수 있다.

대응: Composite 공개 API와 상태 전이를 유지하고, Texture Export 연결 전에 Composite 자동·GUI 회귀를 통과시킨다.

### 14.4 입력 차단 약화

위험: Composite Modal 구조를 그대로 복사하면 일반 입력이 `PASS_THROUGH`되어 Export 중 Layer가 바뀔 수 있다.

대응: Native Bake 실행 방식만 공유하고 Texture Export의 `BLOCKING` 이벤트 정책은 독립적으로 유지한다.

### 14.5 투명도 변경의 전역 영향

위험: 공통 Overlay의 알파를 바꾸면 COLMAP Import, 데이터 업데이트, Composite 등 모든 진행창 외형이 달라진다.

대응: 이번 작업에서는 전역 알파를 바꾸지 않고 Texture Export를 공통 활동형 Layout에 맞춘다.

---

## 15. 완료 기준

다음 조건을 모두 충족해야 구현 완료로 판정한다.

- Texture Export의 Color·Alpha Bake가 모두 Blender Native `OBJECT_BAKE` Job으로 실행된다.
- Bake 중 진행창과 경과 시간이 계속 갱신된다.
- Bake 중 일반 사용자 입력은 차단된다.
- Color·Alpha Bake 각각에서 `Esc` 취소가 안전하게 동작한다.
- Export 결과의 Color·Alpha·해상도·파일 구조가 기존과 같다.
- Layer 눈 상태, 선택, 활성 Layer, Paint Target과 Brush가 변하지 않는다.
- 기존 파일 원자 확정과 실패 Rollback이 유지된다.
- Hash 검사와 저장 직전 전체 Snapshot 재검사가 다시 추가되지 않는다.
- Composite 생성의 Native Bake와 진행률 UI에 회귀가 없다.
- 모든 임시 Image·Node·Timer·파일이 성공·실패·취소 뒤 제거된다.
- 자동 테스트, 실제 Blender GUI 회귀, ZIP 빌드와 설치본 회귀가 모두 통과한다.

---

## 16. 최종 구조 요약

```text
Export Texture
  ↓
입력·출력 계약 검증
  ↓
TextureExportJob 생성 + 공통 진행률 Overlay 시작
  ↓
Color Native Bake ── 활동형 UI·입력 차단·Esc 취소
  ↓
Alpha Native Bake ── 활동형 UI·입력 차단·Esc 취소
  ↓
Color·Alpha 결합
  ↓
임시 파일 기록
  ↓
Backup + 원자 확정
  ↓
상태 복원 + 임시 자원 정리
  ↓
완료
```

핵심 결정은 **Composite 생성에서 검증된 Native Bake 실행·복원 기반을 공유하되, Texture Export의 입력 차단과 파일 Transaction은 그대로 독립 유지하는 것**이다.
