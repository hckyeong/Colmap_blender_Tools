# Camera View Stencil 추적·Camera Texture 원색 투영 릴리스 노트

## 변경 사항

- Camera View 안에서 Viewport 이동·확대, 영역 크기와 Camera Frame 변화를 0.1초 간격의 저비용 Token 비교로 감지합니다.
- 변화가 있으면 사용자가 명시적으로 연결한 프로젝트 전용 Draw Brush의 Stencil 위치와 크기만 현재 Camera Frame에 맞춥니다.
- Viewport 회전으로 Camera View를 벗어나면 Brush Texture 연결만 해제하고 Source Image, 활성 Layer와 Painting Mode는 유지합니다.
- Camera View 복귀 시 같은 프로젝트 전용 Draw Brush가 활성인 경우에만 연결을 복구합니다. 다른 Blender Tool과 Brush는 선택하거나 변경하지 않습니다.
- 프로젝트 전용 Draw Brush 색상을 흰색으로 맞춰 Camera Texture RGB가 검은색과 곱해지는 문제를 수정했습니다.
- Reconnect Transaction Snapshot에 전용 Draw Brush 색상을 포함해 실패 시 호출 전 상태로 복구합니다.

## 호환성 경계

- Clone·Blur·Smear·Fill·Mask와 사용자 정의 도구의 Brush 설정에는 개입하지 않습니다.
- 사용자가 Camera View 이탈과 무관하게 Stencil Texture 또는 Mapping을 수동 변경한 경우 자동 복구하지 않습니다.
- Mask 편집 중에는 Camera Stencil 추적 Timer를 시작하지 않습니다.
- 파일 Load·Undo·Redo·Save 경계에 장기 Brush Snapshot이나 자동 마이그레이션을 추가하지 않습니다.

## 검증 기준

- Camera View 이동과 확대 뒤 Stencil 위치·크기가 실제 Camera Frame과 일치합니다.
- Camera View 이탈 뒤 Source와 Paint 상태가 유지되고 Brush Texture만 해제됩니다.
- Camera View 복귀 뒤 같은 Draw Brush Pointer만 다시 연결됩니다.
- 실제 Camera Texture Stroke가 검은색이 아닌 Source RGB를 Layer에 기록합니다.
- Blender 기본 Paint 도구 전환과 독립 Undo 동작이 기존과 동일합니다.
