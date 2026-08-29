# COLMAP Tools 0.1.10 Align N 패널 유지 Release Note

## 원인

다른 Workspace로 이동하면 Align 세션은 안전하게 종료됩니다. 보존된 READY Align Workspace를 다시 선택한 직후에는 자동 세션 재연결이 끝나기 전까지 세션 상태가 `INACTIVE` 또는 `ENTERING`이므로, 기존 패널 표시 조건이 N 패널 전체를 숨겼습니다.

## 변경

- N 패널 표시 여부는 활성 세션이 아니라 유효한 READY Align Workspace와 소유 Screen을 기준으로 판단합니다.
- 세션 재연결 중에도 Data Layers, Viewport, Marker Align과 Merge & Export 패널을 유지합니다.
- 재연결 전에는 패널 입력만 비활성화하고 `Align 세션 연결 중` 상태를 표시합니다.
- 세션이 다시 `ACTIVE`가 되면 기존 UI를 새로 만들지 않고 즉시 입력 가능 상태로 전환합니다.
- 일반 Workspace에서는 Align N 패널을 표시하지 않습니다.

## 검증

Workspace GUI 회귀에서 Align 종료 후 일반 Workspace를 거쳐 기존 READY Align Workspace 탭으로 직접 재진입합니다. 화면 소유권이 복구된 직후 N 패널 표시와 이후 세션 활성화를 각각 확인합니다.

## 배포물

`build/colmap_tools-0.1.10.zip`은 `SmallGicp` Production 프로필이며 SHA-256은 `9EA5EB508C6FBFFF6A299E49F8C323FDA916C98D9D80A3B3A39328F43DAEACED`입니다.
