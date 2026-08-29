# COLMAP Tools 0.1.19 Align OBJ Export 평면 배치 Release Note

## 변경 내용

- Export OBJ 파일명은 `모델표시이름.obj` 형식으로 생성합니다.
- MTL은 같은 모델 표시 이름의 `모델표시이름.mtl`로 생성합니다.
- 원본 텍스처 이미지는 별도 하위 폴더나 순번 접두사 없이 원본 파일명 그대로 OBJ·MTL과 같은 폴더에 복사합니다.
- 여러 모델이 같은 원본 이미지를 공유하면 이미지는 한 번만 복사하고 각 MTL이 같은 파일을 참조합니다.
- 모델 표시 이름이 중복되거나 서로 다른 원본 텍스처가 같은 파일명을 쓰면 기존 파일을 덮어쓰지 않고 명확한 충돌 오류를 표시합니다.
- Windows 경로 길이 제한 사전 진단과 구조화된 Export 실패 로그는 계속 유지합니다.

## 출력 예시

```text
aligned_obj/
├─ Gilt-bronze Pensive Maitreya Bodhisattva 78_low poly.obj
├─ Gilt-bronze Pensive Maitreya Bodhisattva 78_low poly.mtl
└─ Gilt-bronze Pensive Maitreya Bodhisattva 78_low poly.png
```

## 검증 결과

- 모델 표시 이름 전용 OBJ·MTL 파일명 회귀 통과
- 원본 이미지명의 단일 폴더 복사와 `_textures` 하위 폴더 미생성 회귀 통과
- 같은 원본 이미지 공유와 서로 다른 동명 이미지 충돌 거부 회귀 통과
- 사용자 오류 프로젝트 기준 후보 OBJ·MTL·이미지 경로가 각각 183자로 Windows 제한 안에 들어옴을 확인
- 격리 설치 ZIP과 실제 Blender 사용자 Extension 설치본의 OBJ·MTL·이미지 왕복 회귀 통과

`build/colmap_tools-0.1.19.zip`의 SHA-256은 `5F686587FBA709FFE79024F6DF329AD2B33D426C7B31B6B3CECF452A067781DE`입니다.
