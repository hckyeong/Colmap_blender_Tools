# 0.3.18 프로젝트 전용 Stencil Draw Brush

## 변경 목적

Blender Essentials Draw Brush는 읽기 전용 자산일 수 있어 Camera Stencil Texture를 직접 연결할 수 없습니다. 사용자가 매번 편집 가능한 Brush를 따로 선택하지 않아도 되도록, 최초 수동 Reconnect에서만 현재 Draw 설정을 복제한 프로젝트 전용 Brush를 생성합니다.

## 변경 동작

- Draw Tool에서 `Reconnect Stencil Texture`를 처음 누르면 `Projection Paint Stencil Draw` Local Brush가 정확히 하나 생성됩니다.
- 생성된 Brush는 현재 Draw Brush의 크기·강도·Blend·Stroke·Texture 설정을 이어받고 Camera Stencil을 연결합니다.
- 이후 Reconnect와 Draw Tool 복귀는 저장된 동일 Brush를 재사용합니다.
- `.blend` 저장·재열기 뒤에도 프로젝트 전용 Brush와 Stencil 연결을 그대로 사용합니다.
- Clone, Blur, Smear, Fill, Mask Brush는 복제하지 않으며 Blender 기본 또는 사용자가 선택한 Brush를 그대로 사용합니다.
- Tool·Workspace·저장·종료 경계에서 외부 Brush 상태를 복구하는 장기 Snapshot이나 임대 구조를 두지 않습니다.

## 실패 처리

최초 Reconnect 도중 Camera·Texture·Viewport 연결이 실패하면 원래 활성 Brush를 복구하고, 그 호출에서 새로 생성한 프로젝트 전용 Brush만 제거합니다. 이전에 정상 생성되어 저장된 프로젝트 전용 Brush와 사용자 Brush는 삭제하지 않습니다.

## 데이터 호환

자동 데이터 마이그레이션은 실행하지 않습니다. 기존 파일에 프로젝트 전용 역할이 기록된 Brush가 없으면 다음 수동 Reconnect가 새 Brush를 한 번 생성합니다. 기존 Layer, Image, Composite, Mask, Camera와 Paint Pixel은 변경하지 않습니다.

## 검증 기준

- 편집 가능한 Draw Brush와 읽기 전용 Essentials Draw Brush 모두에서 최초 Reconnect가 전용 Brush를 하나만 생성해야 합니다.
- 두 번째 Reconnect와 Draw → 다른 Tool → Draw 왕복은 같은 Brush Pointer를 사용해야 합니다.
- 최초 Reconnect 실패 뒤 원래 Brush가 활성화되고 새 Brush가 남지 않아야 합니다.
- 저장·재열기 뒤 동일 전용 Brush와 Camera Stencil이 유지되어야 합니다.
- Native Clone Source 지정, Pixel 복제와 Undo 결과가 변경되지 않아야 합니다.
