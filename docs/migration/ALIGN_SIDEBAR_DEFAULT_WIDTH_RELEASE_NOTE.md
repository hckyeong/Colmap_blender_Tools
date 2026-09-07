# Align N 패널 기본 폭 0.3.39

- 작성일: 2026-09-07

## 변경

새 Align 작업공간의 N 패널 기본 폭을 화면 배율 적용 전 370으로 설정했습니다. 요청 당시 Blender에서 측정한 폭은 478픽셀이고 화면 배율은 약 1.29167입니다.

Align 전용 Workspace Seed에 폭을 저장합니다. 작업공간 진입 때마다 폭을 덮어쓰지 않으므로 기존 `.blend`의 폭과 사용자가 이후 조절한 폭을 유지합니다. 다른 도구의 Seed와 사용자 환경설정은 변경하지 않습니다.

Blender의 `workspace.append_activate`는 저장된 영역 폭을 기본값으로 초기화합니다. Align 새 작업공간에만 `bpy.data.libraries.load`로 Seed의 UI를 보존하는 옵션을 적용합니다. 기존 소유권·스키마 검사와 실패 시 정리 절차는 공통 Factory를 그대로 거칩니다. Modifier와 Projection Painting의 기본 호출 방식은 유지합니다.

## 템플릿 생성

`build_tools/workspaces/create_workspace_seed.py`에 선택적 `--sidebar-width` 인자를 추가했습니다. 이 옵션은 별도 Factory Startup GUI를 단일 3D Viewport로 정리한 다음 기본 폭을 설정합니다. 기존 Factory 화면은 Align 초기 구성 중 Outliner 영역만 남았다가 새 Viewport로 바뀌어 원래 3D Viewport에 설정한 폭이 사라졌습니다. 단일 Viewport Seed에서는 A/P 분할 후에도 설정한 폭이 이어집니다.

Blender의 영역 폭 속성은 읽기 전용이므로 Blender 경계 드래그 이벤트로 폭을 설정하고, 목표값을 확인한 뒤 Workspace와 Screen만 저장합니다. 런타임에 메모리를 직접 수정하는 코드는 없습니다.

기존 기본 생성 명령은 그대로 사용할 수 있습니다. Align Seed를 재생성할 때에는 Blender 5.2를 `--factory-startup --enable-event-simulate`로 시작하고 다음 인자를 전달합니다. 이 옵션은 백그라운드 실행에서는 사용할 수 없습니다.

```text
--python build_tools/workspaces/create_workspace_seed.py --
--tool-id align --workspace-name __COLMAP_ALIGN_SEED_5_2__
--output tools/align/resources/workspaces/colmap_align_seed_5_2.blend
--sidebar-width 370
```

생성 후 `build_tools/workspaces/validate_workspace_seed.py`로 중립 데이터와 메타데이터를 검증합니다. 기존 Seed 스키마와 작업공간 스키마는 유지합니다.

## 검증 결과

- Seed의 중립 데이터·메타데이터 검사 통과.
- 소스와 정식 설치본의 실제 GUI에서 새 Align 패널 폭 477픽셀, 일반 작업공간 왕복 후 477픽셀 유지 확인. 요청한 478픽셀과의 1픽셀 차이는 Blender 정수 폭 변환에 따른 차이입니다.
- 공통 Factory GUI 검사에서 Align 기본 폭 보존과 세 도구의 생성·소유권·삭제 확인.
- 정식 빌드의 구조·공존·코어 검사 279개와 통합 등록·해제, CC 스테이징 실제 정렬, ZIP 검증 통과.
- 정식 설치본 0.3.39 적용. 설치 전후 사용자 환경설정 파일의 SHA-256 일치.

배포 ZIP은 `build/colmap_tools-0.3.39.zip`이며 SHA-256은 `6e77cf8534ee74c1b0bce7903fab318448144b77ad0a48136920f42ebe945857`입니다. 실제 설치본 측정은 `build/align-sidebar-installed.json`에 보관합니다. 실행 중인 Blender는 재시작한 뒤 새 Align 작업공간을 만들면 적용됩니다.
