# 패키지 구조

```text
__init__.py
blender_manifest.toml
shell/
tools/
├─ align/
├─ modifier/
└─ projection_paint/
shared/
├─ blender_runtime/
├─ colmap_transfer/
└─ colmap_dataset/  # 0.2.0 읽기 전용 Legacy 마이그레이션 입력
tests/
docs/
build_tools/
```

Root `__init__.py`는 `shell.lifecycle`만 호출합니다. Root Shell은 도구의 구체 기능을 실행하지 않으며 각 도구가 공개한 `register()`와 `unregister()`만 호출합니다.

각 도구의 Blender 계층은 같은 도구의 Core와 공용 계약만 사용합니다. 세 도구 사이 직접 Import는 Architecture 테스트에서 금지합니다. `workspace_factory.py`는 Workspace 생성·소유권·삭제를, `workspace_handoff.py`는 등록된 도구 생명주기의 종료 완료와 다음 진입 순서만 담당합니다.

`shared/colmap_transfer`는 Blender ID가 없는 불변 값 Snapshot과 Provider Registry만 제공합니다. Modifier가 현재 확정 Reconstruction의 값을 제공하고 Projection Painting은 사용자가 업데이트 버튼을 누른 시점에만 복사합니다. `shared/colmap_dataset`은 기존 `0.2.0` 파일을 자동 삭제하지 않기 위한 Legacy 입력으로만 남습니다. Align은 두 패키지에 의존하지 않습니다.

`tools/modifier/core/edit_delta.py`, `edit_journal.py`, `domain_persistence.py`는 Blender와 분리된 편집 Delta·전용 Undo/Redo Journal·저장 Codec을 소유합니다. `tools/modifier/blender/modifier_runtime_domain.py`는 Domain Registry와 Scene의 일시 연결만 관리하고, `modifier_project_store.py`와 `modifier_persistence_handlers.py`는 Active/Staging Text 저장 경계를 담당합니다. Modifier Scene 요소와 Timeline은 Domain 결과에서 재생성 가능한 파생 표시입니다.

`tools/modifier/blender/camera_review_authority.py`는 Modifier Camera 쓰기 권한의 공통 검증 경계입니다. Timeline과 Viewport는 서로를 참조하지 않고 이 권한 경계만 사용해 현재 Window·Workspace·Scene Marker 사용권이 일치할 때만 Scene Camera를 변경합니다.

0.3.37의 Align 기본 정렬은 CC v3입니다. `shell/preferences.py`의 Root 환경설정 한 곳에서 CPU 상한을 저장하고 Align의 스레드 해석 함수를 사용합니다. 패키지의 `tools/align/runtime/python/`은 외부 Python 조정자, `tools/align/runtime/cccore/`는 네이티브 세션 워커·DLL·라이선스·재빌드 원문을 담습니다. 한 Align 작업의 순차 네이티브 명령이 같은 프로세스와 형상 캐시를 재사용하며 Blender 안에는 CC DLL을 적재하지 않습니다. 개발 캐시 `.cccore-runtime/`과 검증기·결과는 배포 ZIP에서 제외합니다.
