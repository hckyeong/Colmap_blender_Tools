# COLMAP Tools 0.3.0 Projection Painting 독립 Snapshot 변경 기록

## 변경 내용

- Modifier 확정 Reconstruction을 Blender ID 없는 값으로 제공하는 `shared/colmap_transfer`를 추가했습니다.
- Projection Painting Dataset 패널에 `COLMAP 데이터 업데이트` 버튼을 추가했습니다.
- 최종 Dataset 패널은 업데이트 버튼과 Point·Camera 크기만 표시하며 Snapshot 상태 정보와 Display Axis UI는 숨깁니다.
- PP Camera, Camera Data, Point Mesh, Axis와 Manifest를 새 Snapshot UUID의 전용 Resource로 생성합니다.
- 재업데이트는 PP Point Scale·Camera Frustum·Axis와 Paint Target·Layer·Texture pixel을 보존합니다.
- Source UUID·Revision·Fingerprint가 Staging 중 변경되면 새 Resource만 폐기하고 기존 Snapshot을 유지합니다.
- 공용 Dataset 자동 Binding과 Display Listener 등록을 신규 PP 경로에서 제거했습니다.
- Painting Undo에서 비활성 Modifier full reconcile을 예약하지 않고, 비활성 PP Runtime도 Workspace 재진입 때 지연 복구합니다.

## 검증 상태

- 순수 Python Transfer·Provider·Modifier Core·Projection Painting Core 회귀 통과
- Blender 5.2 통합 등록·해제 통과
- Blender Background 최초/재업데이트, 독립 표시값, Paint pixel 보존과 실패 Rollback 통과
- Blender Background Camera Import 원자성, Lifecycle과 Undo 비활성 복구 격리 통과
- `SmallGicp` Production ZIP 빌드와 격리 설치본 활성화·Native Runtime·OBJ UV 왕복 통과
- 실제 Blender `user_default` 저장소 재설치와 Snapshot 업데이트 Operator·속성 등록 확인

Production 패키지는 `build/colmap_tools-0.3.0.zip`이며 SHA-256은 `1817922E749BDFD2EA3557E544F8A74C1085FB0F0F4461ACA2692C8FF4017EE7`입니다. 실데이터 GUI의 593 Camera·8K Undo 성능과 기존 `0.2.0` 저장 파일 전체 마이그레이션은 최종 QC 대상입니다.
