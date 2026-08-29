# 문서 안내

이 폴더는 통합 `COLMAP Tools` Extension의 현행 구조와 마이그레이션 계획을 설명합니다.

## 현행 문서

- [독립 도구 통합 계획서](COLMAP_Tools_독립도구_통합_계획서.md)
- [Workspace 격리 구조변경 계획서](COLMAP_Tools_Workspace_격리_구조변경_계획서.md)
- [Camera Marker 세션 격리 구조변경 계획서](COLMAP_Tools_Camera_Marker_세션격리_구조변경_계획서.md)
- [Align OBJ UV 보존 구조변경 계획서](COLMAP_Align_OBJ_UV_보존_구조변경_계획서.md)
- [제품 경계](architecture/PRODUCT_BOUNDARIES.md)
- [패키지 구조](architecture/PACKAGE_STRUCTURE.md)
- [독립 도구 안내](tools/README.md)
- [원본 Source 매핑](migration/SOURCE_MAPPING.md)
- [통합 구현 현황](migration/IMPLEMENTATION_STATUS.md)
- [Workspace 격리 0.1.1 Release Note](migration/WORKSPACE_ISOLATION_RELEASE_NOTE.md)
- [Workspace 자동 전환 0.1.2 Release Note](migration/WORKSPACE_HANDOFF_RELEASE_NOTE.md)
- [Projection Paint Workspace 유지 0.1.3 Release Note](migration/WORKSPACE_PERSISTENCE_RELEASE_NOTE.md)
- [Workspace 자유 이동 0.1.4 Release Note](migration/WORKSPACE_FREE_NAVIGATION_RELEASE_NOTE.md)
- [Modifier 데이터 격리 0.1.5 Release Note](migration/MODIFIER_DATA_VISIBILITY_RELEASE_NOTE.md)
- [Modifier F7 배경 토글 0.1.6 Release Note](migration/MODIFIER_F7_BACKGROUND_RELEASE_NOTE.md)
- [F7 세션 격리 0.1.7 Release Note](migration/F7_SESSION_ISOLATION_RELEASE_NOTE.md)
- [Camera Marker 세션 격리 0.1.8 Release Note](migration/CAMERA_MARKER_SESSION_RELEASE_NOTE.md)
- [Align 수동 종료 버튼 제거 0.1.9 Release Note](migration/ALIGN_EXIT_BUTTON_REMOVAL_RELEASE_NOTE.md)
- [Align N 패널 유지 0.1.10 Release Note](migration/ALIGN_PANEL_PERSISTENCE_RELEASE_NOTE.md)
- [파일 로드 후 Workspace 전환 복구 0.1.11 Release Note](migration/WORKSPACE_MSGBUS_RESTORE_RELEASE_NOTE.md)
- [Point·Camera 표시 UI 통합 0.1.12 Release Note](migration/DISPLAY_SETTINGS_UI_RELEASE_NOTE.md)
- [Point·Camera 표시 UI 여백 제거 0.1.13 Release Note](migration/DISPLAY_SETTINGS_COMPACT_RELEASE_NOTE.md)
- [Align 완료 후 대표 삼각형 숨김 0.1.14 Release Note](migration/ALIGN_PREVIEW_TRIANGLE_VISIBILITY_RELEASE_NOTE.md)
- [Modifier 세션 관리 데이터 단독 표시 0.1.15 Release Note](migration/MODIFIER_SESSION_EXCLUSIVE_VISIBILITY_RELEASE_NOTE.md)
- [Align OBJ UV 보존 0.1.16 Release Note](migration/COLMAP_ALIGN_OBJ_UV_PRESERVATION_RELEASE_NOTE.md)
- [Align OBJ 텍스처 왕복 0.1.17 Release Note](migration/COLMAP_ALIGN_OBJ_TEXTURE_ROUNDTRIP_RELEASE_NOTE.md)
- [Align OBJ Export 짧은 경로 0.1.18 Release Note](migration/COLMAP_ALIGN_OBJ_EXPORT_SHORT_PATH_RELEASE_NOTE.md)
- [Align OBJ Export 평면 배치 0.1.19 Release Note](migration/COLMAP_ALIGN_OBJ_EXPORT_FLAT_LAYOUT_RELEASE_NOTE.md)
- [통합 검증 방법](TESTING.md)
- [ADR-0001: 독립 도구 모음으로 통합](decisions/0001-independent-tools.md)
- [ADR-0006: Small GICP Native Runtime 배포와 Process 격리](decisions/0006-small-gicp-runtime.md)

기존 세 프로젝트의 과거 계획서와 결과 보고서는 원본 저장소에 보존되어 있으며, 통합 구조 검증이 끝난 뒤 `archive/`로 단계적으로 이전합니다.
