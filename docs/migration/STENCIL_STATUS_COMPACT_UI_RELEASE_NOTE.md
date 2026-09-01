# Projection Paint Stencil 상태 한 줄 통합 0.3.25

## 변경 결과

- `Paint Setup` 아래의 중복 Stencil 상태 블록을 제거했습니다.
- 최상단 `Status`의 기존 Stencil 행에 상태와 짧은 원인을 함께 표시합니다.
- 새로운 행을 추가하지 않으므로 Status 영역의 세로 크기는 늘어나지 않습니다.

## 표시 예시

- `CONNECTED`: `Stencil: CONNECTED · 연결됨`
- `BRUSH_CHANGED`: `Stencil: BRUSH_CHANGED · 다른 Brush 활성`
- `CAMERA_SUSPENDED`: `Stencil: CAMERA_SUSPENDED · Camera View 밖`
- `MASK_SUSPENDED`: `Stencil: MASK_SUSPENDED · Mask 편집 중`
- `MANUAL_DISABLED`: `Stencil: MANUAL_DISABLED · 수동 해제`

## 보존 범위

- Stencil 연결 상태 판정
- 상세 상태 원인
- Reconnect와 Camera View 추적 동작
- Paint Target, 활성 Layer와 Blender Paint 도구

## 검증

- 최상단 Status가 압축된 Stencil 문구를 한 행으로 표시
- Paint Setup에서 중복 상태와 안내 행을 표시하지 않음
- Layer UI 회귀 42개 통과

수명주기 14개와 Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성 및 격리 설치본 검사를 통과했습니다. `build/colmap_tools-0.3.25.zip`을 실제 Blender `user_default`에 재설치하고 별도 Blender 프로세스에서 버전 0.3.25, `Stencil: BRUSH_CHANGED · 다른 Brush 활성` 문구와 Paint Setup 중복 제거를 확인했습니다. ZIP SHA-256은 `F7DAF389289ED85E66F98B65E97A6F02895E71291C32E90933779E8E89C94D50`, 크기는 28,677,516바이트입니다.
