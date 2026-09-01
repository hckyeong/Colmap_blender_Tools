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
