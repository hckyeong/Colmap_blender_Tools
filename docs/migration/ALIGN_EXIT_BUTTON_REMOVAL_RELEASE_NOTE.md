# COLMAP Tools 0.1.9 Align 수동 종료 버튼 제거 Release Note

## 변경 목적

COLMAP Align은 다른 COLMAP Workspace 또는 일반 Blender Workspace로 이동할 때 공용 Workspace Handoff가 세션을 자동 종료합니다. 같은 동작을 직접 실행하던 N 패널의 `COLMAP Align 종료` 버튼은 중복 UI이므로 제거합니다.

## 변경 범위

- `COLMAPALIGN_PT_main`은 자식 패널을 묶는 최상위 컨테이너로 유지합니다.
- 패널의 `colmap_align.exit_session` 버튼만 제거합니다.
- `COLMAPALIGN_OT_exit_session` 오퍼레이터와 내부 세션 종료 로직은 유지합니다.
- 자동 Workspace 전환, 등록 해제와 테스트 정리 경로는 기존 종료 오퍼레이터를 계속 사용할 수 있습니다.

## 사용자 동작

Align 작업을 마치려면 다른 COLMAP Workspace 또는 일반 Blender Workspace로 이동합니다. 공용 전환 조정기가 Align 세션을 먼저 종료한 뒤 사용자가 선택한 Workspace로 이동합니다.

## 검증 기준

- Align N 패널 코드에 `colmap_align.exit_session` 버튼 호출이 없어야 합니다.
- 종료 오퍼레이터는 등록된 상태를 유지해야 합니다.
- Align 등록 회귀와 전체 Workspace GUI 자동 전환 회귀가 통과해야 합니다.

## 배포물

`build/colmap_tools-0.1.9.zip`은 `SmallGicp` Production 프로필이며 SHA-256은 `F900777937E9FD560F1416EC524D3AF8D17DF4437E182B6DF0513114CCABA886`입니다.
