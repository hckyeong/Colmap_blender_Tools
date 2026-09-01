# COLMAP Modifier Runtime 중복 무결성 검사 제거·빠른 복원 구조변경 계획서

## 1. 문서 정보

- 작성일: 2026-09-01
- 대상 제품: `COLMAP Tools` Blender Extension
- 대상 기능: `COLMAP Modifier` Runtime 복원
- 연관 기능: Projection Painting의 `COLMAP 데이터 업데이트`
- 제외 기능: COLMAP Align, Modifier의 명시적 Import·Export 형식과 편집 기능
- 문서 상태: 0.3.33 구현·실데이터 검증 완료

구현 결과는 [Modifier 빠른 Runtime 복원 0.3.33 Release Note](migration/MODIFIER_FAST_RUNTIME_RESTORE_RELEASE_NOTE.md)에 기록한다.

## 2. 쉬운 결론

현재 Modifier Runtime 복원은 저장된 원본을 다시 읽는 것에 그치지 않고, 명시적 Import와 같은 전체 검사를 반복한다.

```text
현재 Runtime 복원
→ 원본 SHA-256 1차 계산
→ COLMAP 데이터 전체 해석
→ Reconstruction 전체 무결성 검사
→ 이미지 경로 검사
→ 원본 SHA-256 2차 계산
→ 관계 인덱스 생성
→ Modifier 편집 기록 재적용
→ Point3D 순서 생성
```

이 구조를 다음과 같이 분리한다.

```text
사용자가 직접 Import
→ 새로운 데이터로 취급
→ 기존 전체 무결성 검사 유지

저장 파일에서 Runtime 복원
→ 저장된 Source 지문과 현재 Source 지문 비교
→ 동일하면 이전 Import·Export에서 검증된 원본으로 인정
→ Reconstruction 중복 무결성 검사 생략
→ Modifier 편집 기록만 재적용

Source가 변경되었거나 검증 기준이 달라짐
→ 편집 기록을 적용하지 않고 다시 Import하도록 안내
```

Runtime 복원 자체는 유지한다. `.blend`에는 대용량 COLMAP Reconstruction 전체가 아니라 Source 경로·지문과 Modifier 편집 기록이 저장되므로, Blender 재실행 뒤에는 원본을 다시 읽고 편집 기록을 적용해야 한다.

제거 대상은 복원 그 자체가 아니라, 이미 검증한 동일 원본에 대한 **중복 Reconstruction 무결성 검사와 두 번째 전체 해시**다.

## 3. 현재 코드 기준 원인

### 3.1 명시적 Import 경로

`tools/modifier/core/import_job.py`의 `ImportLoadJob`은 다음 단계를 실행한다.

1. `HASH_SOURCE_BEFORE`
2. `READ_MODEL`
3. `VALIDATE_MODEL`
4. `VALIDATE_IMAGES`
5. `HASH_SOURCE_AFTER`
6. `BUILD_RESULT`

`VALIDATE_MODEL`은 `ReconstructionValidationJob`을 실행하며 Camera, Image, Point3D, Track, Rig와 Frame 전체 관계를 순회한다. 대규모 데이터에서는 Point2D·Point3D Track 대응표까지 생성하므로 시간과 메모리 사용량이 크다.

명시적 Import에서는 이 검사가 필요하다. 처음 보는 Source가 편집 가능한 정상 Reconstruction인지 확인해야 하기 때문이다.

### 3.2 Runtime 복원 경로

`tools/modifier/blender/runtime_state.py`의 `RuntimeRestoreJob.__init__()`도 같은 `ImportLoadJob`을 생성한다.

`RuntimeRestoreJob._advance_load()`는 명시적 Import의 원본 해시·해석·무결성 검사·이미지 검사를 그대로 실행한 뒤에야 다음 단계로 넘어간다.

```text
RuntimeRestoreJob
├─ ImportLoadJob 전체 실행
├─ Relationship Index 생성
├─ 저장된 Edit History 재생
├─ Point3D 결정 순서 생성
└─ Runtime Registry에 최종 설치
```

따라서 현재 화면에 표시되는 `Reconstruction 무결성 검사 중`은 편집 기록을 재적용하는 단계가 아니다. 이전에 검증한 원본을 다시 전수 검사하는 중복 단계다.

### 3.3 Export 성공 뒤 상태

`state_from_successful_export()`는 Export된 현재 Reconstruction을 새로운 기준선으로 전환한다.

- `baseline_reconstruction`: Export된 현재 결과
- `operations`: 빈 튜플
- `edit_revision`: `0`
- `history_hash`: 빈 편집 이력의 Hash
- `source_fingerprint`: 검증된 Export 결과의 새 지문

따라서 Export 후 `.blend`까지 저장했다면 다음 실행에서 적용할 이전 편집 기록은 없다. Export 결과는 이미 Staging Read·무결성 검사까지 통과했으므로, 동일한 Source를 Runtime 복원 시 다시 전수 검사할 필요가 없다.

## 4. 변경 목표

1. 명시적 Import의 전체 무결성 검사를 유지한다.
2. Modifier Export의 Staging 무결성 검사와 원자적 교체를 유지한다.
3. 동일하고 이미 검증된 Source의 Runtime 복원에서는 `ReconstructionValidationJob`을 실행하지 않는다.
4. Runtime 복원에서 Source 전체 SHA-256은 한 번만 계산한다.
5. Source가 검사 또는 해석 도중 변경되면 편집 기록 적용 전에 실패시킨다.
6. 저장된 편집 기록, Revision과 History Hash 검증은 그대로 유지한다.
7. 빠른 복원 결과가 기존 전체 복원 결과와 완전히 같아야 한다.
8. Projection Painting의 독립 Snapshot과 수동 업데이트 계약을 변경하지 않는다.
9. 새 대용량 Runtime Cache 파일이나 `.blend` 내 Reconstruction 복사본을 만들지 않는다.

## 5. 변경하지 않는 계약

- `.blend` 로드 직후 Modifier Runtime을 자동 생성하지 않는다.
- Modifier Workspace 첫 진입 또는 Projection Painting의 수동 업데이트에서만 COLD Runtime을 복원한다.
- Modifier는 COLMAP Import·편집·Export의 유일한 권위 생산자다.
- Projection Painting은 업데이트 성공 시점의 값만 자기 Snapshot으로 복사한다.
- Modifier와 Projection Painting은 Blender Camera·Point Resource를 공유하지 않는다.
- Align은 이번 변경과 무관하다.
- Source가 달라졌을 때 기존 편집 기록을 억지로 재적용하지 않는다.
- Export 성공 전에는 원본과 Runtime 기준선을 변경하지 않는다.

## 6. 목표 구조

### 6.1 두 Loader의 역할 분리

공용 `ImportLoadJob`에 `검사 생략` 옵션을 추가하지 않는다. 잘못된 호출에서 신규 Import 검증이 빠지는 것을 막기 위해 역할을 명확히 나눈다.

```text
ImportLoadJob
└─ 신규·수동 Import 전용
   └─ 전체 Hash·Parse·Integrity·Image 검증

RuntimeSourceRestoreJob
└─ 저장된 Modifier Source 복원 전용
   ├─ 저장 Metadata 검증
   ├─ 현재 Source SHA-256 1회 계산
   ├─ 저장 Source Fingerprint와 비교
   ├─ COLMAP Parse
   ├─ Parse 전후 파일 Stat Guard 확인
   ├─ Image Path Manifest 재구성
   └─ 검증된 동일 Source 결과 구성
```

복원 전용 Job은 `expected_source_fingerprint`가 없거나 현재 검증 Schema와 맞지 않으면 시작할 수 없게 한다.

### 6.2 동일 Source 신뢰 조건

다음 조건을 모두 만족할 때만 빠른 복원을 허용한다.

1. Dataset UUID가 비어 있지 않다.
2. 저장된 Model·Images 경로와 Input Format이 유효하다.
3. 저장된 `source_fingerprint`가 유효한 SHA-256이다.
4. 저장된 Source Schema가 현재 복원 코드가 신뢰하는 Schema와 같다.
5. 현재 Source에서 계산한 SHA-256이 저장된 `source_fingerprint`와 같다.
6. Source 파일의 경로 집합과 COLMAP Layout이 저장 상태와 같다.
7. 해시 계산부터 Parse 완료까지 파일 크기·수정 시각·파일 식별 정보가 바뀌지 않았다.
8. 저장된 편집 이력 길이, Revision과 History Hash가 일치한다.

하나라도 다르면 빠른 복원을 중단한다. 변경된 Source에 이전 편집 기록을 적용하는 자동 보정은 하지 않는다.

### 6.3 Source 변경 경쟁 방지

현재 `SourceFingerprintJob`이 내부에서 사용하는 파일별 Stat 정보를 불변 `SourceFileSignature`로 노출한다.

각 Signature는 최소한 다음 값을 가진다.

- 정규화된 상대 경로
- 파일 크기
- `mtime_ns`
- 플랫폼에서 제공되는 경우 Device·File ID

복원은 다음 순서로 수행한다.

```text
1. Source 파일 목록 확정
2. 파일별 Stat 캡처와 SHA-256 계산
3. 저장 Fingerprint와 비교
4. COLMAP Parse
5. 모든 파일의 현재 Stat을 다시 비교
6. 같을 때만 복원 결과 생성
```

이 방식으로 기존 `HASH_SOURCE_AFTER`의 두 번째 전체 파일 읽기를 제거하면서, Parse 중 외부 변경은 감지한다.

### 6.4 검증 Token의 출처 구분

`ValidatedReconstructionToken`을 무조건 새로 발급하지 않고 검증 출처를 구분한다.

- `FULL_IMPORT`: 이번 Import에서 전체 무결성 검사 완료
- `VALIDATED_EXPORT`: 이번 Export Staging에서 전체 무결성 검사 완료
- `TRUSTED_RESTORE`: 동일 Fingerprint·동일 Schema임을 확인하여 이전 검증을 승계

관계 인덱스는 정확한 Reconstruction 객체, Dataset UUID, Source Fingerprint와 Validation Revision이 Token과 일치할 때만 전수 재검사를 생략한다.

검증 출처는 Runtime 내부 값으로 관리하며 사용자 `.blend`에 불필요한 대형 데이터나 자동 마이그레이션 기록을 추가하지 않는다.

### 6.5 편집 기록 재적용

빠른 Source 복원이 완료된 뒤 현재 순서를 그대로 유지한다.

```text
검증된 동일 원본 Reconstruction
→ Point·Image·Frame 관계 인덱스 생성
→ 저장된 Modifier 편집 Operation 순차 재생
→ Point3D 결정 순서 생성
→ Scene Token 최종 재검증
→ Runtime Registry에 한 번만 설치
```

복원 도중 실패하거나 Workspace를 벗어나면 부분 Reconstruction, Index와 편집 결과를 폐기한다. 완성되기 전 전역 Runtime에는 설치하지 않는다.

## 7. 사용자 경험

### 7.1 처음 COLMAP 데이터를 Import할 때

```text
COLMAP Import 실행
→ 원본 데이터 확인
→ COLMAP 데이터 읽기
→ Reconstruction 무결성 검사
→ 이미지 파일 확인
→ Import 완료
```

처음 보는 데이터이므로 기존과 동일하게 전체 검사를 보여준다. 안전성은 줄어들지 않는다.

### 7.2 Blender를 다시 실행하고 Modifier에 처음 들어갈 때

```text
Modifier Workspace 진입
→ Workspace UI 즉시 표시
→ 저장된 COLMAP 원본 확인
→ COLMAP 데이터 읽기
→ 저장된 Modifier 편집 내용 적용
→ Modifier 작업 준비 완료
```

동일한 Source라면 `Reconstruction 무결성 검사 중` 단계가 나타나지 않는다.

권장 진행 문구는 다음과 같다.

- `Modifier Runtime 복원 중 · 저장된 원본 확인`
- `Modifier Runtime 복원 중 · COLMAP 데이터 읽기`
- `Modifier Runtime 복원 중 · 편집 관계 준비`
- `Modifier Runtime 복원 중 · 저장된 편집 내용 적용`
- `Modifier Runtime 복원 중 · Point3D 순서 준비`
- `Modifier Runtime 복원 완료`

### 7.3 같은 Blender 실행 중 다시 Modifier로 돌아올 때

Runtime Cache와 Scene Token이 일치하면 원본을 다시 읽지 않는다.

```text
Modifier Workspace 재진입
→ 기존 Runtime 즉시 재연결
```

진행률 UI가 다시 나타나지 않는 것이 정상이다.

### 7.4 Modifier에서 편집한 뒤 저장·재실행할 때

```text
원본 COLMAP 데이터 읽기
→ 저장된 편집 Operation 재생
→ 마지막 Modifier 편집 상태 복원
```

무결성 검사는 생략해도 편집 기록 재적용은 생략하지 않는다. 사용자가 삭제하거나 수정한 결과는 기존과 동일하게 복원된다.

### 7.5 Modifier Export 후 저장·재실행할 때

```text
Export 성공
→ 편집 결과를 새 원본 기준선으로 전환
→ 편집 기록과 Revision을 0으로 초기화
→ .blend 저장
→ 재실행 시 Export된 원본만 빠르게 복원
```

Export 결과는 이미 검증됐고 편집 기록도 비워졌으므로 중복 적용은 발생하지 않는다.

Export 후 `.blend`를 저장하지 않았다면 저장 파일의 이전 Fingerprint와 디스크의 새 Source가 다르다. 이 경우 빠른 복원은 실패시키고 다시 Import하도록 안내한다.

### 7.6 원본 파일을 외부 프로그램에서 변경했을 때

```text
Modifier Workspace 진입
→ Source Fingerprint 불일치 감지
→ Runtime 복원 중단
→ 이전 Modifier 편집 기록 적용 안 함
→ 다시 Import 안내
```

표시 문구는 원인을 직접 설명한다.

```text
원본 COLMAP 파일이 마지막 Import 또는 Export 이후 변경되었습니다.
기존 편집 기록은 적용하지 않았습니다. COLMAP 데이터를 다시 Import하십시오.
```

### 7.7 Projection Painting에서 COLMAP 데이터 업데이트를 누를 때

Modifier Runtime이 COLD이면 같은 빠른 복원 Job을 공유한다.

```text
COLMAP 데이터 업데이트
→ 저장된 Modifier 원본 확인
→ Modifier 편집 결과 복원
→ Projection Painting 전용 Snapshot 생성
→ 성공 시 기존 Snapshot을 새 Snapshot으로 교체
```

복원 실패·취소 시 기존 Projection Painting Camera, Point, Layer와 Texture는 그대로 유지한다.

## 8. 진행률 재배치

Runtime 복원 전용 진행률은 실제 비용을 기준으로 별도 범위를 사용한다.

| 단계 | 권장 범위 | 의미 |
| --- | ---: | --- |
| Source 확인·Fingerprint | 0~15% | 저장 Source와 현재 Source가 같은지 확인 |
| Reconstruction Parse | 15~65% | Camera·Image·Point3D 읽기 |
| Image Manifest | 65~68% | Camera Review 이미지 경로 준비 |
| Relationship Index | 68~84% | Modifier 편집용 역방향 관계 생성 |
| Edit History Replay | 84~94% | 저장된 Modifier 편집 내용 적용 |
| Point3D Order | 94~99.5% | 결정적인 표시·처리 순서 준비 |
| Commit | 99.5~100% | Scene Token 확인 후 Runtime 설치 |

실제 벤치마크 결과에 따라 비율은 조정하되, 완료 전 100%를 표시하지 않는다.

## 9. 실패 처리

| 상황 | 처리 |
| --- | --- |
| Source 경로 없음 | Runtime 복원 중단, 다시 Import 안내 |
| Source Fingerprint 불일치 | 편집 기록 미적용, 다시 Import 안내 |
| 해시·Parse 중 파일 변경 | Stale Source로 실패, 부분 결과 폐기 |
| 저장 History Hash 불일치 | 편집 기록 손상으로 실패, 자동 수정 금지 |
| 저장 Schema가 현재 기준과 다름 | 빠른 복원 금지, 명시적 재Import 안내 |
| Parser 오류 | 정확한 파일과 레코드 오류 표시 |
| Image 파일 누락 | Camera Review에 사용할 누락 경로 표시, 기존 정책에 따라 실패 또는 경고 |
| Workspace 이탈·취소 | 미완료 Job과 파일 Handle 정리, Cache 미설치 |
| Projection Painting 업데이트 중 실패 | 기존 Snapshot과 Paint 자산 유지 |

## 10. 구현 단계

### 단계 0. 현재 병목 계측

- 실제 데이터에서 `HASH_SOURCE_BEFORE`, `READ_MODEL`, `VALIDATE_MODEL`, `VALIDATE_IMAGES`, `HASH_SOURCE_AFTER`, Index, History와 Point Order 시간을 각각 기록한다.
- 계측 로그는 테스트 전용으로만 두고 문제 해결 뒤 제품 코드에서 삭제한다.
- 명시적 Import와 COLD Runtime 복원의 호출 횟수를 분리한다.

완료 조건: 복원 지연 중 각 단계의 실제 시간을 비교할 수 있어야 한다.

### 단계 1. Source Signature 계약 분리

- `SourceFingerprintJob` 결과에 Fingerprint와 파일별 Signature Snapshot을 함께 제공한다.
- Parse 완료 뒤 같은 Signature인지 검증하는 순수 Python 함수를 추가한다.
- 기존 `source_fingerprint()` 공개 반환값과 명시적 Import 동작은 유지한다.

완료 조건: 파일 내용 Hash는 같더라도 해석 중 파일이 바뀌면 Commit 전에 실패해야 한다.

### 단계 2. Runtime 전용 Source Restore Job 추가

- 명시적 Import와 분리된 `RuntimeSourceRestoreJob`을 추가한다.
- 저장 Fingerprint 확인 전에는 Reconstruction을 Runtime 기준선으로 승인하지 않는다.
- 동일 Fingerprint에서는 `ReconstructionValidationJob`과 `HASH_SOURCE_AFTER`를 실행하지 않는다.
- Image Path Manifest는 Camera Review에 필요하므로 기존 검증 결과 형식과 동일하게 구성한다.

완료 조건: 동일 Source 복원 결과가 기존 `ImportLoadJob` 결과와 값 단위로 같아야 한다.

### 단계 3. Modifier RuntimeRestoreJob 연결

- `RuntimeRestoreJob`이 `ImportLoadJob` 대신 복원 전용 Job을 사용하게 한다.
- Index, History, Point Order와 마지막 Commit 순서는 유지한다.
- Runtime Cache가 이미 있으면 현재처럼 파일 작업 없이 즉시 재연결한다.

완료 조건: COLD 복원에서 전체 무결성 검사 호출은 0회이고 편집 결과는 기존과 같아야 한다.

### 단계 4. UI 진행 상태 교체

- 명시적 Import UI의 `Reconstruction 무결성 검사 중` 문구는 유지한다.
- Runtime 복원 UI에서는 빠른 복원 전용 단계 이름을 표시한다.
- Source 불일치 메시지에 `기존 편집 기록은 적용하지 않았다`는 내용을 포함한다.
- Projection Painting 업데이트 진행창에서도 같은 복원 단계가 표시되게 한다.

완료 조건: 사용자가 Import 검증과 저장 상태 복원을 구분할 수 있어야 한다.

### 단계 5. 회귀·실데이터 성능 검증

- Core, Blender Background와 실제 GUI 검사를 실행한다.
- 실제 사용자 데이터에서 기존 전체 복원과 빠른 복원의 결과·시간·메모리를 비교한다.
- 디버그 계측과 임시 파일을 모두 제거한다.

완료 조건: 기능 동등성과 성능 승인 기준을 모두 통과해야 한다.

## 11. 예상 변경 파일

| 경로 | 변경 내용 |
| --- | --- |
| `tools/modifier/core/fingerprint.py` | 파일별 Signature Snapshot·사후 검증 경계 추가 |
| `tools/modifier/core/runtime_source_restore_job.py` | 동일 Source 전용 증분 Restore Loader 추가 |
| `tools/modifier/core/model.py` | 필요 시 검증 출처 또는 Restore 결과 계약 추가 |
| `tools/modifier/blender/runtime_state.py` | `ImportLoadJob` 대신 Restore Loader 연결·진행률 재배치 |
| `tools/modifier/blender/modifier_workspace.py` | 빠른 복원 상태·오류 문구 표시 |
| `tools/modifier/blender/projection_snapshot_provider.py` | 기존 공유 Restore 진행값 전달 확인 |
| `tests/modifier/core/` | 빠른 복원, Source 변경, 결과 동등성 테스트 |
| `tests/modifier/blender_background/` | Workspace COLD·WARM 복원과 Export 후 복원 테스트 |
| `tests/projection_paint/blender_background/` | COLD Modifier 업데이트와 기존 Snapshot 보존 테스트 |
| `tests/blender_gui/` | 실제 데이터 단계별 시간과 UX 회귀 |
| `docs/` | 구현 현황, 테스트 방법과 Release Note 갱신 |

변경 파일은 구현 중 확인된 최소 범위로 제한한다. Align과 Projection Painting의 Layer·Texture·Stencil 코드는 수정하지 않는다.

## 12. 테스트 계획

### 12.1 Core

- 명시적 Import는 `ReconstructionValidationJob`을 정확히 한 번 실행한다.
- 동일 Fingerprint Runtime 복원은 `ReconstructionValidationJob`을 실행하지 않는다.
- Runtime 복원은 Source 전체 Fingerprint를 한 번만 계산한다.
- 기존 전체 복원과 빠른 복원의 Camera·Image·Point3D·Rig·Frame 값이 같다.
- Point·Image·Frame Relationship Index가 같다.
- 편집 이력 없음, Point 삭제, Image 삭제와 혼합 이력 결과가 같다.
- Source Fingerprint 불일치 시 History Replay 호출은 0회다.
- Parse 중 파일 크기·수정 시각·파일 ID 변경을 감지한다.
- 취소 뒤 열린 Stream과 부분 결과가 남지 않는다.
- TXT·BIN과 Legacy·Modern Layout을 모두 검사한다.

### 12.2 Blender Background

- `.blend` 로드 직후 자동 Runtime 복원은 계속 0회다.
- Modifier Workspace 첫 진입에서만 COLD 복원이 시작된다.
- 동일 실행의 재진입은 Source 파일을 다시 읽지 않는다.
- Scene의 Edit Revision·History Hash가 바뀌면 기존 Job을 Commit하지 않는다.
- Export 성공 뒤 `operations=()`, `edit_revision=0` 상태로 빠르게 복원한다.
- Export 후 `.blend` 미저장 상태를 재현하면 Fingerprint 불일치로 안전하게 중단한다.
- 복원 실패 시 기존 Projection Painting Snapshot이 변하지 않는다.

### 12.3 실제 GUI와 사용자 경험

```text
1. 실제 .blend 파일 열기
2. Modifier Workspace 진입
3. `Reconstruction 무결성 검사 중` 문구가 나타나지 않는지 확인
4. 기존 Modifier 편집 결과와 Camera Review 확인
5. 다른 Workspace로 이동 후 Modifier 재진입
6. 진행률 없이 즉시 Runtime 재연결되는지 확인
7. Modifier 편집 후 .blend 저장·재실행
8. 편집 결과가 다시 적용되는지 확인
9. Modifier Export 후 .blend 저장·재실행
10. 편집 기록 중복 없이 Export 결과가 기준선인지 확인
11. 원본 파일을 외부에서 변경
12. 편집 기록을 적용하지 않고 다시 Import 안내가 뜨는지 확인
13. Projection Painting의 COLMAP 데이터 업데이트 실행
14. 기존 Snapshot 유지와 새 Snapshot의 원자 교체 확인
```

## 13. 성능 승인 기준

- 동일 Source COLD Runtime 복원에서 `ReconstructionValidationJob` 생성: `0회`
- 동일 Source COLD Runtime 복원에서 전체 Source SHA-256 계산: `1회`
- 동일 실행 WARM 재진입에서 Source Read·Hash·Parse: `0회`
- History Replay: 저장된 Operation당 정확히 `1회`
- Runtime Registry 설치: 성공한 복원당 `1회`
- 최대 Main Thread 처리 Tick: 목표 `50ms 이하`
- 실제 데이터에서 COLD Runtime 총 복원 시간이 기존 대비 유의미하게 감소해야 한다.
- 최소 목표는 Source Load·검증 구간 `30% 이상 단축`이며, 미달하면 남은 Phase 병목을 다시 계측한다.
- 빠른 복원 결과의 Camera·Image·Point3D 수, 편집 Revision과 History Hash는 기존 방식과 같아야 한다.

## 14. 위험과 대응

| 위험 | 대응 |
| --- | --- |
| 신규 Import에서 검사가 실수로 빠짐 | Import Job과 Restore Job 클래스를 분리하고 Restore에는 expected Fingerprint를 필수화 |
| 외부 Source 변경을 놓침 | 전체 SHA-256 1회와 Parse 전후 파일 Signature 비교를 함께 사용 |
| 검증 규칙이 업데이트됐는데 이전 검증을 신뢰함 | Source Schema가 다르면 빠른 복원 금지·명시적 재Import 안내 |
| 잘못된 편집 History를 동일 Source에 적용함 | Revision·History Hash·Operation Source Fingerprint를 모두 재검증 |
| Image 경로 변경을 놓침 | Reconstruction 검사는 생략해도 Image Path Manifest 검사는 유지 |
| 중간 결과가 Runtime에 노출됨 | Source·Index·History·Point Order 완료 후 한 번만 Commit |
| 진행률은 빨라졌지만 UI가 멈춤 | 기존 Tick 예산과 취소 경계를 유지하고 단계별 최대 Tick을 측정 |
| 새 Cache 파일 관리가 복잡해짐 | 이번 범위에서는 디스크 Runtime Cache를 만들지 않음 |

## 15. 완료 조건

1. 직접 Import는 기존 전체 무결성 검사를 유지한다.
2. Export는 기존 Staging 무결성 검사와 원본 교체 안전성을 유지한다.
3. 동일 Source Runtime 복원에서 Reconstruction 전체 무결성 검사를 반복하지 않는다.
4. 동일 Source Runtime 복원에서 Source 전체 Hash는 한 번만 계산한다.
5. 외부 변경 Source에는 기존 편집 기록을 적용하지 않는다.
6. 기존 Modifier 편집 결과가 저장·재실행 뒤 동일하게 복원된다.
7. Export 성공 뒤 편집 기록이 비워지고 새 원본이 기준선이 된다.
8. Export 후 `.blend` 미저장 상태는 중복 적용 대신 Source 불일치로 차단된다.
9. Projection Painting의 기존 Snapshot과 Paint 자산 독립성이 유지된다.
10. Align에는 영향이 없다.
11. Pure Python, Blender Background와 실제 GUI 회귀가 통과한다.
12. 실제 데이터에서 복원 시간과 최대 UI Tick이 승인 기준을 만족한다.
13. 제품 코드에 임시 계측·디버그 로그가 남지 않는다.

## 16. 최종 사용자 흐름

```text
처음 Import
→ 전체 무결성 검사
→ 안전한 기준 Source 저장

Modifier 편집 후 .blend 저장
→ Blender 재실행
→ 동일 Source 지문 확인
→ 원본 빠르게 읽기
→ 저장된 편집 내용 재적용
→ Modifier 작업 재개

Modifier Export 후 .blend 저장
→ Export 결과가 새 기준 Source
→ 편집 기록은 비어 있음
→ 재실행 시 Export 결과만 빠르게 복원

원본 외부 변경
→ 지문 불일치
→ 이전 편집 내용 적용 안 함
→ 다시 Import 안내
```

이 변경은 안전 검사를 없애는 것이 아니다. 처음 Import와 Export에서 완료된 검증을 동일한 Source 복원에서 다시 반복하지 않도록 검증 시점을 분리하는 변경이다.
