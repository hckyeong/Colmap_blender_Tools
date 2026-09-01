# COLMAP Tools

`COLMAP Tools`는 다음 세 Blender 도구를 하나의 Extension으로 배포합니다.

- COLMAP Align
- COLMAP Modifier
- Projection Painting

세 도구는 순차 파이프라인이 아닌 독립 도구입니다. `COLMAP Modifier`만 COLMAP 데이터를 Import·편집·Export하며, `Projection Painting`은 사용자가 업데이트 버튼을 누른 시점의 값을 독립 Snapshot으로 복사합니다. `COLMAP Align`은 이 전달 구조에도 연결되지 않습니다.

`.blend`를 다시 열 때 Modifier의 대용량 원본은 자동 복원하지 않습니다. Modifier Workspace 진입 또는 Projection Painting의 `COLMAP 데이터 업데이트`에서만 진행률과 함께 지연 복원하며, 기존 Projection Painting Snapshot은 Modifier Runtime과 무관하게 재진입합니다.

통합 원칙과 단계별 작업 범위는 [독립 도구 통합 계획서](docs/COLMAP_Tools_독립도구_통합_계획서.md)를 참조하십시오.

## 현재 구조

```text
shell/       공통 Extension 등록과 메뉴
tools/       서로 독립된 Align, Modifier, Projection Painting
shared/      Workspace Runtime, 값 Snapshot 전달과 Legacy 마이그레이션 계약
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
