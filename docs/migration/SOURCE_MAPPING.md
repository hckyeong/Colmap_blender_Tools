# 원본 Source 매핑

원본 프로젝트는 비교와 회귀 확인을 위해 그대로 보존하고, 통합 Runtime Source는 아래 위치에 복사했습니다.

| 원본 프로젝트 | 통합 Runtime Source | 통합 테스트 |
|---|---|---|
| `COLMAP_Align/src/colmap_align/` | `tools/align/` | `tests/align/` |
| `COLMAP_Modifier/`의 패키지 Source | `tools/modifier/` | `tests/modifier/` |
| `COLMAP_Projection_Paint/`의 패키지 Source | `tools/projection_paint/` | `tests/projection_paint/` |

Align의 기존 `.small-gicp-runtime/python/`은 Source로 복사하지 않습니다. 기본 통합 빌드는 이 런타임을 읽기 전용 입력으로 사용해 격리 Staging의 `tools/align/runtime/python/`에만 복사합니다. 기존 런타임이 없을 때는 통합 Root의 Git 제외 캐시에 같은 고정 버전 계약으로 새로 준비합니다.

원본 폴더의 중첩 Git 저장소, 브랜치와 미커밋 변경은 수정하지 않습니다. 이후 기능 변경은 통합 Runtime Source를 기준으로 수행하고, 원본 폴더는 통합이 안정화될 때까지 읽기 전용 비교 자료로 취급합니다.
