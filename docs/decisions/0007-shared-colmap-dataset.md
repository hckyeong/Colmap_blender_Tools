# ADR-0007: Modifier·Projection Painting 공용 Dataset

- 상태: ADR-0008로 대체
- 결정일: 2026-08-29

## 결정

Align은 기존처럼 데이터와 Runtime을 완전히 독립적으로 유지한다. Modifier와 Projection Painting은 도구 세션을 합치지 않고 같은 Scene의 중립 공용 COLMAP Dataset만 공유한다.

- Modifier만 COLMAP 데이터를 Import하고 확정 편집을 공용 Dataset에 반영한다.
- Projection Painting은 공용 Snapshot과 Blender Resource를 읽기 전용으로 사용한다.
- 두 도구는 서로의 패키지를 직접 Import하지 않고 `shared/colmap_dataset` 계약만 사용한다.
- Point·Camera Blender Resource는 한 벌만 만들고 두 전용 View Layer에서 공유한다.
- Marker, Runtime, Undo, Paint Target, Layer, Texture와 Stencil은 도구별 소유권을 유지한다.
- 기존 Projection Painting 자원은 자동 삭제하지 않고 Legacy 상태로 보존한다.

## 이유

같은 COLMAP 데이터를 두 번 Import하면 Camera Object, Frame Marker와 표시 설정이 중복되어 충돌할 수 있다. 중립 Dataset을 한 번만 생성하면 Modifier의 확정 편집 결과를 Projection Painting이 재진입할 때 그대로 읽을 수 있고, 도구별 세션 종료와 Undo 경계도 유지할 수 있다.

## 결과

Projection Painting은 공용 Dataset이 없으면 Modifier에서 먼저 Import하라는 상태를 표시하고 투영 기능을 비활성화한다. 일반 Blender Workspace와 Align에서는 공용 Dataset Root를 숨기며, Modifier와 Projection Painting Workspace에 들어갈 때 각각의 전용 View Layer에서 표시한다.

## 대체 결정

실시간 공용 Blender Resource와 Undo 복구 결합이 확인되어 2026-08-29 ADR-0008의 수동 독립 Snapshot 구조로 대체했습니다. 이 문서는 `0.2.0` 파일 마이그레이션 근거로만 보존합니다.
