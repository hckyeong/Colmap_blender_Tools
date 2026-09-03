# Modifier 독립 Runtime·전용 Undo/Redo 0.3.34 Release Note

## 변경 내용

- Modifier의 현재 COLMAP 데이터와 편집 이력을 Blender Scene이 아닌 독립 Python Runtime Domain의 권위로 전환했습니다.
- Point·Camera 제거와 Pending Filter 적용은 변경 범위 Forward/Inverse Delta를 전용 Journal에 기록합니다.
- Modifier Workspace의 `Ctrl+Z`는 Modifier Undo, `Ctrl+Shift+Z`와 `Ctrl+Y`는 Modifier Redo로 동작합니다. 빈 Journal 입력도 Blender Global Undo로 넘어가지 않습니다.
- 다른 Blender Workspace와 Projection Painting의 Global Undo는 기존대로 작동하며 Modifier Domain과 Journal을 변경하거나 폐기하지 않습니다.
- Global Undo가 파생 Point·Camera·Marker를 바꾸면 보존된 Runtime 결과로 표시만 다시 동기화합니다.
- Import는 Loader 결과를 새 Domain에 직접 설치하고, Export 성공은 현재 결과를 새 Baseline으로 확정한 뒤 Undo·Redo Journal을 비웁니다.
- `.blend`에는 Source Metadata, Domain UUID·Generation과 확정 EditOperation만 Active/Staging Text로 원자 저장합니다.
- 재실행 때 Source를 자동으로 읽고 Operation을 재생해 Runtime과 Undo Command를 다시 구성합니다. Redo Stack은 빈 상태로 시작합니다.
- Projection Painting의 수동 데이터 업데이트는 Modifier의 Domain UUID·Generation·Revision·History Hash를 포함한 불변 Snapshot을 복사하며 Runtime이나 Journal을 공유하지 않습니다.

## 사용자 경험

1. COLMAP을 Import하고 Modifier에서 편집합니다.
2. Modifier Workspace의 `Ctrl+Z`, `Ctrl+Shift+Z`, `Ctrl+Y`는 Modifier 작업만 되돌리거나 다시 적용합니다.
3. Projection Painting이나 일반 Workspace에서 Blender Undo를 사용해도 Modifier 편집 결과는 바뀌지 않습니다.
4. Workspace로 돌아오면 원본을 다시 읽지 않고 기존 Runtime을 즉시 표시합니다.
5. `.blend`를 저장하고 다시 열면 저장한 Source와 Operation으로 Modifier Runtime을 자동 복원합니다.
6. Export가 성공하면 출력 결과가 새 원본 기준선이 되고 이전 Modifier Undo·Redo 기록은 비워집니다.

## 기존 프로젝트 주의사항

자동 마이그레이션은 제공하지 않습니다. `0.3.33` 이하에서 저장한 `.blend`의 기존 Modifier 데이터는 `0.3.34`에서 한 번 다시 Import한 뒤 저장해야 합니다. 기존 Projection Painting Snapshot은 독립 데이터로 유지되지만, 최신 Modifier 결과를 사용하려면 사용자가 `COLMAP 데이터 업데이트`를 실행해야 합니다.

## 검증

- Modifier Core 216개
- Architecture 12개
- Coexistence 11개
- Shared Transfer 8개
- Modifier Import·Export·Point Edit·저장 재열기·Global Undo Blender Background 회귀
- 실제 Blender GUI의 `Ctrl+Z → Ctrl+Shift+Z → Ctrl+Z`, F7, Export와 Workspace 재진입 회귀

Production ZIP은 `build/colmap_tools-0.3.34.zip`이며 SHA-256은 `AE85BE8034C49567180B79462E7FC157CD8CCDDDE4931ABD8F1E6A80C925DAC7`, 크기는 28,725,664바이트입니다. 격리 설치본과 실제 Blender `user_default` 설치본에서 버전 `0.3.34`를 확인했습니다.
