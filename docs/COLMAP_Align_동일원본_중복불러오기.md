# Align 동일 원본 중복 불러오기

## 요구와 원인

2026-09-18 사용자 요청: 같은 데이터를 여러 번 불러와 독립 레이어로 사용한다. 기존 저장소가 정규 원본 경로를 유일 키로 취급해 OBJ는 준비를 마친 뒤 등록 시 거부하고, COLMAP은 읽기 전에 거부했다.

## 변경 범위와 계약

- OBJ·COLMAP 같은 원본 경로의 반복 불러오기를 허용한다. 매번 새 모델 ID와 독립 배열·변환을 생성한다.
- OBJ 및 연결 OBJ는 매번 별도 내장 Mesh와 자산 UUID를 가진다. 원본 공유·캐시·메모리 절약 기능은 이번 범위에 넣지 않는다.
- 표시 이름은 기존 이름 충돌 처리와 동일하게 `_2`, `_3`을 붙인다. 저장된 이름과 모델 ID는 복원 시 유지한다.
- 기존 `source_path_index: dict[str, str]`와 단일 ID 조회 API를 유지한다. 각 경로에서 가장 먼저 등록되어 남아 있는 모델 ID를 대표로 저장한다. 대표가 삭제되면 다음 모델로 교체하고 마지막 모델이 없어지면 경로를 제거한다.
- 모델 ID 중복, 형상·메타데이터 검증과 실패 시 원자적 복구는 유지한다. 파일 경로는 출처이며 레이어 식별자는 모델 ID다.
- 저장 형식은 기존 schema v8을 유지한다. OBJ는 원본 파일 없이 내장 자산으로 복원하고 COLMAP은 기존 외부 원본 검증을 유지한다.
- 내보내기에서 생성 이름은 구분하며 원본 경로 덮어쓰기 방지와 사용자가 직접 만든 출력 이름 충돌 검사는 유지한다.

경로 대표 선택은 Python 사전의 삽입 순서를 이용한다. [Python 공식 사전 문서](https://docs.python.org/3/library/stdtypes.html#mapping-types-dict)를 기준으로 기존 사전 API를 유지한다.

## 구현과 검증

원본 경로 중복 거부를 제거하고 모델 ID에 따라 등록한다. 독립 OBJ에도 저장소의 기존 이름 충돌 처리를 적용한다. 등록 실패·삭제 시 경로 대표를 다른 생존 레이어로 갱신하며, 삭제 저장 실패 시 모델 순서와 대표 색인을 원상 복구한다. 기존 ID가 충돌하면 내장 자산 확정과 GPU 준비 전에 거부해 기존 캐시를 보존한다. 연결 OBJ의 부모 ID 사용은 유지한다.

Blender 5.2에서 다음 검사를 통과했다.

- `test_duplicate_sources.py`: 같은 OBJ 세 번 가져오기, 독립 배열·내장 자산, 등록 실패 보존, 삭제 순서 및 삭제 실패 복구, 실제 `.blend` 저장·재열기, 외부 OBJ 삭제 후 각 변환·표시 상태 복원, 고유 이름으로 OBJ 내보내기 등 3개 통합 검사.
- `test_import_runtime.py`의 관련 3개 검사: COLMAP 반복 읽기와 독립 대기 레이어, COLMAP·OBJ ID 충돌 시 기존 GPU 캐시·자산 보존.
- `test_companion_import.py`의 11개 검사: 같은 데이터셋과 연결 OBJ 반복 가져오기, 등록 실패·삭제 시 다른 레이어 보존 포함.
- 기존 내장 OBJ, 연결 OBJ 저장·복원, 프로젝트 복원, OBJ 내보내기 검사 파일 전체.
- 같은 COLMAP 원본을 두 번 가져온 뒤 저장 상태에서 새 Runtime으로 타이머 복원해 서로 다른 ID·이름·정렬 행렬·A/P 상태와 출처 대표가 유지되는지 확인했다. 추가 확인 로그는 배포 검증 폴더의 `check_duplicate_restore.log`다.
- 정식 빌드의 구조·공존·Core·Blender 등록·네이티브 정렬·ZIP 검사와 격리 설치본의 네 가지 정렬 입력 조합·OBJ/UV 입출력 검사.

`test_import_runtime.py` 전체에는 기존 축 프리셋 문구 기대 불일치가 있다. 테스트는 `RealityCapture (RC), Blender, Maya, Houdini, Custom`을 기대하지만 기존 HEAD부터 실제 값은 `RC COLMAP, RC 표준 OBJ, 원본 축 유지, Maya, Houdini, 직접 설정`이다. 상태 표시 접두사 기대도 같은 이유로 오래된 값이다. 이번 변경 범위에 포함하지 않았으며 전체 검사 파일 통과로 보고하지 않는다.

## 배포

2026-09-18 정식 CCCoreLib 프로필로 `build/colmap_tools-0.3.52.zip`을 생성했다. 이전 0.3.51 ZIP 대비 버전 파일 두 개와 Align의 `importer.py`, `mesh_importer.py`, `model_repository.py`, `operators/model_group.py`만 달라졌다.

- ZIP SHA-256: `9f31c59969caaf8029eaf58f6421ef9032f2ed356579861860f502f728339617`.
- 로컬 빌드·설치·실파일 검증 산출물: `build/verification/duplicate-sources-0.3.52/`.
- Blender 5.2의 실제 `user_default` 저장소에 설치하고 새 프로세스에서 버전 0.3.52 활성화 및 배포 파일 1,170개의 해시 일치를 확인했다. 사용자 환경설정 파일은 설치 전후 SHA-256이 동일하다. 기존에 열려 있는 Blender는 저장 후 재시작해야 새 코드를 사용한다.

## 실제 대용량 OBJ 중복 불러오기

실제 설치본의 별도 숨김 GUI 프로세스에서 사용자가 지정한 `Taiwan_armadillo_scanHighpoly.obj`를 공개 `colmap_align.import_obj` 오퍼레이터와 실제 이벤트 타이머로 두 번 가져왔다. 최종 확인에서 첫 번째는 27.872초, 두 번째는 27.458초였으며 이는 각 오퍼레이터 완료를 타이머에서 관측한 시간이다. 디스크 캐시를 비우지 않은 단일 실행 결과이며 저장 시간이나 통계 평균이 아니다.

두 레이어 모두 정점 10,173,478개·삼각면 20,128,488개이며 이름은 `taiwan_armadillo_scanhighpoly`, `taiwan_armadillo_scanhighpoly_2`다. 서로 다른 모델 ID·내장 UUID·정점 배열과 두 뷰포트의 GPU 표면 표시를 확인했다. Windows 원본 경로 정규화에 따른 소문자 표기는 기존 동작을 유지한다. 원본 파일 크기·수정 시각도 보존됐다.

결과는 `large-gui-result.json`, 실제 화면은 `large-duplicate-viewport.png`에 보관한다. 첫 실행은 두 번 가져오기 후 검사 스크립트의 대소문자 기대에서 실패했으며, 제품 코드 변경 없이 그 기대를 수정해 전체 검사를 재실행했다. 현재 열려 있는 사용자 작업은 변경하지 않았다.
