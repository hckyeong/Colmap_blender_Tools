# Projection Paint Camera Frame Source 최종 대기 0.3.21

## 변경 사항

- Timeline Camera Frame을 빠르게 이동하는 동안에는 마지막 Frame 요청만 저장합니다.
- 마지막 Frame 입력 후 0.30초 동안 추가 입력이 없을 때 최종 Camera와 Source Image를 한 번만 연결합니다.
- 정상적인 Frame 이동을 Camera Guard 오류 복구 횟수와 분리했습니다.
- Source Image 연결이 실패하면 기존 Source Pointer와 Metadata, Render 해상도를 복구하고 동일 최신 요청만 최대 2회 재시도합니다.
- 더 최신 Frame이 입력되면 이전 재시도를 폐기하고 새 최종 Frame 기준으로 대기 시간을 다시 계산합니다.
- 사용자가 누른 `Reconnect Stencil Texture`는 대기 시간을 건너뛰고 현재 Frame Source를 즉시 연결합니다.

## 유지되는 동작

- Camera Stencil 외의 Blender Paint Tool, Brush, Clone Source 위치, Paint Target, Layer와 Undo에는 개입하지 않습니다.
- Camera View 이동·확대와 Camera View 이탈·복귀 Stencil 추적은 기존 동작을 유지합니다.
- Modifier, Align, 공용 Dataset과 Projection Paint 독립 Snapshot 구조는 변경하지 않습니다.

## 검증 범위

- 0.30초 안의 20회 Frame 변경에서 중간 Source Commit 0회
- 마지막 입력 후 0.30초 경과 시 최종 Source Commit 1회
- 새 Frame 입력에 따른 대기 시간 재시작과 이전 재시도 폐기
- Camera·Marker·IMAGE_ID·Source Image Pointer 최종 일치
- 실패 시 기존 Source와 Render 상태 원자 복구
- 정상 Frame 변경 중 Camera Guard 복구 횟수 0 유지
- Tool·Brush·Paint Target·활성 Layer 무변경
- 사용자 `.blend`를 저장하지 않는 실제 설치본 GUI 회귀

Blender Background 대상 회귀 91개와 Production 검사 220개, Extension 등록·해제, 내장 `small_gicp` Runtime 합성, 격리 설치본과 Workspace·Camera Marker GUI 검사를 통과했습니다. 실제 `user_default` 설치본으로 사용자 `Untitled.blend`를 저장하지 않고 열어 Frame 20회 빠른 이동 뒤 최종 Frame 20·IMAGE_ID 20 연결, Camera Guard 복구 횟수 0, Tool·Brush·Paint Target·활성 Layer 불변을 확인했습니다. 이어서 Camera View 이동·확대·이탈·복귀, Draw → Blur → Smear → Clone → Fill → Mask → Draw 왕복과 실제 Camera Texture Stroke도 통과했습니다.

Production 패키지는 `build/colmap_tools-0.3.21.zip`이며 SHA-256은 `F292251C8AF155104C66C885E3437DF81A3CD86DEE3D08A40A8BA2DD1756E403`, 크기는 28,671,284바이트입니다. 실제 Blender `user_default`에 0.3.21을 설치하고 별도 Blender 프로세스에서 설치본 버전을 확인했습니다. 이미 열려 있던 Blender 프로세스는 재시작한 뒤 0.3.21 Runtime을 사용합니다.
