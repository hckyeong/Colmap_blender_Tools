# COLMAP Tools 0.1.5 Modifier 데이터 격리 Release Note

## 해결한 문제

COLMAP Modifier가 가져온 Point와 Camera는 실제 Blender Collection·Object이므로 기존에는 Modifier Workspace를 종료해도 일반 Blender Workspace와 다른 COLMAP Workspace에 계속 표시되었다. Align의 GPU 표시 데이터처럼 Workspace 경계 밖에서는 보이지 않되, Modifier 편집 결과와 Camera Review 상태는 삭제하지 않고 보존할 필요가 있었다.

## 변경 내용

- Scene마다 애드온 소유 `COLMAP Modifier Data` View Layer를 하나 보장한다.
- Modifier가 소유한 최상위 Collection은 전용 View Layer에만 포함하고 다른 모든 View Layer에서는 `LayerCollection.exclude`로 제외한다.
- Modifier Workspace 진입 시 전용 View Layer를 활성화하고, 종료 시 진입 전에 사용하던 일반 View Layer와 Workspace를 복원한다.
- 기존 `.blend` 데이터, Import 완료, 파일 로드와 Blender Undo·Redo 뒤에도 관리 Collection의 포함·제외 상태를 다시 동기화한다.
- Object나 Collection 데이터 블록을 삭제하거나 전역 `hide_viewport`를 변경하지 않으므로 Modifier, 편집 결과, Export 대상과 Camera Review Pending·Undo 상태를 보존한다.
- 명시적 종료 직후 빠르게 Modifier를 다시 여는 경우 남아 있던 일반 Workspace 복귀 확인을 새 진입 요청으로 병합해 새 세션이 다시 종료되는 경합을 제거한다.

## 회귀 검증

- Blender Background에서 전용 View Layer의 관리 Collection 포함, 모든 일반 View Layer의 제외와 사용자 Collection의 정상 표시를 확인했다.
- 실제 COLMAP TXT Fixture의 Import·Frame All·Camera Review·Point Edit·Global Undo·BIN Export를 실행했다.
- Modifier 종료 후 관리 Object 데이터 블록은 유지되고 일반 Workspace에서는 보이지 않음을 확인했다.
- 빠른 재진입 후 전용 View Layer와 관리 Collection이 복원되고 Pending Camera와 전용 Undo가 유지됨을 확인했다.
- Align, Modifier, Projection Paint와 일반 Workspace 사이의 전체 자동 종료·전환 GUI 행렬을 통과했다.

## 배포 파일

`build/colmap_tools-0.1.5.zip`은 `SmallGicp` Production 프로필이다. SHA-256은 `1371F308C3FF736D94300928B4827E6CEB8456C962B4290908B14204C6DB748C`이다.
