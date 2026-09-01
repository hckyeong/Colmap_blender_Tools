# ADR-0008: Projection Painting 수동 독립 Snapshot

- 상태: 채택
- 결정일: 2026-08-29

## 결정

Modifier는 COLMAP Import·편집·Export의 권위 데이터를 소유합니다. Projection Painting은 `COLMAP 데이터 업데이트`를 실행한 시점에만 `shared/colmap_transfer`의 불변 값을 받아 자기 Camera·Point·Axis·Manifest를 새 UUID로 생성합니다.

- Modifier와 PP는 Blender Object, Camera Data, Mesh와 표시 설정을 공유하지 않습니다.
- 재업데이트 전까지 Modifier 편집·Undo는 PP Snapshot을 변경하지 않습니다.
- Paint Target, Layer, Texture, Mask, Adjustment와 Composite는 Snapshot 교체 대상에서 제외합니다.
- 업데이트는 연결 전 Staging, Source token 재검증, Commit과 실패 Rollback을 사용합니다.
- 별도 Ctrl+Z Stack은 만들지 않고 비활성 도구의 무거운 Undo 복구만 지연합니다.
- Align은 계속 완전 독립입니다.

## Legacy

`shared/colmap_dataset`과 `Scene.colmap_dataset`은 기존 `0.2.0` 파일을 읽기 위한 fail-closed 입력으로 보존합니다. 신규 PP 실행 경로는 자동 Binding이나 Display Listener를 등록하지 않으며 소유권이 불명확한 기존 데이터는 자동 삭제하지 않습니다.
