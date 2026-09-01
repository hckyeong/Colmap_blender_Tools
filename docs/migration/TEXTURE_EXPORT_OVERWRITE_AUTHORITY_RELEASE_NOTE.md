# Projection Paint Texture Export 덮어쓰기 권한 통합 0.3.29

## 원인

Texture Export Operator는 Blender `ExportHelper`의 숨겨진 `check_existing` 확인과 애드온의 `Overwrite Existing`을 동시에 사용하고 있었습니다. Blender 파일 브라우저에서 덮어쓰기를 승인해도 애드온의 별도 `overwrite` 값은 `False`로 남아 기존 파일 검사에서 다시 거부될 수 있었습니다.

## 변경 사항

- Blender 파일 브라우저의 `check_existing` 기본 확인을 비활성화했습니다.
- 사용자가 직접 확인할 수 있는 `Overwrite Existing`을 유일한 덮어쓰기 권한으로 고정했습니다.
- 기존 파일 Backup, 전체 출력 확정과 실패 시 Rollback Transaction은 그대로 유지했습니다.
- SINGLE뿐 아니라 여러 UDIM 출력도 같은 옵션 하나로 전체 묶음을 처리합니다.

## 검증

- Operator RNA의 `check_existing=False`·숨김 상태 확인
- `Overwrite Existing=False`·표시 상태 확인
- 기존 UDIM 묶음 전체 교체 확인
- 중간 확정 실패 시 기존 묶음 전체 복구 확인
- Backup 정리 실패와 확정 성공 상태의 분리 보고 확인
- Texture Export Blender Background 24개 통과

Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성과 격리 설치본 검사를 통과했습니다. `build/colmap_tools-0.3.29.zip`을 실제 Blender `user_default`에 설치하고 별도 Blender 프로세스에서 버전 0.3.29와 단일 덮어쓰기 권한 RNA를 확인했습니다.

Production ZIP SHA-256은 `608BACE711D89CEAD4BCA3D3314750F5D8A7DAA1D0E66C36AC7F56E046C03BF7`, 크기는 28,679,508바이트입니다.
