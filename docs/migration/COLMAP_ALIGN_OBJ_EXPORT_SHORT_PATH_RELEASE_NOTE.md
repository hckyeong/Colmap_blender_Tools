# COLMAP Tools 0.1.18 Align OBJ Export 짧은 경로 Release Note

## 변경 내용

- Export OBJ 파일명에서 모델 표시 이름을 제거했습니다.
- 새 파일명은 `순번_모델ID지문.obj` 형식입니다.
- MTL은 같은 이름의 `.mtl`, 텍스처 폴더는 같은 이름의 `_textures` 접미사를 사용합니다.
- OBJ 내부 `o` 레코드와 Export 보고서의 `display_name`은 기존 모델 표시 이름을 유지합니다.
- 후보 폴더를 만들기 전에 OBJ·MTL·보고서·텍스처 복사 경로를 모두 검사합니다.
- Windows에서 경로가 260자를 넘으면 파일 복사를 시도하지 않고 현재 길이, 제한값, 해결 방법과 대상 경로를 OBJ Export 오류에 표시합니다.
- 같은 오류를 Blender 로그에도 `obj_export_failed` 이벤트로 기록합니다.

## 검증 결과

- 긴 모델 표시 이름이 Export 파일명에 포함되지 않는 회귀 통과
- 272자 텍스처 출력 경로가 `[Errno 2]` 대신 명확한 경로 길이 오류로 변환되는 회귀 통과
- 사용자 오류 프로젝트 기준 텍스처 대상 경로가 272자에서 219자로 감소함을 확인
- OBJ·MTL·원본 텍스처 복사와 Blender 재수입 회귀 통과

`build/colmap_tools-0.1.18.zip`의 SHA-256은 `C27E5B1A7FC0DFCC8CFC1D7504860F827935D5FD60BE43AC98BC8DAA9D5DCEC2`입니다.
