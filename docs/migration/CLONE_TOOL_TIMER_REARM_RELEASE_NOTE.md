# Clone Source 위치 지정 작업공간 복귀 릴리스 노트

## 문제

Projection Paint 작업공간을 벗어나면 Paint Tool Edge Timer가 정상 종료되지만, 다시 돌아올 때 Camera View Timer만 등록되고 Paint Tool Edge Timer는 누락됐습니다. 이 상태에서 Clone Tool을 선택해도 저장 파일에 남은 `use_clone_layer=True`가 유지되어 `Clone from Paint Slot` 방식이 활성화됐고, 같은 이미지 Clone의 Viewport `Ctrl+LMB` Source 위치 지정이 작동하지 않았습니다.

## 변경 사항

- 작업공간 복귀 시 Paint Tool Edge Timer와 Camera View Timer를 각각 다시 등록합니다.
- Clone Tool이 이미 활성인 상태로 복귀해도 `use_clone_layer=False`를 확정합니다.
- Clone Tool 전환 Edge에서도 기존과 동일하게 `use_clone_layer=False`를 한 번 적용합니다.
- Clone Brush와 Blender의 `clone_offset`은 읽거나 직접 쓰지 않습니다.

## 검증 기준

- 작업공간을 벗어나면 두 Timer가 종료되고, 복귀하면 두 Timer가 다시 등록됩니다.
- 저장된 `use_clone_layer=True` 상태에서 Clone Tool에 진입하면 값이 `False`가 됩니다.
- 활성 Layer Image가 Source이자 Target으로 유지됩니다.
- Viewport `Ctrl+LMB` Source 위치 지정, Clone Stroke와 Blender Undo가 실제 GUI에서 동작합니다.
- 파일을 저장하지 않고 실데이터 작업공간 왕복과 Blender 기본 Paint 도구 전환을 검증합니다.
