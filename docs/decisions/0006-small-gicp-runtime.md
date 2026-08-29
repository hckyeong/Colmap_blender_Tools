# ADR-0006: Small GICP Native Runtime 배포와 Process 격리

- 상태: 채택
- 결정일: 2026-08-27

## 배경

Align의 Production 배포는 Blender Process 안에 Native 모듈을 직접 Import하지 않고 외부 CPython Worker로 `small_gicp`를 실행합니다. 기존 Align 프로젝트에는 이 방식으로 검증된 이동식 Windows x64 런타임과 빌드 절차가 이미 존재합니다. 통합 Extension은 이 자산을 새 의존성으로 다시 설계하지 않고 동일한 계약으로 이어받아야 합니다.

## 결정

- 통합 기본 배포 프로필은 `SmallGicp`이며 Align 내부 값은 `SMALL_GICP`로 고정합니다.
- 내장 런타임은 CPython 3.12.10, NumPy 1.26.4와 small_gicp 1.0.1 Windows x64 조합으로 고정합니다.
- Native `small_gicp`는 Blender Python에 Import하지 않고 내장 `python.exe`의 별도 Process에서 실행합니다.
- 대용량 런타임은 Git과 통합 Runtime Source에 포함하지 않고 Build Staging의 `tools/align/runtime/python/`에만 복사합니다.
- 기존 Align 런타임은 마이그레이션 기간에 읽기 전용 빌드 입력으로 재사용할 수 있습니다. 없으면 통합 Root의 Git 제외 캐시에 같은 계약으로 준비합니다.
- 공식 CPython Embeddable ZIP은 고정 SHA-256으로 검증합니다. NumPy와 small_gicp 버전, Runtime Manifest, 필수 Native 파일과 라이선스를 검사합니다.
- Staging과 격리 설치본에서 각각 합성 GICP를 실행해야 배포를 통과합니다.
- `SmallGicp` ZIP에서는 Open3D·Hybrid 선택형 구현, Open3D Runtime과 `pyvenv.cfg`를 제외합니다.
- `Full`은 선택형 Backend 개발용 프로필로 유지하며 내장 Small GICP 런타임을 포함하지 않습니다.

## 결과

Blender와 Native 모듈의 장애 경계가 Process로 분리되고, 사용자는 별도 Python 설치 없이 Production ZIP을 사용할 수 있습니다. 대신 기본 배포는 현재 Windows x64로 제한되고 ZIP 크기가 증가합니다.

자동 검증은 패키징, 고정 버전, 이동성과 최소 합성 연산을 보장합니다. 실제 COLMAP Dataset의 Align GUI 전체 회귀, 대형 점군 성능, 취소·종료와 Native 예외 격리는 별도 후속 검증으로 유지합니다.
