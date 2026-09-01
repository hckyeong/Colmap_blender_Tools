# COLMAP Tools 0.1.20 표시 파라미터 Workspace 복구 Release Note

## 원인

Modifier는 Import 이후 원본 COLMAP 파일이 바뀌면 Viewport Runtime 재구성 단계에서 검증 오류가 발생했습니다. 이 오류가 Workspace 설정 Timer 경계 밖으로 전달되면서 세션이 `ENTERING`에 멈추고 Modifier 패널이 사라졌습니다. 이때 모든 Workspace에 노출되던 Projection Paint 패널을 조작하면 값은 숨겨진 Paint 데이터에 적용되어 현재 Modifier 화면에서는 변화가 보이지 않았습니다.

Projection Paint는 소유 COLMAP Collection의 View Layer 눈 표시가 꺼진 상태를 재진입 시 복원하지 않았습니다. 또한 Point Scale은 Geometry Nodes 입력값만 변경하고 Object 데이터 재평가와 3D Viewport 다시 그리기를 요청하지 않아 표시 갱신이 다음 의존성 그래프 갱신까지 지연되었습니다.

## 변경

- Modifier 원본 검증 실패는 편집 Runtime 복원 경고로 보존하고 Workspace 활성화와 Point·Camera 표시 기능은 유지합니다.
- Modifier Workspace 구성 중 발생한 `ValueError`도 정식 실패 경로로 처리해 `ENTERING` 고착을 막습니다.
- Projection Paint 패널은 해당 Scene의 Projection Paint Workspace에서만 표시합니다.
- Projection Paint 재진입 시 현재 데이터셋의 소유 COLMAP Collection에 설정된 전역·View Layer 숨김과 제외 상태를 해제합니다.
- Projection Paint Point Scale 변경 뒤 관리 Point Object의 Geometry 재평가와 열린 3D Viewport 다시 그리기를 요청합니다.

## 검증

- MCP로 Modifier의 Scene 속성, Geometry Nodes 입력과 593개 Camera Data 값이 함께 바뀌는 것을 확인했습니다.
- MCP로 원본 변경 검증 오류가 Modifier 세션을 `ENTERING`에 고착시키는 재현 경로를 확인했습니다.
- MCP로 Projection Paint 소유 Collection의 `hide_viewport`가 켜져 Point와 Camera가 모두 가려지는 상태를 확인했습니다.
- 숨김 해제 뒤 Camera Frustum Size 변경이 즉시 보이고, Point Object `update_tag` 호출 뒤 Point Scale 변경이 실제 뷰포트에 반영되는 것을 확인했습니다.
- Modifier Runtime 경고, Projection Paint 패널 소속, Collection 표시 복원과 Point 갱신 회귀를 자동화했습니다.

## 배포물

`build/colmap_tools-0.1.20.zip`의 SHA-256은 `12AA4F6DFF3F9C091BDB596BB7BCDE4D5224AA47486A2D1D97A6F9EA90929FF0`입니다.
