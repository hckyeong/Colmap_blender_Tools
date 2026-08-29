# COLMAP Tools 0.1.7 F7 세션 격리 Release Note

## 해결한 문제

통합 Extension에서 Modifier와 Projection Paint가 모두 F7을 Camera Background 전환에 사용한다. 기존 Projection Paint 오퍼레이터는 Camera View 여부만 검사했기 때문에 COLMAP Modifier Workspace에서도 실행 가능했고, 실제 F7 입력을 먼저 소비해 Modifier의 `Show Background Image`가 전환되지 않았다.

MCP로 연결한 사용자 Blender에서 F7을 실제 Window 메시지로 전송해 Modifier 속성과 Camera Background 슬롯은 바뀌지 않고 최근 실행 오퍼레이터가 `PROJECTION_PAINT_OT_review_background_toggle`로 기록되는 것을 확인했다.

## 변경 내용

- Modifier F7은 기존과 같이 활성 Modifier 세션, Modifier Workspace와 Modifier Camera Review Session을 모두 확인한다.
- Projection Paint F7은 현재 Window가 해당 Scene의 `paint_workspace`를 표시할 때만 허용한다.
- 두 도구는 같은 F7 키를 유지하지만 다른 도구의 Workspace에서는 서로의 오퍼레이터가 `poll=False`가 되므로 키맵 등록 순서에 의존하지 않는다.
- Camera Background 표시 속성과 각 도구의 기존 갱신 경로는 변경하지 않는다.

## 회귀 검증

- 기존 코드에서 외부 Workspace의 Projection Paint F7 `poll()`이 `True`가 되는 실패를 재현했다.
- 수정 후 Projection Paint Workspace의 Camera View에서만 `poll=True`, Modifier·일반 Workspace와 Perspective View에서 `poll=False`임을 확인했다.
- 0.1.7 설치 후 MCP로 연결한 실제 Blender의 Modifier Workspace에서 Projection Paint `poll=False`, Modifier `poll=True`를 확인했다.
- 실제 Windows F7 입력으로 실행 오퍼레이터가 `COLMAP_OT_review_background_toggle`인지, Modifier 속성이 OFF로 바뀌고 현재 Review 관리 슬롯 71·72·73만 함께 OFF가 되는지 확인했다.
- Projection Paint Workspace에서는 Projection Paint `poll=True`, Modifier `poll=False`이며 Projection 속성만 바뀌고 Modifier 속성이 유지됨을 확인했다.
- 테스트 후 Modifier Workspace, Camera View, Review Image 72와 Background ON 상태를 복구했다.

## 배포 파일

`build/colmap_tools-0.1.7.zip`은 `SmallGicp` Production 프로필이다. SHA-256은 `CB3F3F7231574F8D8F7814C0EB669CCF53B95094394E29D4F63D3FC28BE638A8`이다.
