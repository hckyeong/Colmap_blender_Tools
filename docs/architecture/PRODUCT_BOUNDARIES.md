# 제품 경계

`COLMAP Align`, `COLMAP Modifier`, `Projection Painting`은 하나의 Extension에 포함되지만 서로의 실행이나 데이터를 전제로 하지 않는 독립 도구입니다.

## 공통 Root 책임

- 단일 Extension 등록과 해제
- `Window → COLMAP Tools` 메뉴
- Add-on Preferences
- 도구별 Lifecycle 호출과 등록 실패 Rollback

## 도구 책임

각 도구는 Scene Property, Runtime, Workspace, Handler, Timer, Draw Handler, Keymap과 테스트를 독립적으로 소유합니다. 다른 도구의 Runtime 모듈을 직접 Import하지 않습니다.

## Workspace 전환 경계

`shared/blender_runtime/workspace_handoff.py`는 도구별 Runtime을 Import하지 않고 각 도구가 등록한 `시작`, `점유 판정`, `종료 요청`, `종료 완료 판정`, `오류 기록` Callback만 순서대로 호출합니다. Layout 구성과 데이터 정리는 계속 각 도구가 소유하며, 공용 계층은 이전 도구 종료가 확인되기 전에 다음 도구를 시작하지 않는 Window 단위 배타 실행만 보장합니다.

## 공통화 기준

같은 이름이나 유사한 코드만으로 공통화하지 않습니다. 입력·출력·예외·성능 계약과 도구별 회귀 테스트가 일치하는 코드만 `shared/`로 이동합니다.
