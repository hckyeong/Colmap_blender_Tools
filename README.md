# COLMAP Tools

`COLMAP Tools`는 다음 세 Blender 도구를 하나의 Extension으로 배포합니다.

- COLMAP Align
- COLMAP Modifier
- Projection Painting

세 도구는 실행 순서나 데이터 전달 관계가 없는 독립 도구입니다. Root Extension은 설치, 등록과 `Window → COLMAP Tools` 메뉴만 통합합니다.

통합 원칙과 단계별 작업 범위는 [독립 도구 통합 계획서](docs/COLMAP_Tools_독립도구_통합_계획서.md)를 참조하십시오.

## 현재 구조

```text
shell/       공통 Extension 등록과 메뉴
tools/       서로 독립된 Align, Modifier, Projection Painting
shared/      계약 검증 뒤에만 사용할 공통 경계
tests/       Architecture, 공존과 도구별 회귀 검증
docs/        현행 설계와 통합 계획
build_tools/ Extension 검증과 빌드 도구
```

## 빌드

Blender 5.2가 기본 경로에 설치된 환경에서 다음 명령을 사용합니다.

```powershell
.\build_tools\build_extension.ps1
```

기본 빌드는 Align의 검증된 `small_gicp` Native Runtime을 Build Staging에만 포함하는 `SmallGicp` Production 프로필입니다. 생성한 ZIP의 격리 설치와 설치본 합성 GICP 검증은 다음 명령으로 실행합니다.

```powershell
.\build_tools\test_installed_extension.ps1
```
