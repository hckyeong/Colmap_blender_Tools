# COLMAP Align·Modifier 내장 Runtime Snapshot 및 이미지 폴더 재연결 구조변경 계획서

## 1. 문서 정보

- 작성일: 2026-09-01
- 대상 제품: `COLMAP Tools` Blender Extension
- 대상 Blender: 5.2 LTS 이상
- 기준 버전: `0.3.33`
- 대상 기능: `COLMAP Align`, `COLMAP Modifier`, 외부 COLMAP 이미지 연결
- 유지 기능: Projection Painting의 독립 Blender ID Snapshot과 수동 업데이트 계약
- 문서 상태: 구현 전 계획

## 2. 간단 요약

현재 Align과 Modifier는 `.blend`에 원본 경로와 작업 상태만 저장하고, Blender를 다시 열면 외부 COLMAP 파일을 다시 읽어 Runtime을 만든다. Modifier는 원본 읽기뿐 아니라 Point와 Image의 편집 관계 인덱스도 다시 생성하므로 실데이터에서 전체 복원에 약 121.57초가 걸렸다.

구조를 다음과 같이 바꾼다.

```text
최초 Import
→ 외부 COLMAP 전체 검증
→ 해석된 Reconstruction 생성
→ 편집 관계·표시 순서 생성
→ 작업 Runtime만 메모리에 설치
→ 디스크 Snapshot 없음·저장 필요

Blender 재실행
→ 외부 COLMAP 파일을 다시 읽지 않음
→ 내장 Runtime Snapshot 압축 해제·복원
→ Align·Modifier Runtime 연결
→ 복원 성공한 Snapshot Text Chunk를 현재 메모리에서 즉시 제거
→ 작업 중 Runtime만 유지
```

디스크의 마지막 저장 `.blend`에는 Snapshot이 그대로 남는다. 현재 Blender 메모리에서 Text Chunk를 제거해도 사용자가 저장하지 않으면 기존 `.blend`는 바뀌지 않는다.

```text
.blend 저장
→ 현재 Runtime으로 Snapshot Chunk 재생성
→ Snapshot 검증
→ Blender 파일에 기록
→ save_post에서 현재 메모리의 Snapshot Text Chunk 다시 제거
```

Modifier Export도 검증된 Runtime과 Provenance·Edit Revision Token을 권위값으로 사용한다.

```text
현재
Staging 작성
→ Staging 재Parse
→ Reconstruction 전체 무결성 검사
→ Runtime과 Staging 전체 비교
→ Staging 파일 재Hash
→ 원자 Commit

변경 후
Runtime Provenance·Edit Revision Token 확인
→ Staging을 한 번 작성하면서 형식·수량·Digest·Hash 계산
→ 파일 닫기 뒤 크기·수량·Digest·Token 확인
→ Backup·원자 Commit
→ 현재 Runtime을 새 외부 출력 기준선으로 확정
→ Runtime 변경됨·.blend 저장 필요
```

검증을 없애는 변경이 아니다. 동일 데이터를 다시 읽고 비교하는 중복 Pass를 제거하고, 작성 과정에서 검증 증명값을 만드는 구조로 바꾼다. 출력 경로·권한·여유 공간 검사, Staging 파일, Backup, 원자 교체와 Rollback은 유지한다.

카메라 원본 이미지는 용량 때문에 `.blend`에 넣지 않는다. 이미지 연결은 다음 계약을 사용한다.

```text
저장값
├─ Images Folder 절대경로
└─ IMAGE_ID별 원본 파일명

절대경로를 읽지 못함
→ [Images Folder 지정]
→ 사용자가 새 절대경로 선택
→ 선택 폴더의 이미지 파일명을 IMAGE_ID별 파일명과 비교
→ 누락·중복이 없을 때만 전체 경로 원자 교체
```

상대경로는 저장·복구 계약에 사용하지 않는다.

## 3. 현재 코드 기준 문제

### 3.1 Modifier

`tools/modifier/blender/runtime_state.py`는 저장된 Source 경로·Fingerprint·편집 History를 사용해 COLD Runtime을 복원한다.

현재 `0.3.33`은 중복 Reconstruction 전체 무결성 검사를 제거했지만 다음 작업은 계속 수행한다.

```text
Source SHA-256 1회
→ COLMAP Parse
→ 이미지 경로 확인
→ Point ↔ Image 관계 인덱스 전체 생성
→ 편집 History 재생
→ Point3D 순서 생성
```

사용자 실데이터의 단계별 결과는 다음과 같다.

| 단계 | 시간 |
| --- | ---: |
| Source 확인·읽기 | 33.14초 |
| 관계 인덱스 생성 | 78.41초 |
| 편집 History 재생 | 0.05초 |
| Point3D 순서 생성 | 9.24초 |
| 합계 | 121.57초 |

외부 원본을 그대로 다시 Import하는 것보다 Runtime 복원이 오래 느껴지는 주원인은 관계 인덱스 재생성이다.

### 3.2 Align

Align은 `tools/align/project_persistence`의 Text Snapshot에 Source 경로, Catalog, Alignment, Marker와 작업 상태를 저장한다. 대용량 Reconstruction 배열은 Runtime Repository에만 존재하므로 파일을 다시 열면 외부 Source를 다시 Import한다.

이번 변경 뒤에도 Align은 Modifier·Projection Painting과 데이터 소유권을 공유하지 않는다. Align 전용 Runtime Snapshot을 별도로 소유한다.

### 3.3 Projection Painting

Projection Painting은 이미 Modifier 값을 다음 Blender ID로 복사해 `.blend`에 저장한다.

- 전용 Point Mesh
- 전용 Camera Object와 Camera Data
- Timeline Marker
- Dataset·Image Manifest
- Source UUID·Revision·Fingerprint

다만 Painting에 필요 없는 전체 Point2D 관측, Modifier 편집 인덱스와 History는 저장하지 않는다. 기존 독립 Snapshot과 Paint·Undo 계약은 유지한다.

### 3.4 이미지 경로

현재 Modifier와 Projection Painting의 이미지 Manifest는 검증된 절대경로를 저장한다. 프로젝트와 이미지 폴더를 다른 위치로 옮기면 기존 절대경로가 끊긴다.

이번 변경은 상대경로로 자동 변환하지 않는다. 사용자가 새 Images Folder의 절대경로를 지정하면 저장된 파일명으로 다시 연결한다.

### 3.5 Modifier Export 중복 재검증

현재 `tools/modifier/core/export_job.py`는 다음 단계를 순서대로 실행한다.

```text
WRITE_STAGING
→ PARSE_STAGING
→ VALIDATE_STAGING
→ COMPARE_STAGING
→ HASH_STAGING
→ COMMIT
```

`tools/modifier/core/export_staging_jobs.py`가 작성한 COLMAP 파일을 다시 Parser로 읽고, `tools/modifier/core/export_integrity_job.py`로 전체 Reconstruction을 검사한 뒤, Runtime의 Camera·Image·Point3D·Point2D·Track을 다시 값 단위로 비교한다. 마지막에는 작성한 파일을 다시 읽어 SHA-256을 계산한다.

이 구조는 Export 직전 Runtime의 출처와 편집 Revision을 신뢰할 수 없을 때는 방어적 의미가 있다. 이번 계획처럼 검증된 Runtime Provenance와 Edit Revision Token을 권위값으로 고정하면, 같은 데이터를 작성·재해석·전체 비교·재Hash하는 Pass는 중복이다. 특히 대용량 Point2D와 Track을 여러 번 순회하므로 Export 초반 병목과 메모리 사용을 키운다.

다만 Staging 파일을 직접 최종 경로에 쓰거나 작성 결과를 아무 검증 없이 Commit해서는 안 된다. Writer가 출력하는 Byte와 Record를 한 번의 Pass에서 검사·집계하고, 파일을 닫은 뒤 증명값을 확인한 경우에만 기존 Backup·원자 교체 절차로 넘어가야 한다.

## 4. 실데이터 용량 기준

현재 사용자 데이터의 원본 크기는 다음과 같다.

| 데이터 | Point3D | 관측 연결 | 원본 TXT | zlib 6 압축 |
| --- | ---: | ---: | ---: | ---: |
| Modifier merged | 709,048 | 2,880,086 | 207.67MiB | 95.47MiB |
| Align Component 합계 | 709,048 | 2,880,086 | 207.07MiB | 95.29MiB |

현재 `Untitled.blend`는 142.12MiB다. 완성된 Runtime Snapshot을 별도로 내장하면 예상 증가는 다음과 같다.

| Snapshot | 예상 증가 |
| --- | ---: |
| Modifier 완성 Runtime | 115~165MiB |
| Align 완성 Runtime | 100~150MiB |
| 합계 | 215~315MiB |
| 예상 최종 `.blend` | 357~457MiB |

Blender 파일 압축, 배열 자료형과 관측 Index 압축률에 따라 약 ±30% 차이가 날 수 있다. 구현 전후 실제 ZIP·`.blend` 크기를 반드시 기록한다.

## 5. 목표

1. Align과 Modifier의 COLMAP Runtime 복원 입력을 `.blend` 내부에 저장한다.
2. 같은 Snapshot Schema에서 재실행할 때 외부 sparse 파일 읽기·Hash·Parse를 수행하지 않는다.
3. Modifier 관계 인덱스와 Point3D 결정 순서를 다시 계산하지 않는다.
4. Modifier 편집 History와 현재 편집 결과를 동일하게 복원한다.
5. Align Component, Alignment Transform, Marker와 선택 상태를 동일하게 복원한다.
6. 이미지 파일은 외부에 유지한다.
7. 이미지 루트는 절대경로로 저장하고 IMAGE_ID별 파일명으로 연결한다.
8. 저장된 이미지 경로가 끊기면 사용자가 새 Images Folder를 지정할 수 있게 한다.
9. 이미지 재연결은 누락·중복 검사를 통과한 뒤 한 번에 Commit한다.
10. Projection Painting의 기존 독립 Snapshot, Paint Target, Layer, Texture, Stencil과 Undo를 변경하지 않는다.
11. 기존 프로젝트를 자동 마이그레이션하지 않는다.
12. Snapshot Schema가 다르면 원인을 표시하고 명시적 재생성을 요구한다.
13. Modifier Export의 Staging 재Parse·전체 무결성 재검사·Runtime 전체 비교·사후 파일 재Hash를 제거한다.
14. Modifier Export는 단일 작성 Pass의 형식·수량·Digest·Hash 검증과 기존 Backup·원자 Commit·Rollback을 유지한다.
15. Snapshot 복원 성공 뒤 인코딩된 Text Chunk의 상시 메모리 점유를 `0`으로 만든다.
16. Blender 저장 때만 현재 Runtime Snapshot을 일시적으로 생성하고 저장 완료·실패 뒤 메모리에서 제거한다.

## 6. 제외 범위

- 카메라 원본 이미지의 `.blend` Pack
- Texture 이미지의 `.blend` Pack
- 상대경로 저장 또는 `//images` 자동 변환
- Align·Modifier·Projection Painting의 Runtime 공유
- Projection Painting의 Modifier 자동 동기화
- 기존 프로젝트의 자동 Snapshot 생성·자동 Schema 변환
- `pickle` 기반 직렬화
- 손상되거나 버전이 다른 Snapshot의 추측 복구
- Align이 사용하는 외부 OBJ와 Texture의 새 내장 정책
- Modifier Export의 Staging·Backup·원자 교체·Rollback 안전장치 제거
- 작업 중 Snapshot Text를 외부 Sidecar 파일을 영구 권위값으로 사용하는 구조

Align OBJ를 완전 이동 가능하게 만드는 기능은 OBJ·UV·Material·Texture 경로를 별도 검토해야 하므로 이번 COLMAP Runtime Snapshot 범위와 분리한다.

## 7. 도구별 소유 구조

```text
.blend
├─ Align Runtime Snapshot
│  ├─ Align Manifest
│  ├─ Component Reconstruction Section
│  ├─ Catalog·Transform Section
│  └─ Marker·Selection Section
│
├─ Modifier Runtime Snapshot
│  ├─ Modifier Manifest
│  ├─ Baseline Reconstruction Section
│  ├─ Relationship Index Section
│  ├─ Point Order Section
│  └─ Edit State Section
│
└─ Projection Painting Snapshot
   ├─ 기존 Point Mesh·Camera·Marker
   └─ 기존 Paint Target·Layer·Texture
```

- Align은 Align Snapshot만 읽고 쓴다.
- Modifier는 Modifier Snapshot만 읽고 쓴다.
- Projection Painting은 기존처럼 수동 업데이트 때 Modifier의 확정값만 복사한다.
- `shared.colmap_dataset`과 Modifier Dataset Adapter는 수동 업데이트용 Source Metadata·Blender ID 조회 경계로 유지한다.
- Projection Painting은 업데이트가 끝난 뒤 Source Blender ID Pointer를 영구 보관하지 않고 새 전용 Blender ID를 생성한다.
- 도구끼리 Runtime 객체나 영구 Blender ID Pointer를 저장·공유하지 않는다. 공용 Dataset은 Projection Painting Runtime이나 Undo의 권위값이 아니다.

## 8. 내장 Snapshot 저장 형식

### 8.1 저장 매체

대용량 임의 Binary를 안전하게 `.blend`에 저장하기 위해 숨김 Blender Text Data Block을 사용한다.

```text
.colmap_runtime_modifier_<dataset_uuid>_<generation>_manifest
.colmap_runtime_modifier_<dataset_uuid>_<generation>_<section>_<chunk_index>
.colmap_runtime_align_<project_uuid>_<generation>_manifest
.colmap_runtime_align_<project_uuid>_<generation>_<section>_<chunk_index>
```

각 Section은 다음 순서로 기록한다.

```text
고정 Schema Binary
→ Section별 zlib 압축
→ Base85 문자열 변환
→ 숨김 Text Data Block 기록
```

Base85는 Blender Text에 Binary를 안전하게 저장하기 위한 Encoding일 뿐이며 데이터 의미를 갖지 않는다.

논리 Section 하나를 100MiB급 Text 하나로 만들지 않는다. 압축·Base85 결과를 고정 최대 크기의 물리 Chunk로 나누고 Chunk 하나당 숨김 Text 하나를 사용한다.

- `MAX_ENCODED_CHUNK_BYTES`는 구현 상수로 고정하며 초기 상한은 8MiB로 둔다.
- 실데이터 Blender 저장·로드·메모리 Benchmark 결과에 따라 더 낮출 수 있지만 검증 없이 높이지 않는다.
- Text `write()`는 Chunk 단위로만 호출하고, 한 번에 전체 Section 문자열을 다시 결합하지 않는다.
- Decoder도 Chunk를 순서대로 읽어 Base85 해제·압축 해제·Hash 계산을 Streaming으로 수행한다.

### 8.2 금지 형식

다음 형식은 사용하지 않는다.

- 임의 Python 객체를 실행할 수 있는 `pickle`
- Python `repr()` 복원
- Blender Context·Pointer·메모리 주소 저장
- 하나의 거대한 JSON 숫자 배열
- 압축 해제 크기 상한이 없는 Payload

### 8.3 공통 Manifest

```text
RuntimeSnapshotManifest
├─ tool_id
├─ schema_version
├─ generation_uuid
├─ owner_uuid
├─ source_dataset_uuid
├─ source_fingerprint
├─ created_blender_version
├─ created_addon_version
├─ created_at
├─ section_count
├─ total_uncompressed_bytes
├─ total_encoded_bytes
└─ sections[]
   ├─ section_id
   ├─ record_schema
   ├─ dtype·shape 또는 string table 계약
   ├─ record_count
   ├─ uncompressed_bytes
   ├─ compressed_bytes
   ├─ sha256
   ├─ chunk_count
   └─ chunks[]
      ├─ chunk_index
      ├─ encoded_bytes
      └─ sha256
```

압축을 해제하면서 Section SHA-256을 함께 계산한다. Snapshot을 읽기 위한 Pass와 검증 Pass를 따로 반복하지 않는다.

### 8.4 배열 계약

- 숫자 배열은 명시적 little-endian 자료형을 사용한다.
- ID는 실제 최대값 검사 뒤 `uint32` 또는 `uint64`를 Manifest에 명시한다.
- 좌표·Pose·Error의 정밀도는 기존 Runtime과 동일하게 유지한다.
- 가변 길이 관측 목록은 `offsets + flat values` 구조로 저장한다.
- 이미지 이름은 UTF-8 String Table과 Offset 배열로 저장한다.
- 압축 해제 전 레코드 수·배열 크기·최대 허용 Byte를 검증한다.

### 8.5 메모리 상주 계약

Snapshot Text는 `.blend` 저장 형식이지 작업 Runtime이 아니다. Blender가 파일을 열면서 Text Data Block을 메모리에 올리지만, Add-on은 복원 성공 뒤 이를 계속 보관하지 않는다.

```text
디스크 Snapshot Text
→ Chunk Streaming Decode
→ Runtime Codec·참조 무결성 검증
→ Runtime Registry 원자 설치
→ 해당 Tool의 Manifest·Chunk Text 제거
→ MEMORY_TEXT_RELEASED·DISK_SNAPSHOT_CURRENT
```

- Runtime 설치가 완전히 성공한 Tool의 Text만 제거한다.
- Schema 불일치·Chunk 손상·Decode 실패가 발생하면 원본 Text를 제거하지 않는다.
- Base85 문자열, 압축 Byte와 Decode 중간 Buffer는 Chunk 처리가 끝나는 즉시 해제한다.
- 정상 작업 중에는 Runtime, Baseline, Relationship Index와 History만 메모리에 유지한다.
- Snapshot Text는 `save_pre`의 저장 준비 구간에서만 다시 생성한다.
- `save_post` 또는 `save_post_fail`에서 현재 메모리의 Snapshot Text를 제거하고 Runtime-only 상태로 돌아간다.
- 디스크의 이전 `.blend`는 사용자가 새 저장을 완료하기 전까지 마지막 Snapshot을 계속 보존한다.

### 8.6 메모리 상태와 디스크 저장 상태 분리

Text가 메모리에 없다는 사실만으로 사용자 데이터가 미저장인 것은 아니다. 다음 두 상태축을 분리한다.

| 상태축 | 값 | 의미 |
| --- | --- | --- |
| 메모리 Text | `TEXT_RELEASED` | 정상 복원 또는 저장 후 Text 제거 완료 |
| 메모리 Text | `TEXT_SAVE_TRANSIENT` | Blender 저장을 위해 일시 생성 중 |
| 메모리 Text | `TEXT_ERROR_RETAINED` | 복원 실패 원본을 진단용으로 보존 중 |
| 디스크 동기화 | `DISK_CURRENT` | 디스크 `.blend`의 마지막 Snapshot과 현재 Runtime 내용이 같음 |
| 디스크 동기화 | `RUNTIME_DIRTY` | Import·편집·Undo·Redo·Export 결과가 아직 `.blend`에 저장되지 않음 |
| 디스크 동기화 | `NO_DISK_SNAPSHOT` | 최초 Import 뒤 아직 한 번도 Snapshot을 저장하지 않음 |

정상 Load 직후와 Save 직후는 `TEXT_RELEASED + DISK_CURRENT`다. Text 제거 자체 때문에 Blender가 파일을 수정 상태로 표시할 수 있지만 Add-on 관점에서 Runtime 데이터 저장이 필요한 상태는 아니다. Import·편집·Undo·Redo·Export 뒤에만 `RUNTIME_DIRTY`로 바뀌며, 다음 저장 요청은 Text 존재 여부와 관계없이 현재 Runtime으로 Snapshot을 재생성한다.

## 9. Modifier Snapshot 내용

### 9.1 불변 Baseline Section

- Camera Model과 Intrinsics
- Image ID, Camera ID, Pose와 원본 파일명
- Point2D 좌표와 Point3D ID
- Point3D ID, 좌표, 색상, Error와 Track
- Rig와 Frame
- Dataset UUID·Source Fingerprint·Schema
- Image Root 절대경로와 IMAGE_ID별 파일명

### 9.2 관계 Index Section

- Point3D ID → Image ID 목록
- Image ID → Point3D ID 목록
- Camera ID → Image ID 목록
- Image ID → Frame ID
- Offset 배열과 정렬된 Flat ID 배열

이 Section을 직접 복원하므로 재실행 시 `_RelationshipIndexBuildJob`을 실행하지 않는다.

### 9.3 편집·표시 Section

- Modifier Baseline Generation UUID
- SourceDescriptor 전체: Dataset·Sparse·Model·Images 절대경로, 입력 형식, Layout, 선택 파일명, Source Fingerprint, Dataset UUID와 Schema
- Modifier Edit Revision과 History Hash
- 마지막 저장 시점의 Point3D 결정 순서 또는 Baseline 순서에 적용할 결정적 Filter 정보
- Image Path Manifest 상태와 Validation Warning
- Pending Camera Filter와 Review 복구에 필요한 영구값

대용량 Baseline·Index를 포함한 새 Generation은 Blender 저장 시점에만 현재 Runtime으로 만든다. Import·Export·일반 Point·Image 편집 사이에는 Generation을 만들지 않는다. Undo 권위값은 기존 Scene History이며 Text에 Edit Operation을 별도의 두 번째 Undo Stack으로 중복 기록하지 않는다.

Load 성공 직후는 `TEXT_RELEASED + DISK_CURRENT`, 최초 Import 직후는 `TEXT_RELEASED + NO_DISK_SNAPSHOT`, 편집·Undo·Redo·Export 뒤는 `TEXT_RELEASED + RUNTIME_DIRTY`다. 편집 Operator마다 Text Generation을 만들지 않으며 Blender 저장 때 그 시점의 Runtime과 Scene History를 한 번 직렬화한다.

Snapshot Decoder가 만든 Camera·Image·Point3D·Rig·Frame Record는 불변이며 Runtime Mapping도 읽기 전용으로 설치한다. Modifier 편집은 기존 Mapping을 직접 수정하지 않고 새 Reconstruction·Index Revision을 만든다.

### 9.4 복원 순서

```text
Blender `load_post` 자동 복원 Job
→ Snapshot Manifest·Schema 확인
→ Baseline Section 증분 해제
→ Relationship Index Section 증분 해제
→ Edit Section과 History Hash 확인
→ 저장된 편집 상태 적용
→ Point Order 연결
→ Scene Token 재검증
→ Runtime Registry에 한 번만 설치
```

외부 COLMAP Source 경로가 없더라도 Snapshot이 정상이면 Modifier 편집·Review·Export를 사용할 수 있어야 한다.

편집과 Review는 외부 sparse 경로 없이 바로 사용할 수 있다. Export는 출력 목적지가 필요하므로 저장된 Model Directory를 읽을 수 없으면 과거 절대경로를 조용히 다시 만들지 않고 사용자가 새 Export Folder를 지정하게 한다. 새 위치 Export가 성공하면 메모리 Runtime의 SourceDescriptor와 외부 출력 기준선을 갱신하고 Snapshot은 다음 Blender 저장 때 만든다.

## 10. Align Snapshot 내용

### 10.1 Reconstruction Section

- Component별 Camera·Image·Point3D·Rig·Frame
- Point2D와 Track
- Component Source UUID·Fingerprint와 원본 파일명
- Merge Export에 필요한 원본 ID·관측 순서

### 10.2 Project State Section

- Model Catalog 순서와 선택 상태
- Active Model, 단일·복수 Selection, Selection Anchor
- Reference·Moving Model 지정
- 확정 Alignment Transform
- 3 Point Marker Pair
- ICP 확정 상태와 Runtime 호환 Version
- 표시 Sample 기준과 Point 표시 순서
- Axis와 Display 설정

### 10.3 복원 순서

```text
Blender `load_post` 자동 복원 Job
→ 기존 Project Text의 가벼운 Metadata 확인
→ Align Runtime Snapshot 증분 해제
→ Model Repository 구성
→ Alignment·Marker·선택 상태 적용
→ GPU 표시 데이터 준비
→ Align Runtime 활성화
```

`.blend` `load_post`는 Align·Modifier Snapshot 복원 Job을 자동 예약한다. 일반 Workspace를 장시간 막지 않도록 Chunk Decode는 Main Thread 시간 예산을 지키는 증분 Job으로 실행하지만, 해당 Workspace에 처음 들어갈 때까지 미루지는 않는다. 각 Tool Runtime 복원이 성공하는 즉시 해당 Tool의 Snapshot Text를 제거한다. 같은 Blender 실행에서 Workspace를 다시 들어오면 Runtime Cache를 재사용한다.

Align의 내장 범위는 COLMAP Component다. 현재 프로젝트에 OBJ가 포함되어 있으면 OBJ Geometry·UV·Material·Texture는 이번 Snapshot에 내장하지 않으므로 외부 OBJ Asset이 없을 때는 해당 Component만 `외부 Asset 필요` 상태로 남긴다. 계획서의 `외부 Component 없이 복원`은 COLMAP Component에만 적용한다.

Align Merge Export는 복원된 Model Repository의 Component Payload를 Worker 입력으로 전달해야 한다. Worker가 저장된 외부 sparse 경로를 다시 Hash·Parse하지 않으며, 재연결된 Images Folder가 필요한 작업에는 명시적 절대경로를 함께 전달한다.

## 11. Snapshot 생성·갱신 시점

### 11.1 최초 Import

```text
외부 Source 선택
→ 기존 전체 Import·무결성 검사
→ Runtime 생성
→ Runtime Registry 설치
→ TEXT_RELEASED·NO_DISK_SNAPSHOT
→ Import 완료
```

처음 보는 외부 데이터의 전체 검사는 유지한다. 최초 Snapshot은 Import 중 만들지 않고 사용자가 `.blend`를 저장할 때 생성한다.

### 11.2 Modifier 편집

- 불변 Baseline과 관계 Index Section은 재사용한다.
- Edit Revision, History와 활성 상태만 갱신한다.
- Ctrl+Z를 별도 Stack으로 바꾸지 않는다.
- 편집·Undo·Redo 뒤 디스크 동기화 상태를 `RUNTIME_DIRTY`로 표시한다.
- 여러 편집 사이에는 Snapshot을 다시 직렬화하지 않는다.

### 11.3 Modifier Export

Export는 외부 COLMAP Source를 다시 읽어 현재 Runtime을 검증하지 않는다. 다음 조건을 모두 만족하는 Runtime만 Export 입력으로 허용한다.

- Runtime의 Provenance Generation UUID와 마지막 복원·Import Token이 같다.
- 현재 Edit Revision과 History Hash가 Export 시작 Token에 포함된다.
- Runtime의 Camera·Image·Point3D·Rig·Frame·관측 Index가 Snapshot Schema 검증을 통과한 상태다.
- Export 입력 Reconstruction의 Mapping과 Record가 불변이며, 관리되는 모든 편집은 새 Runtime Mutation Revision을 만든다.
- Export가 진행되는 동안 사용자 입력을 막는 기존 경계 안에서 Token이 바뀌지 않는다.

Staging Writer는 별도 재검증 Job이 요구하던 증명값을 파일 작성과 동시에 만든다.

```text
Runtime Provenance·Edit Revision Token 고정
→ 출력 경로·권한·여유 공간·덮어쓰기 정책 확인
→ 기존 출력 Backup·Rollback 자료 준비
→ TXT 또는 BIN Staging 단일 Pass 작성
   ├─ 각 Record의 필수 필드·ID·수치·참조 의미 검사
   ├─ Camera·Image·Point3D·Rig·Frame·Point2D·Track·Sensor·Data ID 수량 집계
   ├─ Runtime 순회 중 Canonical Model Digest 계산
   ├─ 실제 출력 Byte를 전달하면서 파일별 SHA-256 계산
   └─ 파일별 작성 Byte 수 집계
→ Stream flush·close
→ fsync
→ Regular File·Link/Reparse·파일 Identity·크기·수량·Digest·시작 Token 재확인
→ Staging 파일을 최종 경로에 원자 Commit
→ Runtime·Scene SourceDescriptor 기준선 Commit
→ Edit Revision·History 초기화
→ TEXT_RELEASED·RUNTIME_DIRTY
→ Rollback 자료와 임시 파일 정리
```

단일 Pass Writer가 유지해야 하는 의미 검증은 현재 `tools/modifier/core/integrity.py`의 정상·오류 계약과 동등해야 한다.

- Mapping Key와 Record ID 일치
- Camera Model ID·이름·Parameter 수·유한값·양수 Focal Length
- Image Pose의 유한값과 Quaternion 유효성
- Point3D 수치·RGB·비어 있지 않은 Track
- Point2D ↔ Track의 양방향 일치, 중복 Track과 Point2D 범위
- Rig Sensor 중복·Camera 참조·Pose
- Frame의 Rig Sensor 소속·Data ID 중복·Image와 Camera 일치
- Image의 단일 Frame 소속과 Rig·Frame을 사용하는 모델의 전체 Image Coverage

이 검증은 Writer의 Camera→Image→Point3D→Rig→Frame 순회 중 보조 Set·Map을 유지해 수행한다. 검증을 별도 전체 Job으로 두 번 실행하지 않지만 의미 검증 항목을 줄이지 않는다.

Writer 완료 결과는 `ExportWriteAttestation`과 같은 불변 결과값으로 전달한다. 이 결과에는 최소한 다음 값이 포함되어야 한다.

- Export 형식과 Schema Version
- 시작 Runtime Provenance Token·Edit Revision·History Hash
- Camera·Image·Point3D·Rig·Frame·Point2D·Track·Sensor·Data ID 수량
- Canonical Model Digest
- 파일별 상대경로·Byte 수·SHA-256
- 출력 형식·Layout·상대 파일명·Byte 수·파일 SHA-256으로 계산한 새 Source Fingerprint
- Writer가 닫은 파일 수와 완료 상태

다음 기존 단계는 실행하지 않는다.

- 작성한 Staging을 Parser로 다시 읽는 `PARSE_STAGING`
- 재해석한 Reconstruction의 `VALIDATE_STAGING` 전체 검사
- Runtime과 Staging의 Record 전체를 다시 비교하는 `COMPARE_STAGING`
- 작성 완료 파일을 다시 읽는 `HASH_STAGING`

파일 Hash 자체는 제거하지 않는다. Writer가 실제 파일 Handle에 전달하는 Byte를 Hashing Binary Sink에도 동시에 전달하여 별도 파일 읽기 Pass만 제거한다. TXT도 문자열 Hash가 아니라 실제 UTF-8 출력 Byte를 Hash한다. Canonical Model Digest는 Runtime Record를 직렬화하는 동일 순회에서 계산하며, 기대 수량과 Writer 수량이 다르거나 시작 Token·Mutation Revision이 바뀌면 Commit 전에 중단한다.

재Parse한 Staging Model이 더 이상 없으므로 현재 `ExportResult.loaded` 의존을 그대로 둘 수 없다. 성공 결과는 다음 값으로 Blender Commit을 수행한다.

```text
ExportResult
├─ 기존 current_reconstruction 불변 참조
├─ ExportWriteAttestation
├─ Attestation에서 만든 새 SourceDescriptor·Source Fingerprint
├─ 새 Validation Token
├─ 새 Image Path Manifest
└─ Backup·교체·정리 결과
```

`export_completion.py`와 Modifier Dataset Adapter는 이 결과로 Runtime 기준선과 수동 Projection Painting 업데이트용 Source Metadata를 갱신한다. 새 SourceDescriptor를 만들기 위해 Staging을 다시 Parse하지 않는다.

Export 성공 뒤 현재 결과를 메모리 Runtime의 새 기준선으로 확정하고 디스크 동기화 상태를 `RUNTIME_DIRTY`로 둔다. Export 중에는 Snapshot Text를 만들지 않는다. 외부 파일 교체 뒤 Runtime·Scene Commit이 실패하면 Rollback 자료로 외부 파일을 복구하고 Export 전 Runtime 상태를 유지한다.

외부 sparse는 즉시 변경되지만 `.blend`의 Snapshot은 사용자가 Blender 파일을 저장해야 갱신된다. Export 완료 UI에 `외부 COLMAP Export가 완료되었습니다. 현재 상태를 유지하려면 .blend를 저장하십시오.`를 표시한다. 저장하지 않고 종료하면 외부 sparse는 새 값이고 `.blend`는 마지막 저장 Snapshot으로 돌아간다.

Staging 작성·Attestation 확인·Commit 중 하나라도 실패하면 최종 출력 경로를 부분 상태로 남기지 않고 기존 Rollback 계약으로 복원한다. 이 원자성 보장은 Blender 프로세스가 실행 중이고 예외를 처리할 수 있는 범위다. 여러 COLMAP 파일 교체 도중 프로세스 강제 종료·전원 손실까지 Crash-Atomic이라고 표현하지 않는다. 해당 수준이 필요하면 별도 디스크 Transaction Journal과 다음 실행 복구 기능을 추가해야 한다.

### 11.4 Align 변경

- Marker·Selection·Transform 변경은 Align Runtime Project State를 갱신하고 `RUNTIME_DIRTY`로 표시한다.
- 새 Component Import·제거·Merge 기준선 변경도 Runtime만 갱신하며 Snapshot은 Blender 저장 때 만든다.
- ICP 실행 중간값과 외부 Worker 임시 파일은 Snapshot에 넣지 않는다.

### 11.5 Blender 저장

정상 작업 중에는 Snapshot Text가 존재하지 않는다. Blender의 모든 저장 경로는 현재 Align·Modifier Runtime을 저장 직전에 한 번 직렬화해야 한다.

저장 대상 Tool의 상태는 다음처럼 처리한다.

- Runtime이 정상인 Tool: 현재 Runtime으로 새 저장용 Generation을 만든다.
- Snapshot 복원에 실패해 원본 Text를 보존 중인 Tool: 손상·비호환 Text를 수정하지 않고 그대로 통과시키며 새 Generation으로 승격하지 않는다.
- Runtime과 저장 대상 Snapshot이 모두 없는 Tool: Generation을 만들지 않는다.
- 정상 Runtime이 있는 Tool 중 하나라도 Generation 생성·검증에 실패하면 전체 Blender 저장을 시작하지 않는다.

```text
Blender Save 요청
→ Align·Modifier Runtime·Scene Token 고정
→ 새 Snapshot STAGING Chunk Streaming 생성
→ Section·Chunk·참조 무결성 검증
→ ACTIVE Manifest 승격
→ Blender 기본 Save 실행
→ save_post: 저장된 Generation 기록
→ 이번 저장에서 생성한 정상 Tool의 Manifest·Chunk Text 제거
→ TEXT_RELEASED·DISK_CURRENT 복귀
```

`save_pre`에서 Snapshot 생성이 실패하면 Snapshot 없는 `.blend`를 덮어쓰면 안 된다. 구현 단계 0에서 Blender 5.2의 `save_pre` 예외·취소 동작을 실제 GUI로 먼저 검증한다. Handler만으로 Save 중단을 보장할 수 없으면 Ctrl+S·메뉴 Save·Save As·자동 저장을 포함하는 검증된 Save Gateway를 구현해야 하며, 이 경계가 확보되지 않으면 메모리 우선 구조를 배포하지 않는다.

`save_post_fail`은 이번 저장에서 생성한 STAGING·ACTIVE Text만 현재 메모리에서 제거하고 Runtime을 유지해 사용자가 다시 저장할 수 있게 한다. Load 복원 실패 때문에 보존 중인 원본 Text는 제거하지 않는다. Save 성공 직후 Text 제거 때문에 Blender 자체가 다시 파일 수정 상태로 표시되는 것은 허용하되 Add-on의 디스크 동기화 상태는 `DISK_CURRENT`다. 사용자가 저장하지 않고 종료하면 방금 저장된 디스크 Snapshot에는 영향이 없다.

## 12. 원자적 Generation 교체

```text
1. 새 generation_uuid 발급
2. STAGING Section Text 생성
3. Section별 크기·Schema·SHA-256 검증
4. STAGING Manifest 생성
5. Scene Source·Revision Token 재검증
6. ACTIVE Manifest Pointer를 새 Generation으로 교체
7. 새 Generation의 state를 ACTIVE로 확정
8. Blender 기본 Save가 ACTIVE Generation을 파일에 기록
9. save_post에서 저장 성공 Generation 확인
10. 이번 저장에서 생성한 정상 Tool의 ACTIVE Manifest·Chunk Text 제거
11. Scene Snapshot Pointer를 TEXT_RELEASED·DISK_CURRENT로 전환
```

파일을 연 직후에는 디스크에서 읽힌 ACTIVE Generation이 메모리에 존재한다. 해당 Generation은 Runtime 복원과 검증이 모두 성공한 뒤 제거한다. 저장 준비 중에는 새 STAGING·ACTIVE Generation만 일시적으로 존재하며 Save 성공·실패 뒤 모두 메모리에서 제거한다. 단, 복원 실패로 보존한 원본 Text는 사용자가 재Import로 교체하기 전까지 이 정리 대상에서 제외한다.

일반 Modifier 편집·Undo·Redo는 메모리 Baseline과 기존 Scene History만 되돌린다. Import·Export처럼 Baseline 자체를 바꾸는 작업은 Blender Global Undo 대상이 아니며 Add-on Undo Domain을 명시적으로 새 기준선으로 초기화한다. `undo_post`는 Text Generation Pointer를 복원 대상으로 사용하지 않는다.

Load Decode 실패 시 디스크에서 읽힌 ACTIVE Text를 현재 메모리에서도 유지하고 명확한 오류를 표시한다. Save 준비 실패·취소·`save_post_fail`에서는 새 STAGING·ACTIVE Text만 제거하고 Runtime을 유지한다. Text Data Block의 삭제가 Undo로 자동 복구된다고 가정하지 않는다.

## 13. 이미지 절대경로·파일명 계약

### 13.1 저장값

```text
ImageFolderManifest
├─ absolute_images_root
├─ root_revision
├─ image_path_revision
├─ manifest_digest
├─ image_count
└─ entries[]
   ├─ image_id
   ├─ original_image_name
   └─ relink_file_name
```

- `absolute_images_root`는 사용자가 마지막으로 승인한 절대경로다.
- `original_image_name`은 COLMAP Reconstruction의 원래 값과 하위경로를 그대로 보존하며 Export 값 동등성에 사용한다.
- `relink_file_name`만 경로가 없는 Basename으로 저장해 새 Folder 검색 Key로 사용한다.
- 상대경로와 과거 상위 폴더 구조는 복구 기준으로 사용하지 않는다.
- 이미지 후보 선택은 파일명 일치만 사용하며 파일 내용 Hash를 반복 계산하지 않는다.
- 파일명 비교는 현재 OS의 파일명 대소문자 규칙을 반영하되 원문을 보존한다.

### 13.2 정상 연결

절대 Images Folder가 존재하면 폴더를 한 번 스캔해 파일명 Index를 만든다. IMAGE_ID마다 파일 시스템을 다시 검색하지 않는다.

```text
file_name → 정확히 한 파일
```

모든 필수 파일이 정확히 하나씩 존재하면 절대 파일 경로 Map을 Runtime에 연결한다.

### 13.3 경로를 읽지 못했을 때

도구 Panel에 다음 상태와 버튼을 표시한다.

```text
이미지 폴더를 찾을 수 없습니다.
593개 이미지 연결이 필요합니다.
[Images Folder 지정]
```

사용자가 폴더를 선택하면 선택한 폴더 전체를 한 번 순회해 파일명 Index를 만든다. 하위 폴더도 검색하지만 저장되는 연결 기준은 파일명뿐이다.

### 13.4 누락·중복 처리

| 결과 | 처리 |
| --- | --- |
| 파일명 1개 발견 | 해당 IMAGE_ID 후보로 승인 |
| 파일명 없음 | 누락 목록에 추가 |
| 같은 파일명 2개 이상 | 중복 충돌 목록에 추가 |
| 접근 권한 오류 | 읽지 못한 폴더·파일 표시 |

누락 또는 중복이 하나라도 있으면 새 절대경로를 저장하지 않는다. 임의의 첫 파일을 선택하지 않는다.

### 13.5 Commit

```text
새 Images Folder 스캔
→ 모든 IMAGE_ID 검증
→ 활성 Dataset·Runtime Token 재검증
→ 새 절대 Root와 IMAGE_ID Path Map·image_path_revision·manifest_digest 원자 교체
→ Camera Review·Stencil 연결 갱신
```

Projection Painting 재연결 성공 시 새 `image_path_revision`을 Frame Source 요청 Token과 Image Cache 소유권에 반영하고, 과거 Source Image·Stencil Source Cache만 폐기한다. Paint Target, Layer Texture, Undo와 `source_modifier_*` Snapshot Token은 바꾸지 않는다. 재연결 실패 시 기존 Paint Target, Layer, Texture와 기존 Snapshot을 그대로 유지한다.

## 14. 사용자 경험

### 14.1 최초 Import

```text
COLMAP Import
→ 원본 무결성 검사
→ Runtime 생성
→ 디스크 Snapshot 없음·저장 필요
→ 작업공간 준비 완료
```

진행 UI에는 다음 단계를 구분해 표시한다.

- `COLMAP 데이터 읽기`
- `Reconstruction 무결성 검사`
- `편집 관계 준비`

Snapshot 압축·저장은 최초 `.blend` 저장 진행 단계에서 수행한다.

### 14.2 Blender를 다시 열었을 때

일반 Workspace와 기존 Projection Painting Snapshot은 즉시 사용할 수 있다. `load_post`가 Align·Modifier Runtime 복원을 자동 시작하며, 일반 Workspace를 막지 않도록 Chunk 단위로 진행한다.

```text
Blender 파일 열기
→ Align·Modifier Snapshot 자동 복원
→ 저장된 편집 상태 연결
→ Runtime 원자 설치
→ 복원된 Snapshot Text 메모리 해제
→ 작업 준비 완료·디스크 Snapshot 최신
```

Text 제거 때문에 Blender 자체는 파일을 수정 상태로 표시할 수 있다. 그러나 Add-on은 이를 Runtime 미저장으로 표시하지 않는다. 사용자가 저장하지 않고 종료해도 디스크의 기존 `.blend`와 그 안의 마지막 Snapshot은 그대로 유지된다. 사용자가 다시 저장하면 현재 Runtime으로 Snapshot을 만들어 `.blend`에 기록한 뒤 메모리에서는 다시 제거한다.

다음 단계는 더 이상 나타나지 않는다.

- 외부 sparse 파일 검사
- 외부 Source SHA-256
- COLMAP TXT·BIN Parse
- Reconstruction 전체 관계 인덱스 재생성

### 14.3 `.blend`와 이미지 폴더를 이동했을 때

```text
새 위치에서 .blend 열기
→ 내장 Runtime 데이터는 정상 복원
→ 과거 Images Folder 절대경로 실패
→ 이미지 연결 필요 상태 표시
→ [Images Folder 지정]
→ 새 이미지 폴더 선택
→ 파일명 검사 완료
→ Camera Review·Stencil 복귀
```

Point 편집, Alignment, Layer와 기존 Texture는 이미지 재연결 전에도 유지한다. Camera Background와 Stencil처럼 실제 이미지가 필요한 기능만 비활성화한다.

### 14.4 Snapshot이 없거나 호환되지 않을 때

자동 이전하지 않고 상태를 명확하게 표시한다.

```text
이 프로젝트에는 현재 버전의 Runtime Snapshot이 없습니다.
원본 COLMAP 데이터를 다시 Import한 뒤 .blend를 저장하십시오.
```

Schema가 다를 때도 기존 Snapshot을 수정하거나 추측 복구하지 않는다.

### 14.5 원본 COLMAP을 새 버전으로 교체할 때

내장 Snapshot은 외부 Source 변경을 자동 감시하지 않는다. 사용자가 명시적으로 다시 Import한다.

```text
[COLMAP 원본 다시 불러오기]
→ 새 Source 전체 검증
→ 새 Runtime 설치
→ Runtime 변경됨·.blend 저장 필요
```

### 14.6 Modifier Export

사용자는 기존과 같이 Export 위치와 덮어쓰기 여부를 선택한다. Export가 시작되면 Runtime과 Edit Revision을 고정하고 다음 흐름만 본다.

```text
[COLMAP Export]
→ Runtime Provenance·편집 상태 확인
→ 출력 준비·Backup
→ COLMAP 파일 작성·동시 검증
→ 출력 Commit
→ 메모리 Runtime 기준선 갱신
→ Export 완료·.blend 저장 필요 안내
```

진행 UI에 `Staging 해석`, `Reconstruction 무결성 검사`, `Runtime과 Staging 비교`, `Staging 재Hash` 단계는 표시하지 않는다. 파일 작성 중에는 실제 Record 또는 Byte 진행률을 계속 갱신하고, Commit과 Runtime 기준선 갱신을 마지막 단계로 표시한다.

Runtime Provenance나 Revision Token이 유효하지 않으면 오래 걸리는 외부 원본 재검사를 몰래 실행하지 않는다. Export를 시작하지 않고 `현재 Runtime을 다시 불러오십시오.`라고 안내한다.

저장된 Model Directory를 읽을 수 없으면 `원본 경로 없음`을 표시하고 새 Export Folder를 선택하게 한다. 사용자가 승인하기 전에는 과거 절대경로를 생성하거나 파일을 쓰지 않는다.

## 15. 바뀌는 부분 간단 요약

| 항목 | 현재 | 변경 후 |
| --- | --- | --- |
| Modifier 재실행 | 외부 COLMAP 재읽기 | `.blend` 내장 Snapshot 복원 |
| Modifier 관계 Index | 재실행마다 전체 생성 | 저장된 Index 직접 복원 |
| Align 프로젝트 복원 | 외부 Component 재Import | Align 내장 Snapshot 복원 |
| Snapshot 복원 시점 | Workspace 진입 때 외부 복원 | `load_post`에서 Align·Modifier 자동 복원 |
| 작업 중 Snapshot Text 메모리 | 해당 없음 | 정상 복원·저장 종료 뒤 `0`, Runtime만 유지 |
| Snapshot 생성 시점 | 해당 없음 | Blender 저장 직전에만 현재 Runtime으로 생성 |
| Load·Save 직후 상태 | 외부 Runtime 기준 | Text 제거됨·디스크 Snapshot 최신 |
| Import·편집·Export 뒤 상태 | 외부 경로·Scene 상태 | Runtime 변경됨·.blend 저장 필요 |
| 저장하지 않고 종료 | 외부 경로 기준으로 다음 복원 | 디스크의 마지막 저장 Snapshot 유지, 실제 미저장 Runtime 변경만 폐기 |
| 외부 sparse 필요성 | 재실행에 필수 | 재Import·백업에만 필요 |
| 이미지 | 절대 파일 경로 저장 | 절대 Folder + IMAGE_ID별 파일명 저장 |
| COLMAP Image 이름 | 해석된 원래 이름 | 원래 이름은 보존하고 Basename만 재연결 Key로 추가 |
| 이미지 폴더 이동 | 연결 실패 | 사용자가 새 절대 Folder 지정 |
| 파일명 중복 | 경로가 달라 별도 처리 | 충돌로 중단하고 목록 표시 |
| Projection Painting 데이터 | 독립 Blender ID Snapshot | 구조 유지 |
| 기존 프로젝트 | 외부 Runtime 복원 | 자동 이전 없음·재Import 요구 |
| 외부 sparse 이동 | Modifier Runtime과 Export 모두 실패 | Runtime은 복원, Export 시 새 Folder 명시 선택 |
| Modifier Export 검증 | 작성 뒤 재Parse·전체 검사·전체 비교·재Hash | 작성 1회 중 형식·수량·Digest·Hash 검증 |
| Modifier Export 안전장치 | Staging·Backup·원자 Commit·Rollback | 그대로 유지 |
| `.blend` 용량 | 약 142MiB | 예상 약 357~457MiB |

## 16. UI 변경 계획

### 16.1 Align·Modifier 공통 표시

```text
Runtime Snapshot
├─ Runtime: 복원 중 / 준비 / 실패
├─ 디스크 Snapshot: 없음 / 마지막 저장 완료 / 호환되지 않음 / 손상
├─ 현재 메모리 Snapshot Text: 제거됨 / 저장 준비 중 / 오류 보존 중
├─ 저장 상태: 최신 / Runtime 변경됨 / 저장 중
├─ Snapshot Schema
├─ 생성 시각
├─ 내장 크기
├─ Camera·Image·Point3D 수
└─ [COLMAP 원본 다시 불러오기]
```

정상 상태에서는 `Runtime: 준비`, `디스크 Snapshot: 최신`만 한 줄로 보이고 상세 Metadata는 기본 접힘으로 둔다. Snapshot 수동 생성 버튼은 두지 않으며 Blender 저장 동작이 유일한 생성 경계다. Text 제거만으로 Blender가 수정 상태가 된 경우에는 Add-on이 `Runtime 변경됨`으로 잘못 표시하지 않는다.

### 16.2 이미지 상태

```text
Images
├─ 상태: 연결됨 / 폴더 없음 / 누락 / 중복
├─ 현재 절대경로
├─ 연결 수
└─ [Images Folder 지정]
```

오류 Report에는 다음을 구분한다.

- 폴더 자체를 찾지 못함
- 누락 파일명과 IMAGE_ID
- 중복 파일명과 후보 절대경로
- 권한 때문에 읽지 못한 파일

### 16.3 Modifier Export 진행 표시

```text
COLMAP Export
├─ Runtime Provenance·편집 Revision 확인
├─ 출력 준비·Backup
├─ COLMAP 파일 작성·동시 검증
├─ 출력 Commit
└─ Runtime 기준선 갱신·.blend 저장 필요
```

Blender 저장 진행 UI는 `Runtime 고정 → Snapshot 압축 → Text 기록 → Blender 저장 → 메모리 정리`를 구분한다. 저장 완료 뒤 Text 제거로 Blender 자체가 다시 수정 상태를 표시할 수 있지만 Add-on은 `디스크 Snapshot: 최신`으로 표시한다.

작성 단계는 실제 Writer의 처리 Record 또는 Byte를 기준으로 진행률을 갱신한다. 제거된 재Parse·전체 비교·재Hash 단계를 허위 진행률로 남기지 않는다.

## 17. 기존 데이터·마이그레이션 정책

`0.3.33` 이전 또는 Snapshot이 없는 `.blend`를 자동 변환하지 않는다. Align의 기존 Project Text Codec이 지원하는 v1·v2·v3 자동 Decode·상대경로 Fallback도 새 Runtime Snapshot 생성 경로에는 적용하지 않는다.

```text
기존 파일 열기
→ Snapshot NONE 표시
→ 외부 원본이 있으면 기존 방식으로 명시적 Import 가능
→ Import 성공 뒤 Runtime 설치·저장 필요 표시
→ 다음 Blender 저장 때 새 Snapshot 생성
```

Legacy Project Metadata는 삭제하지 않고 읽기 전용으로 식별해 사용자에게 재Import 대상을 보여줄 수 있다. 그러나 새 ACTIVE Runtime Snapshot으로 자동 승격하거나 새 Schema로 다시 쓰지 않는다.

구현·테스트 중 기존 Dataset 초기화가 필요하면 코드를 통해 자동 처리하지 않고 사용자에게 먼저 알린다. 기존 Scene Resource를 이름만으로 삭제하지 않는다.

## 18. 실패 처리

| 실패 | 처리 |
| --- | --- |
| Manifest 없음 | Snapshot NONE, 재Import 안내 |
| Schema 불일치 | INCOMPATIBLE, 자동 변환 금지 |
| Section 누락 | CORRUPT, Runtime 미설치 |
| SHA-256 불일치 | CORRUPT, 부분 결과 폐기 |
| 압축 해제 상한 초과 | 즉시 중단 |
| Dataset·Revision 변경 | Stale Job 폐기 |
| Load Snapshot 복원 실패 | Runtime 미설치, 원본 Manifest·Chunk Text 보존과 재Import 안내 |
| 한 Tool만 복원 실패한 상태에서 저장 | 실패 Tool의 원본 Text는 그대로 보존하고 정상 Tool만 새 Generation 생성, 실패 Text 자동 승격 금지 |
| Snapshot 저장 준비 취소·실패 | Blender 저장 중단, 일시 생성 Text 제거, Runtime 유지 |
| `save_post_fail` | 일시 생성 STAGING·ACTIVE Text 제거, Runtime 유지, 저장 재시도 안내 |
| Snapshot Text 제거 실패 | Runtime 유지, 잔존 Text와 메모리 미해제 경고, 소유 Generation만 재정리 |
| Save 경로 차단 불가 | 메모리 우선 구조 Release 중단, 검증된 Save Gateway 마련 전 배포 금지 |
| 이미지 Root 없음 | Runtime 유지, 이미지 기능만 비활성 |
| 이미지 누락·중복 | 새 Root 미Commit, 목록 표시 |
| Workspace 이탈 | 자동 복원 Job은 계속 실행, Tool Runtime 준비 전 UI만 대기 상태 표시 |
| Blender Undo·Redo | 오래된 Pointer Cache 폐기, Runtime History만 갱신, 실제 Runtime 차이가 있으면 `RUNTIME_DIRTY` 표시 |
| Export 시작 Token 불일치 | Export 미시작, Runtime 재Import 안내 |
| Export 중 Revision 변경 | Staging 폐기, 최종 경로 미Commit |
| Writer 수량·Digest 불일치 | Staging 폐기, 기존 출력 유지 |
| Export Commit 실패 | 기존 Backup·Rollback 계약으로 복원 |
| 외부 파일 Commit 뒤 Runtime·Scene Commit 실패 | 외부 파일 Rollback, Export 전 Runtime 유지 |
| 저장된 Export 경로 없음 | 새 Export Folder 지정 전 미실행 |
| 이미지 Path Revision 변경 | 과거 Source Image·Stencil Cache만 폐기 후 재연결 |

## 19. 구현 단계

### 단계 0. 현재 계약·성능 기준 고정

- Modifier 0.3.33 결과 수량·History·Export Hash를 Fixture로 고정한다.
- Align Component, Alignment, Marker와 Merge Export 결과를 고정한다.
- 실데이터 Source, Index, Point Order와 GPU 준비 시간을 분리 계측한다.
- Modifier 실데이터 Export의 `WRITE_STAGING`, `PARSE_STAGING`, `VALIDATE_STAGING`, `COMPARE_STAGING`, `HASH_STAGING`, `COMMIT` 시간·최대 메모리·Main Thread 최대 Tick을 형식별로 기록한다.
- 기존 `.blend` 크기와 메모리 최고치를 기록한다.
- Blender 5.2 실제 GUI에서 `save_pre` 예외가 Ctrl+S·메뉴 Save·Save As·자동 저장을 확실히 중단하는지 검증한다.
- Handler만으로 중단을 보장하지 못하면 모든 저장 진입점을 포괄하는 Save Gateway의 구현 가능성과 누락 경로를 먼저 검증한다.

완료 조건: 새 Snapshot 복원 결과를 비교할 권위 기준이 있고, Snapshot 생성 실패 상태로 `.blend`가 덮어써지지 않는 저장 경계가 실증되어야 한다. 이 경계를 확보하지 못하면 이후 구현을 Release하지 않는다.

### 단계 1. 공통 Chunk Store 구현

- 도구 Runtime을 모르는 Blender Text Chunk 저장소를 추가한다.
- 저장 직전의 STAGING·ACTIVE Generation, Section SHA-256과 원자 승격을 구현한다.
- 논리 Section을 최대 8MiB Base85 물리 Chunk Text로 분할하고 Streaming encode/decode한다.
- Tool Owner·영구 Scene/Project UUID·Generation Revision·Chunk 순서·총 Digest를 Manifest에 포함한다.
- 크기·레코드 수·압축 해제 상한을 검사한다.
- `pickle`과 실행 가능한 Payload를 허용하지 않는다.
- Tool별 복원 성공 뒤 소유 Manifest·Chunk Text만 제거하는 API를 구현한다.
- 저장 성공·실패·취소 뒤 일시 생성 Generation을 제거하고 Runtime-only 상태로 복귀시킨다.

완료 조건: 임의 Binary Section을 `.blend` 저장·재열기 뒤 동일 Byte로 복원하고, 복원 성공 및 저장 종료 뒤 현재 메모리의 인코딩된 Snapshot Text Byte가 `0`이어야 한다.

### 단계 2. Modifier Codec 구현

- Reconstruction, Image String Table, Track과 Rig·Frame Codec을 구현한다.
- Relationship Index와 Point Order Codec을 구현한다.
- 기존 Runtime 객체와 값 단위 동등성을 검사한다.
- SourceDescriptor 전체, Image Path Manifest·Warning과 불변 Runtime Mapping을 Codec 계약에 포함한다.
- Decoder에서 Reconstruction·Relationship Index·Point Order·Scene History Token의 상호 일치를 검사한다.
- Decoder가 Runtime 원자 설치를 완료한 뒤에만 Text 제거를 승인하는 결과 계약을 추가한다.
- Encoder는 Blender 저장 때 현재 Runtime 전체를 한 번 Streaming 직렬화하며 편집마다 Section을 갱신하지 않는다.

완료 조건: 외부 Source 없이 Modifier Runtime을 완전히 구성해야 한다.

### 단계 3. Modifier Lifecycle 연결

- Import는 `NO_DISK_SNAPSHOT` 또는 `RUNTIME_DIRTY`, Export는 `RUNTIME_DIRTY`로 표시하고 Snapshot Text를 만들지 않는다.
- `load_post`에서 `내장 Snapshot → 명시적 재Import 안내` 순서의 자동 증분 복원 Job을 예약한다.
- Snapshot 복원에서는 외부 Source Loader와 `_RelationshipIndexBuildJob`을 호출하지 않는다.
- Runtime 원자 설치가 성공하면 Modifier Manifest·Chunk Text를 즉시 제거한다. 실패하면 진단을 위해 그대로 둔다.
- Projection Painting Provider가 복원된 Modifier Runtime에서 동일 값을 제공하는지 확인한다.
- Export 입력을 Runtime Provenance Token·Edit Revision·History Hash로 고정한다.
- Export 입력 Mapping·Record를 불변으로 고정하고 Mutation Revision을 Commit 직전 재확인한다.
- Staging Writer가 Record 형식·수량·Canonical Digest·출력 Byte SHA-256을 한 번의 순회에서 계산하도록 바꾼다.
- 기존 전체 Integrity의 Point2D↔Track·Rig↔Frame 의미 검증을 Writer 내부 한 Pass로 이전한다.
- `PARSE_STAGING`, `VALIDATE_STAGING`, `COMPARE_STAGING`, 별도 `HASH_STAGING` 단계를 제거한다.
- Staging·Backup·원자 Commit·Rollback과 Export 뒤 메모리 Runtime 기준선 갱신은 유지한다.
- 새 SourceDescriptor·Validation Token·Image Manifest를 Attestation에서 만들고 `ExportResult.loaded`의 재Parse 의존을 제거한다.
- 기존 Model Directory가 없을 때 명시적 새 Export Folder를 받는 경로를 추가한다.
- Export 중 Snapshot Text를 생성하지 않고 완료 뒤 `.blend 저장 필요`를 안내한다.
- `save_pre`에서 현재 Modifier Runtime을 저장용 Generation으로 만들고 `save_post`·`save_post_fail`에서 제거한다.

완료 조건: Modifier 편집·Review·Projection Painting 수동 업데이트 결과가 기존과 같고, Export 출력은 기존과 값이 같으면서 중복 읽기 Pass 없이 원자 저장되어야 한다.

### 단계 4. Align Codec·Lifecycle 연결

- Component Reconstruction과 Project State Codec을 구현한다.
- Align Project Metadata와 대용량 Runtime Section을 분리한다.
- `load_post`에서 Align 자동 증분 복원을 예약하고 성공 뒤 Align 소유 Snapshot Text를 제거한다.
- Active·복수 Selection·Selection Anchor·Reference·Moving 상태를 저장·복원한다.
- Merge Worker가 외부 sparse를 재Hash·Parse하지 않고 Snapshot Component Payload를 입력받게 한다.
- OBJ Component는 외부 Asset 의존 상태를 분리 표시하고 COLMAP Snapshot 성공을 막지 않는다.
- Alignment·Marker·Selection·Merge Export를 기존과 비교한다.
- Align 변경은 Runtime Project State와 `RUNTIME_DIRTY`만 갱신하고 Snapshot은 Blender 저장 때 한 번 생성한다.

완료 조건: 외부 COLMAP Component 경로가 없어도 저장 당시 Align COLMAP 작업을 복원해야 한다.

### 단계 5. 이미지 폴더 재연결 구현

- IMAGE_ID별 파일명 Manifest와 절대 Root를 추가한다.
- 선택 폴더의 재귀 파일명 Index를 한 번 생성한다.
- 누락·중복·권한 오류를 구조화된 결과로 반환한다.
- 전체 성공 뒤에만 절대 Root와 Path Map을 Commit한다.
- 원래 COLMAP Image 이름과 Basename 재연결 Key를 분리하고 Path Revision·Manifest Digest를 갱신한다.
- Modifier Camera Review와 Projection Painting Camera·Stencil 연결을 갱신한다.
- Projection Painting Frame Source Token·Source Image Cache에 Path Revision을 반영한다.

완료 조건: `.blend`와 이미지 폴더를 옮긴 뒤 한 번의 Folder 지정으로 모든 이미지를 복구해야 한다.

### 단계 6. UI·진행률

- 자동 Snapshot 복원과 Blender 저장 시 Snapshot 생성 진행 UI를 추가한다.
- Snapshot 상태와 이미지 상태를 구분한다.
- 디스크 Snapshot 상태, Runtime 준비 상태, 현재 메모리 Text 제거 상태와 실제 Runtime 변경 여부를 구분한다.
- 이미지 연결이 없어도 가능한 편집·Alignment 기능은 유지한다.
- 이미지 의존 기능은 원인을 표시하고 비활성화한다.

완료 조건: 사용자가 Runtime 문제와 이미지 경로 문제를 혼동하지 않아야 한다.

### 단계 7. 회귀·실데이터·배포

- Core, Blender Background와 실제 GUI 검사를 실행한다.
- Snapshot 크기·저장 시 생성 시간·복원 시간·최대 Tick·Peak 메모리를 기록한다.
- 복원 성공·저장 종료 뒤 안정 상태의 Snapshot Text 메모리가 `0`인지 검사한다.
- Ctrl+S·메뉴 Save·Save As·자동 저장의 생성·기록·정리 계약을 실제 GUI에서 확인한다.
- 실제 이미지 폴더 이동, 누락, 중복과 Rollback을 검사한다.
- Production ZIP을 빌드하고 `user_default`에 설치한 뒤 설치본으로 다시 검사한다.
- 임시 계측과 디버그 로그를 제거한다.

완료 조건: 기능·성능·용량·설치본 승인 기준을 모두 통과해야 한다.

## 20. 예상 변경 파일

| 경로 | 변경 내용 |
| --- | --- |
| `shell/lifecycle.py` | 공통 Load·Save·Snapshot Text 정리 Handler와 Image Relink Handler 등록·해제 |
| `shell/save_gateway.py` | `save_pre`만으로 저장 중단이 불가능할 때 모든 저장 진입점을 보호하는 Gateway |
| `shared/blender_runtime/` | 도구 중립 Text Chunk Store·일시 Generation·메모리 제거 계약 |
| `shared/image_relink/` | 절대 Folder·파일명 Index·검증 Core |
| `shared/colmap_dataset/` | Modifier Source 전달 Metadata의 Snapshot·Path Revision 반영 |
| `tools/modifier/core/` | Reconstruction·Index·Point Order Snapshot Codec |
| `tools/modifier/blender/runtime_state.py` | 내장 Snapshot 자동 복원·Runtime 원자 설치·복원 Text 제거 |
| `tools/modifier/blender/state_handlers.py` | Load 자동 복원과 Save 직전 생성·성공·실패 정리 경계 |
| `tools/modifier/blender/undo_domain_coordinator.py` | Scene History Undo와 불변 Baseline Generation 경계 유지 |
| `tools/modifier/blender/dataset_adapter.py` | 복원·Export·재연결 결과를 수동 업데이트 Source Metadata에 반영 |
| `tools/modifier/blender/operators_import.py` | Import 뒤 Runtime 설치·`NO_DISK_SNAPSHOT` 또는 `RUNTIME_DIRTY` 연결 |
| `tools/modifier/blender/export_completion.py` | Export 뒤 Runtime 기준선 갱신·`RUNTIME_DIRTY` 연결 |
| `tools/modifier/core/export_job.py` | Export Phase를 단일 Writer 검증·Commit 흐름으로 변경 |
| `tools/modifier/core/export_staging_jobs.py` | 작성 중 수량·Digest·Byte Hash Attestation 생성 |
| `tools/modifier/core/export_transaction.py` | 동기 Export 경로도 동일한 단일 Pass 계약 사용 |
| `tools/modifier/core/export_types.py` | 불변 Export Attestation·Token 결과 형식 추가 |
| `tools/modifier/core/export_progress.py` | 제거·통합된 Export Phase와 진행률 범위 갱신 |
| `tools/modifier/core/export_integrity_job.py` | Export 경로의 중복 전체 재검증 의존 제거 |
| `tools/modifier/blender/operators_export_progress.py` | 실제 Writer Record·Byte 기반 진행 단계 연결 |
| `tools/modifier/blender/export_progress_session.py` | 새 Export Phase 표시와 취소·정리 계약 유지 |
| `tools/modifier/blender/panels.py` | Snapshot·Images 상태와 버튼 |
| `tools/align/project_persistence/` | Align Runtime Section Manifest 연결 |
| `tools/align/registration.py` | Load 자동 복원·복원 Text 제거·Save 직전 Snapshot 생성 연결 |
| `tools/align/model_repository.py` | 내장 Reconstruction 복원 진입점 |
| `tools/align/state.py` | Active·복수 Selection·Anchor·Reference·Moving 영속 상태 연결 |
| `tools/align/merge_export/types.py` | Snapshot Component Payload·Images Root Worker 계약 |
| `tools/align/merge_export/backend.py` | 외부 Source Path 대신 검증된 Payload 전달 |
| `tools/align/merge_export/worker.py` | 외부 sparse 재Hash·Parse 제거와 Payload 기반 Merge |
| `tools/align/merge_export/preflight.py` | 명시적 Images Folder와 Runtime Payload Token 검증 |
| `tools/align/ui/panels.py` | Snapshot·Images 상태와 버튼 |
| `tools/projection_paint/blender/camera_scene.py` | 재연결된 Image Path Map 적용 |
| `tools/projection_paint/blender/properties.py` | Image Path Revision·Manifest Digest 영속값 |
| `tools/projection_paint/blender/frame_source.py` | Path Revision 기반 Source Image Cache 무효화 |
| `tools/projection_paint/blender/stencil_session.py` | 재연결된 Source Revision의 Stencil 복구 |
| `tools/projection_paint/blender/state_handlers.py` | 재실행·Undo 뒤 Path Revision 재연결 |
| `tools/projection_paint/blender/panels.py` | Images Folder 지정 UI |
| `tests/shared/` | Chunk Store와 이미지 파일명 재연결 검사 |
| `tests/modifier/` | Snapshot 저장·재열기·편집·Export 회귀 |
| `tests/align/` | Component·Alignment·Marker·Merge 회귀 |
| `tests/projection_paint/` | Snapshot 독립성·Stencil 이미지 재연결 회귀 |
| `docs/` | 구현 현황·테스트·Release Note 갱신 |

구현 중 확인된 최소 파일만 수정하며 도구 간 직접 Runtime Import를 추가하지 않는다.

## 21. 테스트 계획

### 21.1 순수 Core

- TXT·BIN, Legacy·Modern Reconstruction의 encode/decode 동등성
- 비연속 64-bit ID, 빈 관측, Rig·Frame, UTF-8 파일명
- 관계 Index와 Point Order 동등성
- Section 순서 변경·누락·중복 거부
- Chunk 순서 변경·누락·중복·다른 Generation 혼합 거부
- Schema·dtype·shape·byte length 불일치 거부
- 압축 Payload 손상과 SHA-256 불일치 거부
- 압축 해제 상한과 과도한 record count 거부
- 같은 Runtime의 TXT·BIN Writer Attestation 수량·Canonical Digest 결정성
- Writer가 계산한 파일별 SHA-256과 테스트에서 독립적으로 다시 읽은 SHA-256 일치
- Legacy·Modern 출력의 Record 경계·비연속 ID·빈 관측·잘못된 참조 거부
- Point2D↔Track 양방향 불일치, 중복 Track, Rig Sensor, Frame Data ID·Coverage 오류 거부
- Short Write·flush·close·fsync 실패 시 Attestation 미발급
- 기존 Writer 출력과 단일 Pass Writer 출력의 Parser 결과 차등 비교
- 같은 파일명 0개·1개·2개 이상 재연결 결과
- Windows 대소문자·Unicode 파일명 충돌

### 21.2 Modifier Blender Background

- Import 완료 뒤 Runtime 설치·`NO_DISK_SNAPSHOT` 또는 `RUNTIME_DIRTY`·Snapshot Text `0`
- Blender 저장 직전에만 저장용 Generation 생성
- 저장 성공·실패·취소 뒤 Snapshot Text `0`과 Runtime 유지
- `.blend` 저장·재열기 뒤 외부 Source Read·Hash·Parse 0회
- `_RelationshipIndexBuildJob` 생성 0회
- `load_post` 자동 복원 성공 뒤 Modifier Manifest·Chunk Text `0`
- Schema 불일치·Chunk 손상·Decode 실패 때 Runtime 미설치와 원본 Snapshot Text 보존
- Camera·Image·Point3D·Rig·Frame·Index·Point Order 동등성
- 편집 없음·Point 제거·Image 제거·혼합 History 복원
- Export 결과와 재열기 결과 동등성
- TXT·BIN Export 결과의 Camera·Image·Point3D·Rig·Frame·Point2D·Track 값 동등성
- Export 1회당 Staging Writer 1회, Staging Parser 0회, 전체 Integrity Job 0회, Runtime 전체 비교 Job 0회
- 작성 완료 파일의 별도 Hash 읽기 0회와 Writer 산출 SHA-256 일치
- Runtime Provenance Token·Edit Revision·History Hash 불일치 시 Commit 전 중단
- 동일 Reconstruction 객체·동일 Record 수에서 Record 또는 Mapping 변경 시 Mutation Revision·불변 Mapping 경계로 차단
- Writer Record 수·Canonical Digest·파일 Byte 수 불일치 시 기존 출력 유지
- Writer Record별 실패, 덮어쓰기·Backup·N번째 Commit·Obsolete 삭제 실패별 Rollback과 부분 출력 부재
- 외부 파일 Commit 뒤 Runtime·Scene Commit 실패 시 외부 Rollback과 Export 전 Runtime 유지
- 원본 Model Directory 삭제 뒤 새 Export Folder 미선택·선택 성공 경로
- 저장용 Snapshot 생성 실패 시 Blender 저장 중단·일시 Text 제거·Runtime 유지
- Undo·Redo 뒤 Runtime History 갱신·Snapshot Text `0`·디스크 동기화 상태 정확성
- Projection Painting Provider 결과 불변

### 21.3 Align Blender Background

- Component 1개·복수 Component 복원
- Alignment Transform·Reference·Moving 선택 복원
- 3 Point Marker와 ICP 확정 상태 복원
- 외부 COLMAP 경로 제거 뒤 내장 Snapshot 복원
- `load_post` 자동 복원 성공 뒤 Align Manifest·Chunk Text `0`
- Align Snapshot 복원 실패 때 원본 Text 보존과 Runtime 미설치
- Merge Export 결과 Camera·Image·Point3D·Track 동등성
- 내장 Component Payload만 전달했을 때 Merge Worker의 외부 sparse Read·Hash·Parse 0회
- Active·복수 Selection·Selection Anchor·Reference·Moving 저장·복원
- OBJ 포함 프로젝트에서 COLMAP은 복원되고 누락 OBJ만 외부 Asset 필요 상태로 분리
- Workspace 이탈·재진입 Runtime WARM Cache와 Snapshot 재Decode `0회`
- Snapshot 손상 시 Project Metadata 보존과 명확한 오류

### 21.4 이미지 재연결

- 저장 Root 정상 연결
- 저장 Root 삭제 후 새 절대 Folder 지정
- 파일 순서가 달라도 파일명으로 정확히 연결
- 하위 폴더에 있는 파일 검색
- 누락 1개·복수 누락 목록
- 같은 파일명 중복 후보 목록과 미Commit
- 검사 도중 Dataset Revision 변경 Rollback
- 성공 뒤 Modifier Camera Review 복귀
- 성공 뒤 Projection Painting Camera Background·Stencil 복귀
- Image Path Revision 변경 뒤 이전 Frame Source Image·Stencil Source Cache 폐기
- 실패 뒤 Paint Target·Layer·Texture 불변

### 21.5 실제 GUI

```text
1. 실제 Modifier·Align 데이터를 Import하고 Snapshot Text가 없는지 확인
2. Ctrl+S 저장 중 Snapshot 생성 진행률과 저장 성공을 확인
3. save_post 뒤 현재 메모리의 Align·Modifier Snapshot Text가 모두 제거됐는지 확인
4. Text 제거로 Blender가 수정 상태여도 Add-on은 디스크 Snapshot 최신으로 표시하는지 확인하고, 저장하지 않고 종료한 뒤 마지막 Snapshot이 정상인지 확인
5. .blend 재실행 뒤 load_post 자동 복원과 외부 sparse Read·Hash·Parse 0회를 확인
6. 복원 성공 뒤 Snapshot Text 제거·Runtime 유지·디스크 Snapshot 최신 표시를 확인
7. 외부 sparse 폴더 이름을 임시 변경하고 Modifier 편집·Review·Align 작업을 확인
8. 원본 Model Directory가 없는 상태에서 새 Export Folder를 지정해 Export한다
9. Export 뒤 Snapshot Text는 없고 .blend 저장 필요 안내가 뜨는지 확인
10. 저장하지 않고 복사본을 종료해 디스크 .blend는 마지막 저장 Runtime으로 돌아가는지 확인
11. 다시 Export한 뒤 메뉴 Save·Save As에서도 Snapshot 생성·기록·정리가 같은지 확인
12. 저장 준비 실패와 save_post_fail을 강제로 발생시켜 Snapshot 없는 .blend 덮어쓰기 방지와 일시 Text 정리를 확인
13. 자동 저장 경로도 동일한 저장 계약을 통과하는지 확인
14. 손상 Snapshot 복사본을 열어 Runtime이 설치되지 않고 원본 Text가 보존되는지 확인
15. images 폴더를 새 위치로 옮기고 .blend를 재실행한다
16. Runtime은 복원되고 이미지 연결 경고만 뜨는지 확인
17. Images Folder를 지정해 593개 이미지가 파일명으로 연결되는지 확인
18. Camera Review·Stencil·Frame Source를 확인
19. 중복 파일을 추가하고 재연결 실패·목록·Rollback을 확인
20. 원본 폴더 이름과 테스트 파일을 복구
```

실제 원본 `.blend`는 저장하지 않고 복사본으로 검사한다.

## 22. 성능·용량 승인 기준

- Modifier COLD 복원 외부 Source Read·Hash·Parse: `0회`
- Modifier COLD 복원 관계 Index 생성: `0회`
- Align COLD 복원 외부 Component Import: `0회`
- 동일 실행 WARM 재진입 Snapshot Decode: `0회`
- Modifier 실데이터 COLD 복원: 기존 121.57초 대비 최소 70% 단축
- Modifier 실데이터 COLD 복원 목표: 30초 이하
- 최대 Main Thread 처리 Tick: 목표 50ms 이하
- Snapshot 생성·복원 중 진행 UI 갱신과 취소 가능
- 복원 성공 뒤 안정 상태의 인코딩된 Snapshot Text 메모리: `0Byte`
- Blender 저장 성공·실패·취소 뒤 정상 Tool의 인코딩된 Snapshot Text 메모리: `0Byte`이며 복원 실패 원본 보존은 예외
- Snapshot Peak 메모리는 Load Decode와 Save Encode 구간으로 한정하고 Tool별·합계 Peak를 기록
- 저장 시 현재 Runtime 전체 Snapshot 생성 시간과 Blender 기본 저장 시간을 분리 기록
- Ctrl+S·메뉴 Save·Save As·자동 저장의 Snapshot 누락 저장: `0회`
- Snapshot 물리 Text Chunk: Base85 인코딩 기준 Chunk당 `8MiB 이하`
- Camera·Image·Point3D·Rig·Frame·관측·Index 수량 완전 일치
- Modifier Edit Revision·History Hash·Export Hash 완전 일치
- Modifier Export Staging 작성 Pass: `1회`
- Modifier Export Staging 재Parse·전체 Integrity·Runtime 전체 비교: 각각 `0회`
- Modifier Export 작성 완료 파일 재Hash 읽기: `0회`
- Modifier Export 단일 Pass 결과와 기존 Export 결과의 값 동등성: `100%`
- Modifier Export 시간은 동일 실데이터·동일 디스크·동일 형식에서 기존 단계별 Benchmark와 비교 기록
- Align Merge Export 결과 Hash 또는 값 동등성
- 현재 실데이터 내장 증가 목표: 315MiB 이하
- Snapshot 복원 최고 메모리는 기존 전체 Runtime 복원 대비 1.5배 이하
- Snapshot Generation 교체·Export의 임시 Staging·Rollback·Backup 동시 Peak Disk 사용량을 별도 기록

## 23. 위험과 대응

| 위험 | 대응 |
| --- | --- |
| `.blend` 용량 증가 | Section 압축·자료형 최적화·실제 크기 상한 검사 |
| 단일 대용량 Text 작성 병목 | 논리 Section을 8MiB 이하 물리 Text Chunk로 분할·Streaming 처리 |
| 저장 시간이 길어짐 | 모든 저장에서 현재 Runtime 전체 Snapshot을 한 번 생성하므로 단계별 진행률·Streaming·실데이터 Benchmark와 승인 상한을 적용 |
| Text Encoding 오버헤드 | Load·Save 중에만 Base85 Buffer를 유지하고 Chunk 완료 즉시 해제, 안정 상태 Text 메모리 `0` 검증 |
| 파일 손상 | Section SHA-256·크기·Schema 검증, 복원 실패 시 디스크에서 읽힌 원본 Text 보존 |
| Text 제거와 실제 미저장 편집 혼동 | `TEXT_RELEASED`와 `DISK_CURRENT`·`RUNTIME_DIRTY`를 분리하고, Load·Save 직후에는 디스크 최신으로 표시 |
| Save 직전 Snapshot 생성 실패 | Snapshot 없는 파일 덮어쓰기를 금지하고 검증된 Handler 중단 또는 Save Gateway로 모든 저장 진입점 차단 |
| Save 성공·실패 뒤 Text 제거 실패 | Tool Owner·Generation UUID로 소유 Text만 재정리하고 잔존 메모리 경고 표시 |
| 악성 압축 Payload | 압축 해제 크기·record count 상한, `pickle` 금지 |
| Add-on 업데이트 호환 | Schema 불일치 시 자동 이전 금지·명시적 재Import |
| 이미지 파일명 중복 | 자동 선택 금지·후보 전체 표시 |
| 이미지 누락 때문에 전체 Runtime 차단 | Runtime과 이미지 상태 분리, 이미지 의존 기능만 비활성 |
| Source가 바뀌었는데 내장 Snapshot이 오래됨 | 자동 감시하지 않고 명시적 `원본 다시 불러오기`만 기준선 교체 |
| Projection Painting 독립성 훼손 | 기존 Blender ID Snapshot 유지, 수동 업데이트 계약 유지 |
| 공용 Dataset을 Runtime 공유로 오용 | Modifier Source Metadata 전달과 수동 Snapshot 생성에만 사용, Projection Painting 영구 Pointer 금지 |
| Align·Modifier 결합 | 도구별 Codec·Manifest·Generation 분리 |
| Undo 때 대용량 재직렬화 | Undo·Redo는 Runtime History만 갱신하고 Snapshot은 Blender 저장 때만 생성 |
| Writer 결함을 재Parse가 더 이상 잡지 못함 | Writer 순회 중 형식·범위·참조 검사, 수량·Canonical Digest·Byte Hash Attestation, 기존 Export와 차등 회귀 |
| Export 도중 Runtime 변경 | 시작 Runtime Provenance·Edit·History Token 고정, Commit 직전 재확인 |
| 단일 Pass 뒤 부분 파일 Commit | Staging·Backup·원자 교체·Rollback 계약 유지 |
| Export 뒤 외부 파일과 `.blend` 상태 차이 | Export는 외부 파일·Runtime만 Commit하고 `.blend 저장 필요`를 명시, 저장하지 않으면 마지막 디스크 Snapshot으로 돌아감을 안내 |
| 프로세스 강제 종료를 원자 저장으로 오인 | 처리 가능한 예외까지만 보장하고 Crash-Atomic은 별도 Journal 없이는 주장하지 않음 |
| Align Worker의 외부 sparse 재읽기 | Snapshot Component Payload를 Worker 입력으로 전달 |
| OBJ까지 내장됐다고 오인 | 이번 범위를 COLMAP Component로 명시하고 OBJ는 외부 Asset 상태를 별도 표시 |
| 이미지 재연결 뒤 과거 Cache 유지 | Path Revision·Manifest Digest로 Source Image·Stencil Cache만 무효화 |

## 24. 완료 조건

1. 최초 Import의 전체 원본 검증은 유지된다.
2. Align과 Modifier의 완성 Runtime 복원 입력이 `.blend`에 저장된다.
3. Snapshot이 정상일 때 외부 sparse 경로가 없어도 Runtime을 복원한다.
4. Modifier 관계 인덱스와 Point 순서를 재계산하지 않는다.
5. Modifier 편집·Undo·Review·Export 결과가 기존과 같다.
6. Align Component·Transform·Marker·ICP·Merge 결과가 기존과 같다.
7. 이미지는 Snapshot에 내장하지 않는다.
8. 이미지 절대 Root가 끊기면 사용자가 새 Folder를 지정할 수 있다.
9. IMAGE_ID별 파일명 연결에 누락·중복이 있으면 원자 Commit하지 않는다.
10. 상대경로를 저장·복구 계약으로 사용하지 않는다.
11. Projection Painting의 Snapshot·Paint·Undo 독립성이 유지된다.
12. 기존 프로젝트와 다른 Schema를 자동 마이그레이션하지 않는다.
13. Pure Python, Blender Background와 실제 GUI 회귀를 통과한다.
14. 실데이터 성능·용량·메모리 승인 기준을 통과한다.
15. Production ZIP 빌드, `user_default` 설치와 설치본 검증을 완료한다.
16. 제품 코드에 임시 계측·디버그 로그가 남지 않는다.
17. Modifier Export는 Staging을 한 번만 작성하고 재Parse·전체 재검증·전체 비교·사후 재Hash를 수행하지 않는다.
18. Modifier Export의 수량·Digest·Writer Hash 검증과 Staging·Backup·원자 Commit·Rollback이 유지된다.
19. 단일 Pass Writer가 기존 Reconstruction 의미 무결성 검사를 모두 보존한다.
20. Export 성공 결과는 Staging 재Parse 없이 새 SourceDescriptor·Validation Token·Image Manifest를 구성한다.
21. Snapshot Section은 8MiB 이하 물리 Text Chunk로 저장되고 Chunk 누락·혼합을 거부한다.
22. 일반 Undo·Redo는 Runtime History만 갱신하며 삭제된 Snapshot Text를 다시 가리키지 않는다.
23. Align Merge Worker는 Snapshot Component Payload를 사용하고 외부 sparse를 다시 Parse하지 않는다.
24. Projection Painting 수동 업데이트 독립성과 Image Path Revision 기반 Cache 무효화가 유지된다.
25. Import·편집·Undo·Redo·Export 중에는 Snapshot Text를 생성하지 않고 `NO_DISK_SNAPSHOT` 또는 `RUNTIME_DIRTY`만 갱신한다.
26. Blender 저장 직전에 현재 Align·Modifier Runtime 전체를 한 번 Snapshot으로 생성한다.
27. 정상 Tool은 복원 성공과 저장 성공·실패·취소 뒤 현재 메모리의 인코딩된 Snapshot Text가 `0Byte`다.
28. 복원 실패한 Snapshot Text는 진단과 재Import 판단을 위해 제거하지 않는다.
29. Ctrl+S·메뉴 Save·Save As·자동 저장에서 Snapshot 생성 실패 상태로 `.blend`가 덮어써지지 않는다.
30. 저장하지 않고 종료하면 디스크의 마지막 저장 Snapshot은 유지되고 미저장 Runtime 변경만 폐기된다.

## 25. 최종 사용자 흐름

```text
최초 Import
→ 전체 원본 검증
→ Runtime 생성
→ 디스크 Snapshot 없음·저장 필요

Blender 저장
→ 현재 Align·Modifier Runtime 고정
→ Snapshot Chunk Streaming 생성·검증
→ .blend에 기록
→ save_post에서 현재 메모리 Snapshot Text 제거
→ Runtime-only·디스크 Snapshot 최신

Blender 재실행
→ load_post에서 Align·Modifier 내장 Snapshot 자동 복원
→ Runtime 원자 설치
→ 복원된 Snapshot Text 즉시 제거
→ 외부 COLMAP 재Import 없이 작업 재개

저장하지 않고 종료
→ 디스크의 마지막 저장 Snapshot은 유지
→ 현재 실행에서의 미저장 Runtime 편집은 폐기

Modifier Export
→ Runtime Provenance·편집 Revision 확인
→ Staging 파일 단일 Pass 작성·동시 검증
→ Backup·원자 Commit
→ 현재 Runtime을 새 외부 출력 기준선으로 확정
→ Snapshot 저장 필요 안내

저장된 Model Directory 없음
→ 새 Export Folder 지정
→ 사용자가 승인한 절대경로에 Export
→ 새 SourceDescriptor·Runtime 기준선 확정
→ 다음 Blender 저장 때 Snapshot 반영

프로젝트 이동
→ .blend와 images 폴더 이동
→ .blend 실행
→ 내장 Runtime 정상 복원
→ Images Folder 경로 오류 표시
→ 사용자가 새 절대 Images Folder 지정
→ 파일명 기준 전체 연결
→ Camera Review·Stencil 복귀

새 COLMAP 원본 사용
→ 사용자가 원본 다시 불러오기 실행
→ 전체 검증
→ 새 Runtime 원자 설치
→ 다음 Blender 저장 때 새 Snapshot 기록
```

이 구조의 핵심은 외부 COLMAP 파일을 단순히 `.blend`에 복사하는 것이 아니다. 이미 해석·검증된 Reconstruction과 Modifier 관계 인덱스, Align 작업 상태를 복원 가능한 Section으로 저장해 재실행 비용을 제거하는 것이다.
