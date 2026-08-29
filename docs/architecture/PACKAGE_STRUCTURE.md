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
tests/
docs/
build_tools/
```

Root `__init__.py`는 `shell.lifecycle`만 호출합니다. Root Shell은 도구의 구체 기능을 실행하지 않으며 각 도구가 공개한 `register()`와 `unregister()`만 호출합니다.

각 도구의 Blender 계층은 같은 도구의 Core와 도구 의미를 모르는 `shared/blender_runtime` 계약만 사용합니다. 세 도구 사이 직접 Import는 Architecture 테스트에서 금지합니다. `workspace_factory.py`는 Workspace 생성·소유권·삭제를, `workspace_handoff.py`는 등록된 도구 생명주기의 종료 완료와 다음 진입 순서만 담당합니다.
