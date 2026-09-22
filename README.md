# COLMAP Tools

**[최신 설치 ZIP 다운로드](https://github.com/hckyeong/Colmap_blender_Tools/raw/refs/heads/main/colmap_tools.zip)** · 현재 버전 **0.3.71** · Windows x64 / Blender 5.2 이상

다운로드한 `colmap_tools.zip`을 Blender의 **환경설정 → 애드온 → 디스크에서 설치**에서 선택하세요. 저장소 최상위에는 최신 정식 설치본 하나만 유지합니다.

`COLMAP Tools`는 다음 세 Blender 도구를 하나의 Extension으로 배포합니다.

- COLMAP Align
- COLMAP Modifier
- Projection Painting

세 도구는 순차 파이프라인이 아닌 독립 도구입니다. `COLMAP Modifier`만 COLMAP 데이터를 Import·편집·Export하며, `Projection Painting`은 사용자가 업데이트 버튼을 누른 시점의 값을 독립 Snapshot으로 복사합니다. `COLMAP Align`은 이 전달 구조에도 연결되지 않습니다.

0.3.34 이후 저장된 정상 Modifier Active Payload는 `.blend`를 다시 열 때 원본과 확정 편집 기록으로 Runtime을 자동 복원합니다. 기존 Projection Painting Snapshot은 Modifier Runtime과 독립적으로 유지하며, 최신 편집 결과는 사용자가 `COLMAP 데이터 업데이트`를 실행할 때만 복사합니다.

완료된 기능·제품 경계·저장·검증 결과는 [통합 문서](docs/README.md)를 참조하십시오. `docs/temp/`에는 후속 집필·검증에 필요한 자료 9개만 남겼으며, 정리한 원문의 확인·복원 방법은 [문서 정리 내역](docs/README.md#archive)을 참조하십시오.

설치·실행은 [사용자 매뉴얼 공통 안내](docs/manual/README.md), 기능별 사용 방법은 [Align](docs/manual/ALIGN.md) · [Modifier](docs/manual/MODIFIER.md) · [Projection Paint](docs/manual/PROJECTION_PAINT.md)를 참조하십시오. 캡처 위치 22곳을 표시했으며 화면 이미지는 삽입 대기 상태입니다.

## 현재 구조

```text
shell/       공통 Extension 등록과 메뉴
tools/       서로 독립된 Align, Modifier, Projection Painting
shared/      Workspace Runtime, 값 Snapshot 전달과 Legacy 마이그레이션 계약
tests/       Architecture, 공존과 도구별 회귀 검증
docs/        완료 기능 통합 문서와 temp/의 선별 보존 자료
build_tools/ Extension 검증과 빌드 도구
```

## 빌드

Blender 5.2가 기본 경로에 설치된 환경에서 다음 명령을 사용합니다.

```powershell
.\build_tools\build_extension.ps1
```

기본 출력은 저장소 최상위의 **`colmap_tools.zip`**입니다. 빌드와 ZIP 무결성 검사가 성공하면 이 파일을 교체하고, 같은 출력 폴더의 이전 `colmap_tools-버전.zip`을 정리합니다. 실패한 빌드는 기존 설치본을 유지합니다. 출력 위치를 따로 지정할 때는 `-OutputDirectory`를 사용합니다.

0.3.37부터 기본 빌드는 `CCCoreLib` 프로필입니다. 전용 CPython 3.12.10·NumPy 1.26.4와 CC v4·oneTBB 실행 환경을 ZIP에 포함하므로 별도 Python 설치 없이 정렬할 수 있습니다. 실행 환경은 Git에서 제외한 `.cccore-runtime/`에 준비하고 빌드 스테이징에 복사합니다. 처음 네이티브 실행 환경을 준비할 때에는 Visual Studio 2022 C++ 빌드 도구와 네트워크가 필요합니다.

`-CcCorePythonPath`와 `-CcCoreRuntimePath`로 검증된 실행 환경을 지정할 수도 있습니다. 고정 버전·바이너리·라이선스·재빌드 원문 해시와 실제 정렬 검사를 통과한 입력만 사용합니다.

생성한 ZIP의 격리 설치, 실제 네 가지 CC 입력 조합, 저장·자동 재연결 및 세 도구 공존 검증:

```powershell
.\build_tools\test_installed_extension.ps1 -WorkspaceGui
```

기존 명시적 `-AlignProfile SmallGicp`는 희소 점군 비교용으로 유지합니다. `-AlignProfile Full`은 CC 실행 환경과 선택형 엔진 소스를 포함하며, 구엔진 비교에 필요한 별도 Python 경로를 환경설정에서 지정합니다. 기본 CC ZIP에는 Small GICP·Open3D 실행 패키지를 포함하지 않습니다.

## Align 0.3.41

3점 정렬 또는 현재 위치에서 먼 영역을 먼저 제외하고, 겹침 후보 안에서 메시 표본 50,000개를 확보합니다. 점군은 중복 없이 최대 50,000개를 사용합니다. 결과는 처음 고정한 겹침 평가 표본으로 비교하며, 상태창에서 전체 참고 RMS와 구분합니다. 추가 초기 ICP는 실행하지 않습니다. [통합 정렬 설명](docs/README.md#align-cc)

### 0.3.40 왼쪽 패널 기본값

새 Align 작업공간에서는 왼쪽 A 뷰의 N 패널은 닫고 오른쪽 P 뷰의 정렬 패널만 엽니다. 이후 사용자가 왼쪽 패널을 직접 열면 재진입해도 그 상태를 유지합니다. [통합 패널 설명](docs/README.md#align-ui)

### 0.3.39 기본 폭

새 Align 작업공간의 N 패널을 요청한 기본 폭으로 엽니다. 기본 폭은 화면 배율 적용 전 370이며, 현재 환경에서는 약 478픽셀입니다. 기존 `.blend`에 저장된 폭과 사용자가 조절한 폭은 유지합니다. [통합 패널 설명](docs/README.md#align-ui)

### 0.3.38 오류 수정

내보내기를 실행하지 않은 상태에서 `.blend`를 열 때 `COLMAP Export 오류`가 표시되던 문제를 수정했습니다. 실행 중인 작업이 없으면 이전 내보내기 상태를 비우고, 실제 작업을 중단한 경우에만 중단 사유를 남깁니다. [통합 복원 설명](docs/README.md#align-ui)

### 0.3.37에서 이어지는 기능

레이어의 점군·표면 아이콘에서 고른 형상으로 정렬하며, 각 데이터의 축은 레이어 설정에서 바꿀 수 있습니다. 연결 OBJ와 독립 OBJ의 원본 면·UV는 Blender Mesh로 내장하고, 정렬에는 삼각형 프록시를 사용합니다. OBJ 내보내기는 내장 원본에 확정 행렬을 적용하므로 쿼드/N곤을 보존합니다. COLMAP 원본은 기존 외부 파일 참조 방식을 유지합니다.

3점 마커 정렬 후 CC 정밀 정렬을 실행하거나 현재 위치에서 직접 정렬할 수 있습니다. CC는 강체 정렬 뒤 초기값 대비 최대 ±10% 균일 배율 보정을 수행합니다. 환경설정의 **CPU 스레드**는 0 자동, 1 단일, 2 이상 수동 상한이며 다음 작업부터 적용됩니다.

Align 작업공간에서 저장한 `.blend`를 다시 열면 데이터 복원 후 세션이 자동 연결됩니다. 일반 작업공간에서 저장한 파일은 해당 작업공간을 유지합니다. [통합 저장·복원 설명](docs/README.md#storage)과 [완료 이력](docs/README.md#history)을 참조하십시오.
