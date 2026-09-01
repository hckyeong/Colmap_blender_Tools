# Projection Paint 레이어 선택·활성화·삭제 UX 0.3.23

## 변경 사항

- Layer·Layer Mask·Adjustment·Adjustment Mask는 한 번 클릭하면 UI 선택만 바뀝니다.
- 같은 항목을 Blender 더블클릭 제한 시간 안에 수정키 없이 두 번째로 클릭하면 실제 Paint 대상이 활성화됩니다.
- `Ctrl`·`Shift` 클릭은 복수 선택만 처리하며 실제 Paint 대상을 바꾸지 않습니다.
- 선택은 기존 파란 배경으로, 실제 활성 Paint 대상은 별도의 `RADIOBUT_ON` 기호로 표시합니다.
- 레이어 트리 외부를 클릭하면 선택 배경만 사라지고 실제 활성 대상과 Paint Image는 유지됩니다.

## 삭제 규칙

- `-` 버튼과 레이어 트리 위 `Del`은 활성 항목이 아니라 파란 배경으로 선택된 항목을 삭제합니다.
- Layer와 하위 Mask·Adjustment가 함께 선택되면 부모 Bundle을 한 번만 삭제합니다.
- 삭제 대상은 UUID로 다시 확인하고 깊은 자식과 높은 Index부터 처리합니다.
- Composite 또는 잠긴 항목이 하나라도 섞이면 실제 삭제 전에 전체 작업을 중단합니다.
- 복수 삭제의 Shader Graph 재조정은 모든 논리 삭제가 끝난 뒤 한 번만 수행합니다.
- 마지막 쓰기 가능한 Paint Layer를 삭제하면 기존 계약대로 새 빈 `Layer 1`로 교체합니다.
- `Del`은 Projection Paint 레이어 트리 문맥에서만 소비하며 다른 Editor와 3D Viewport의 Blender 기본 Delete는 통과시킵니다.

## 호환성과 수명주기

- 스크립트의 기존 `EXEC_DEFAULT` 선택 호출은 즉시 선택·활성화 동작을 유지합니다.
- 더블클릭 후보는 파일 Load, Undo, Redo, 작업공간 전환과 Extension 해제 경계에서 제거합니다.
- Layer Pixel, Shader 합성 순서, Composite 계산, Brush, Clone, Stencil과 Camera Frame Source 구조는 변경하지 않습니다.
- 저장 데이터 Schema와 자동 마이그레이션은 추가하지 않았습니다.

## 검증 결과

- Layer UI 41개, 수명주기 14개, Original Layer 16개, Layer Shader 47개 회귀 통과
- Paint Tool Bridge 8개, Paint Resources 43개와 Paint Runtime Mask·Stencil 회귀 통과
- Composite 전체 61개 회귀 통과
- Production 검사 220개, Extension 등록·해제와 내장 Small GICP Runtime 합성 통과
- 격리 설치본 검사와 실제 Blender `user_default`의 0.3.23 버전·설치 경로 확인

실제 버튼 좌표가 필요한 `gui_layer_selection_actual.py`의 입력 시뮬레이션은 최종 수동 QC 대상으로 남습니다. Production 패키지는 `build/colmap_tools-0.3.23.zip`이며 SHA-256은 `4E86574658AD430246BDA61A364C13319454634B413A28E068CC85C62173F9DC`, 크기는 28,677,279바이트입니다.
