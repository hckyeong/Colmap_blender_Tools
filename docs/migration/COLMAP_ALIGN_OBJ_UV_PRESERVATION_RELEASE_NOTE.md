# COLMAP Tools 0.1.16 Align OBJ UV 보존 Release Note

## 변경 내용

- Align OBJ Reader가 `vt` 1~3성분과 `f v/vt`, `f v/vt/vn`의 face-corner UV 인덱스를 읽습니다.
- 하나의 정점이 면마다 다른 UV를 쓰는 seam을 별도 코너 인덱스로 보존합니다.
- UV가 있는 면과 없는 면을 함께 가진 OBJ를 면 단위로 보존합니다.
- 이동·회전·스케일 정렬은 3D 정점에만 적용하고 UV 배열은 변경하지 않습니다.
- Reflection에서는 정점 Winding과 UV Corner 순서를 함께 반전합니다.
- OBJ Export가 `vt`와 `f v/vt`를 증분 기록하고 UV까지 왕복 검증한 뒤 결과를 확정합니다.
- Export 보고서를 Schema 2로 올리고 UV 수, UV 면 수와 Coverage를 기록합니다.

## 호환 범위

- UV가 없는 기존 삼각형 OBJ는 기존 `f v v v` 형식으로 계속 처리합니다.
- 프로젝트 JSON Schema는 변경하지 않습니다. 프로젝트 복원 시 저장된 OBJ source와 fingerprint로 UV를 다시 읽습니다.
- MTL, Texture Image, `vn`, Material Slot과 Blender Texture Viewport 생성은 이번 범위에 포함하지 않습니다.

## 검증 결과

- Architecture 11개, Coexistence 10개, Modifier Core 155개, Projection Paint Core 40개 통과
- Align OBJ Reader·Runtime·프로젝트 재로드와 600,000면 경계 통과
- Align OBJ Export·Reflection·부분 UV Coverage·원자성 통과
- Blender 5.2 기본 OBJ Importer의 face-corner UV Layer와 seam 재생성 통과
- Blender 통합 등록·해제와 관련 Layer Transform·Marker Align·ICP 회귀 통과

`build/colmap_tools-0.1.16.zip`의 SHA-256은 `6CAE3BD07AFD787DC919443B4004F7EAA6456BF3636C4EA451B85CD5E461C3AB`입니다.
