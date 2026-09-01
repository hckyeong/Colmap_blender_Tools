# 도구 경계 안내

통합 Extension은 세 기능을 순차 처리 파이프라인으로 묶지 않습니다. Align은 독립적이며 Modifier와 Projection Painting도 독립 Resource를 소유합니다. PP는 사용자가 요청할 때만 Modifier 값을 복사합니다.

| 도구 | Source 경계 | Blender 진입점 | 데이터 역할 |
|---|---|---|---|
| COLMAP Align | `tools/align/` | `colmap_align.open_workspace` | 독립 데이터 |
| COLMAP Modifier | `tools/modifier/` | `colmap.open_modifier_workspace` | 권위 COLMAP Import·편집·Export |
| Projection Painting | `tools/projection_paint/` | `projection_paint.open_workspace` | 수동 복사한 독립 Snapshot·Paint |

각 도구는 자신의 등록·해제, Workspace와 실행 상태를 소유합니다. `shared/colmap_transfer`는 Blender ID 없는 값 전달만 제공하며 `shared/colmap_dataset`은 기존 파일 마이그레이션 입력으로 유지됩니다.
