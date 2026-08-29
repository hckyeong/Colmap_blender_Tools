# COLMAP Tools 0.1.17 Align OBJ 텍스처 왕복 Release Note

## 변경 내용

- Align OBJ Reader가 `mtllib`, `usemtl`, MTL의 `Kd`와 `map_Kd`를 읽어 면별 재질과 diffuse 이미지 원본 경로를 Runtime에 보관합니다.
- UV와 실제 diffuse 이미지가 함께 있는 OBJ는 `COLMAP RGB`가 켜졌을 때 Align 전용 GPU 뷰포트에 텍스처로 표시됩니다.
- `COLMAP RGB`가 꺼지면 기존 Reference·Moving 역할 색상으로 표시되며 점군 표시 동작은 그대로 유지됩니다.
- UV seam을 보존하기 위해 텍스처 면은 face-corner 위치·법선·UV 배치로 생성합니다.
- OBJ Export는 모델별 MTL을 생성하고 이미지 파일을 모델 전용 텍스처 하위 폴더에 원본 바이트 그대로 복사합니다.
- 출력 OBJ의 `mtllib`, `usemtl`과 생성 MTL의 `map_Kd`는 복사본의 새 상대 경로를 참조합니다.
- 텍스처 복사는 1 MiB 단위로 진행하고 원본 상태와 복사본 SHA-256을 검증한 뒤에만 `aligned_obj` 후보를 최종 승격합니다.
- Export 보고서를 Schema 3으로 올리고 재질 수, diffuse 텍스처 수, 복사 상대 경로와 SHA-256을 기록합니다.

## 호환 범위

- 표준 `OBJ → mtllib → MTL map_Kd → 이미지` 연결이 있는 diffuse 텍스처를 지원합니다.
- UV, MTL 또는 이미지가 없으면 기존 역할 색상 메시 표시와 텍스처 없는 OBJ Export를 유지합니다.
- 원본 MTL을 그대로 복사하지 않습니다. 출력 경로가 바뀌므로 같은 재질 이름·`Kd`를 사용하는 새 MTL을 생성하고 `map_Kd`만 검증된 복사본 상대 경로로 다시 기록합니다.
- Normal Map, Roughness, Metallic, 투명도 Map과 임의 MTL 확장 옵션은 이번 범위에 포함하지 않습니다.
- 프로젝트 JSON Schema는 변경하지 않습니다. 프로젝트 복원은 원본 OBJ와 MTL을 다시 읽습니다.

## 검증 결과

- OBJ·MTL Reader의 재질 순서, 면 재질 연결, 따옴표 포함 `map_Kd`와 옵션 파싱 통과
- UV seam용 face-corner 텍스처 배치와 Blender 5.2 GUI GPU 셰이더·이미지 자원 생성/해제 통과
- OBJ·MTL 생성, 원본 이미지 바이트 복사, SHA-256, 취소·덮어쓰기 원자성 통과
- 내보낸 OBJ를 Blender 5.2 기본 OBJ Importer로 다시 읽어 UV Layer와 복사 이미지 재질 연결 통과

`build/colmap_tools-0.1.17.zip`의 SHA-256은 `344A4CE31D7B33138C7C0BD54EF946C93ECB3952CB2130F437A277EB2C431E07`입니다.
