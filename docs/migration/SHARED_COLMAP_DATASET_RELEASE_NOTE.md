# COLMAP Tools 0.2.0 공용 COLMAP Dataset Release Note

## 변경 내용

- COLMAP Import는 Modifier에만 유지했습니다.
- Projection Painting의 중복 Import UI와 Operator 등록을 제거했습니다.
- `Scene.colmap_dataset`과 중립 소유 Blender Resource를 추가했습니다.
- Modifier와 Projection Painting이 같은 Point·Camera Object와 Camera Data를 사용합니다.
- Modifier의 확정 편집은 Dataset Revision으로 기록되고 Projection Painting 재연결 시 반영됩니다.
- Point Scale, Camera Frustum과 Axis 표시 설정을 공용 Dataset에 통합했습니다.
- 공용 Dataset은 Modifier와 Projection Painting 전용 View Layer에서만 보입니다.
- Paint Target, Layer, Texture, Stencil, Marker와 Undo는 기존처럼 도구별로 분리됩니다.
- 기존 Projection Painting 자원은 자동 삭제하지 않고 Legacy 상태로 보존합니다.

## 호환성

Align은 이 변경에 의존하지 않으며 기존 독립 동작을 유지합니다. Projection Painting은 새 COLMAP 데이터를 직접 Import하지 않으므로 Modifier에서 먼저 Import해야 합니다. `PINHOLE`과 `SIMPLE_PINHOLE`이 아닌 Camera Model은 Dataset을 유지한 채 Projection 기능만 차단합니다.

## 검증

- 순수 Python 구조·공존·Modifier·Projection Painting 회귀
- Blender 5.2 공용 Dataset 등록과 Modifier Import·편집·Export 회귀
- Modifier와 Projection Painting의 공용 Object Pointer, Marker, F7과 Stencil 재연결 회귀
- 일반·Align·Modifier·Projection Painting View Layer 가시성 회귀
- Workspace 전환과 실제 F7 입력 GUI 회귀
- SmallGicp Production ZIP 격리 설치 회귀

Production 패키지는 `build/colmap_tools-0.2.0.zip`이며 SHA-256은 `840BCFA5BBC4280F6A6797D81D81FE5291E3B84E188E57D08606BA26DBBD5F73`입니다. 격리 설치본과 실제 Blender 사용자 저장소 설치본에서 버전 `0.2.0`, 공용 Dataset 등록, Projection Painting Import 클래스 미등록을 확인했습니다.
