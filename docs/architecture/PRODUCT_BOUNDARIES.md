# 제품 경계

`COLMAP Align`, `COLMAP Modifier`, `Projection Painting`은 하나의 Extension에 포함됩니다. Align은 데이터까지 완전히 독립적입니다. Modifier와 Projection Painting도 Blender Resource를 공유하지 않으며, Projection Painting은 수동 업데이트 때만 Modifier 값을 독립 Snapshot으로 복사합니다.

## 공통 Root 책임

- 단일 Extension 등록과 해제
- `Window → COLMAP Tools` 메뉴
- Add-on Preferences
- 값 Transfer Registry와 도구별 Lifecycle 호출
- 등록 실패 Rollback

## 도구 책임

각 도구는 Runtime, Workspace, Handler, Timer, Draw Handler, Keymap과 도구별 테스트를 독립적으로 소유합니다. 다른 도구의 Runtime 모듈을 직접 Import하지 않습니다.

- Align은 공용 Dataset에 의존하지 않습니다.
- Modifier는 독립 Python Runtime Domain을 COLMAP Import·편집·Export의 유일한 권위로 사용합니다. Scene Property와 Point·Camera·Marker는 저장·표시용 파생 데이터입니다.
- Modifier 편집 Undo·Redo는 Domain의 전용 Journal이 소유합니다. Modifier Workspace의 `Ctrl+Z`, `Ctrl+Shift+Z`, `Ctrl+Y`만 전용 Journal로 연결하고 다른 Workspace의 Blender Undo는 그대로 유지합니다.
- Projection Painting은 수동 업데이트 뒤 자기 Camera·Point·Axis·Manifest를 독립 소유합니다.
- Projection Painting의 Paint Target, Layer, Texture, Stencil과 Undo는 공용화하지 않습니다.

## Snapshot Transfer 경계

`shared/colmap_transfer`는 Camera·Image·Point·Pose·경로·Source UUID·Domain UUID·Generation·Revision·History Hash·Fingerprint와 최초 표시 기본값만 전달합니다. Blender ID와 도구 Runtime Pointer는 포함하지 않습니다. Modifier 편집은 업데이트 전 Projection Painting Snapshot에 반영되지 않으며 Projection Painting의 Paint·Undo는 Modifier Runtime과 Journal에 영향을 주지 않습니다.

Modifier의 저장 Payload는 Source Metadata, Domain Identity와 확정 EditOperation만 포함합니다. Reconstruction·관계 Index·Undo Delta·Redo Stack은 저장하지 않으며 재실행 때 Source와 Operation으로 Runtime·Undo Command를 재구축합니다. 구버전 Payload는 자동 이전하지 않고 명시적인 재Import를 요구합니다.

Modifier Root와 PP Snapshot Root는 각각 자기 전용 View Layer에서만 표시하고 일반 Blender Workspace와 Align에서는 제외합니다. 실제 Timeline Marker와 세션 사용권은 계속 활성 도구 하나만 소유합니다.

## Workspace 전환 경계

`shared/blender_runtime/workspace_handoff.py`는 도구별 Runtime을 Import하지 않고 각 도구가 등록한 `시작`, `점유 판정`, `종료 요청`, `종료 완료 판정`, `오류 기록` Callback만 순서대로 호출합니다. Layout 구성과 데이터 정리는 계속 각 도구가 소유하며, 공용 계층은 이전 도구 종료가 확인되기 전에 다음 도구를 시작하지 않는 Window 단위 배타 실행만 보장합니다.

## 공통화 기준

같은 이름이나 유사한 코드만으로 공통화하지 않습니다. Blender ID 없는 값 Transfer 계약만 `shared/`에 두고 도구별 Resource, Runtime, UI와 Undo는 각 도구 경계에 유지합니다.
