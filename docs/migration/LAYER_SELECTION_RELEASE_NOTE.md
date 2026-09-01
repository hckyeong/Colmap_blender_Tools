# COLMAP Tools 0.3.2 Projection Paint 레이어 선택 통합

Projection Paint의 Layer·Layer Mask·Adjustment·Adjustment Mask 선택 방식을 하나로 통합했습니다.

- 행을 한 번 클릭하면 해당 항목이 바로 선택되고 Paint 대상도 함께 활성화됩니다.
- 활성 Layer 이름에 표시하던 체크 표시와 더블클릭 전용 활성화 동작을 제거했습니다.
- 일반 클릭은 기존 선택을 해제하고 한 항목만 선택합니다.
- `Ctrl` 클릭은 개별 항목을 선택하거나 해제하고, `Shift` 클릭은 마지막 선택 기준부터 현재 행까지 범위 선택합니다.
- 두 항목 이상 선택된 동안에는 Projection Paint 작업공간의 Viewport 페인팅 입력을 차단하고 경고를 표시합니다.
- N 패널 버튼과 다른 Blender 작업공간의 입력은 차단하지 않습니다.

Composite 합성 후보 계산은 기존처럼 최상위 Paint Layer만 사용합니다. Mask나 Adjustment를 복수 선택해도 합성 대상에는 포함되지 않습니다.

Production 패키지는 `build/colmap_tools-0.3.2.zip`이며 SHA-256은 `19536B3F5924637C844D3CD18192C01C0EE7F78DEF63BE0C97B87FA2F48FA114`입니다. 격리 설치본 검사와 실제 Blender `user_default` 설치본 검증을 통과했습니다.
