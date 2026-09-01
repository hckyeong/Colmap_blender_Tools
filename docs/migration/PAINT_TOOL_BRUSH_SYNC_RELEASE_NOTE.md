# Projection Paint Blender 도구·브러시 활성화 0.3.14 Release Note

## 변경 내용

- Projection Paint 활성 세션에서 Blender Texture Paint 툴바 선택을 추적합니다.
- Draw, Blur, Smear, Clone, Fill, Mask 도구와 현재 Brush 종류가 다를 때만 Blender 5.2 Essentials의 실제 Brush 자산을 활성화합니다.
- 이미 같은 종류의 사용자 Brush가 활성화돼 있으면 바꾸지 않습니다.
- 도구 동기화 과정은 Brush를 새로 만들거나 복제하지 않습니다.
- 비Draw 도구로 전환하면 기존 Camera Stencil 연결만 안전하게 해제하며, Draw 복귀 뒤 Stencil은 기존 규칙대로 `Reconnect Stencil Texture`로 명시적으로 연결합니다.
- Projection Paint 작업공간과 실제 Texture Paint Target이 활성인 동안에만 동작하므로 다른 Blender 작업공간과 Paint 대상에는 간섭하지 않습니다.

## Blender Essentials 매핑

| Blender 도구 | Brush 종류 | 활성화 자산 |
| --- | --- | --- |
| Paint | `DRAW` | `Paint Hard` |
| Blur | `SOFTEN` | `Blur` |
| Smear | `SMEAR` | `Smear` |
| Clone | `CLONE` | `Clone` |
| Fill | `FILL` | `Fill` |
| Mask | `MASK` | `Mask` |

## 검증

- 매핑·비지원 도구 무변경·Brush 생성 및 복제 금지 자동 회귀 통과
- 기존 Paint Resources, Mask·Stencil Runtime, Layer, Workspace와 Stencil 자원 회귀 통과
- 사용자 `C:\Users\gksru\Desktop\Untitled.blend`를 저장하지 않고 실제 Blender 5.2 GUI에서 여섯 툴바 도구와 실제 Essentials Brush 종류·자산 경로 일치 확인
- GUI 검증 중 `COLMAP GUI` 이름의 검증용 Brush 생성 0개 확인

## 패키지

- 파일: `build/colmap_tools-0.3.14.zip`
- 크기: 28,676,215바이트
- SHA-256: `0691EF192174175F096F64273F7FB8A34A801FAEDB30F02725587248225436C2`
- 격리 설치본, Workspace GUI, Camera Marker GUI와 실제 `user_default` 재설치 통과
- 실제 설치본 `0.3.14`로 사용자 파일의 여섯 도구 전환과 최종 수동 Stencil 재연결 통과

설치 전에 열려 있던 Blender는 이전 Python Runtime을 메모리에 유지하므로 작업을 저장하고 재시작해야 `0.3.14`가 적용됩니다.
