# 독립 도구 안내

통합 Extension은 아래 세 기능을 하나의 처리 파이프라인으로 연결하지 않습니다.

| 도구 | Source 경계 | Blender 진입점 | Scene 속성 |
|---|---|---|---|
| COLMAP Align | `tools/align/` | `colmap_align.open_workspace` | `colmap_align_settings` |
| COLMAP Modifier | `tools/modifier/` | `colmap.open_modifier_workspace` | `colmap_import` |
| Projection Painting | `tools/projection_paint/` | `projection_paint.open_workspace` | `projection_paint` |

각 도구는 자신의 등록·해제와 실행 상태를 소유합니다. Root Shell은 설치 단위, 공통 메뉴와 등록 순서만 관리합니다.
