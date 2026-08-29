# COLMAP Tools 0.1.6 Modifier F7 배경 토글 Release Note

## 해결한 문제

COLMAP Modifier에는 `Show Background Image` Property와 Camera Background 갱신 로직이 있었지만 이를 실행할 Operator와 F7 키맵이 없었다. 따라서 Modifier Camera View에서 F7을 눌러도 관리 Background Image가 켜지거나 꺼지지 않았다.

## 변경 내용

- `colmap.review_background_toggle` Operator를 추가했다.
- 활성 Modifier 세션의 3D Camera View이고 Camera Review Session이 존재할 때만 Operator poll을 허용한다.
- Blender 애드온 `3D View` 키맵에 F7 PRESS를 등록하고 Extension 해제·등록 실패 때 해당 항목만 대칭적으로 제거한다.
- Modifier 키맵을 선두에 배치해 통합 Extension에 함께 등록된 Projection Paint F7과 현재 세션 poll로 구분한다.
- 기존 `review_background_enabled` Property update 경로를 그대로 사용해 애드온이 소유한 Camera Background 슬롯만 갱신한다.

## 회귀 검증

- 설치 전 0.1.5 키맵 레지스트리에서 Modifier F7 항목이 0개임을 재현했다.
- Background에서 Modifier F7 키맵이 정확히 하나 등록되고 비활성 세션에서는 Operator가 poll되지 않으며 Extension 해제 뒤 항목이 남지 않음을 확인했다.
- Blender GUI에서 실제 F7 PRESS·RELEASE 이벤트를 두 번 보내 관리 Camera Background 슬롯이 OFF → ON으로 전환됨을 확인했다.
- Modifier Camera Review 전체 회귀, X/Delete 보호 키맵, Projection Paint F7 수명주기와 세 도구 Workspace 전환 행렬을 통과했다.

## 배포 파일

`build/colmap_tools-0.1.6.zip`은 `SmallGicp` Production 프로필이다. SHA-256은 `9D6EB3E4CED7497B6F16AF422BE6D06F0D0100BE7B8CD847FB3145437639D607`이다.
