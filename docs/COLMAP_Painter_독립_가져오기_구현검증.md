# Painter 독립 가져오기 구현·검증

- 검증일: 2026-10-05
- 구현 버전: 0.3.73, `main` 작업 사본
- 비교 기준: 0.3.72, 커밋 `48afa7d6a3bc939194208b3b7d544d46d1ef7e18`
- 환경: Windows x64, Blender 5.2.0 LTS
- 대상 계획: [독립 가져오기와 공통 파일 읽기 계층](COLMAP_Painter_독립_가져오기_공통IO_계획서.md)

## 1. 구현 결과

Painter의 Dataset에 **COLMAP 불러오기 / OBJ 불러오기**를 연결했다. Modifier나 Align을 먼저 실행하지 않아도 파일을 직접 읽는다. **Modifier에서 업데이트** 버튼·오퍼레이터·제공자 자동 등록을 제거했다. Modifier의 가져오기·편집·내보내기 기능은 유지한다.

| 변경 | 구현 위치와 동작 |
|---|---|
| 공통 COLMAP 읽기 | `shared/colmap_io/`. 기존 Align 파서 실체를 이동하고 기존 `tools/align/colmap_io/` 경로는 호환 모듈로 유지 |
| 검증 경계 | Align의 전체 관측 무결성 검사를 보존. Painter는 기본 구조 검사와 자체 투영·이미지·점 검사를 수행 |
| 공통 OBJ 가져오기 | `shared/blender_runtime/obj_import.py`. Blender 기본 가져오기·호출 문맥 복원·새 자원 정리만 공통화 |
| Painter 직접 COLMAP 입력 | `core/colmap/shared_reader.py`, `core_adapter.py`, `camera_import.py`. 공통 증분 읽기를 준비·검증·확정 흐름과 연결 |
| 대형 점군 복제 억제 | 읽기 전용 배열을 지연 레코드로 연결. 점·트랙의 대형 튜플과 정렬된 식별자 목록 중복 생성을 피함 |
| Painter OBJ 입력 | `model_import.py`. 새 메시·재질·컬렉션·축 루트를 Painter가 독립 소유하고 원본 폴리곤·UV를 유지 |
| 등록·UI | `lifecycle.py`, `panels.py`, `properties.py`. 파일·폴더 선택, 명시적 경로, 원본 축, 진행·취소, 입력 수 표시 |
| 저장 호환 | 새 입력은 `FILE`, 이전 입력은 `MODIFIER_LEGACY`로 구분. 이전 저장 자산·레이어의 복원 계약을 보존 |

COLMAP 재입력은 카메라·점군만 교체한다. 추가 OBJ 입력은 새 오브젝트를 만들며 기존 페인트 대상·레이어·이미지 픽셀을 자동 교체하지 않는다. 실행 상태와 Blender 오브젝트를 도구 간 공유하지 않는다. Blender가 동일 외부 파일의 이미지를 재사용하는 경우에는 기존 이미지의 소유 태그·픽셀을 변경하지 않고 읽기 참조를 유지한다.

직접 입력은 공통 파서를 사용한다. 이전 Painter 파서의 공개 함수는 기존 호출·테스트 호환용으로 남겼으며, 직접 입력 실패를 우회하는 대체 파서로 사용하지 않는다. Painter 투영 지원은 기존 `SIMPLE_PINHOLE`·`PINHOLE` 범위를 유지한다.

## 2. 통과한 검증

| 검사 | 결과 |
|---|---|
| 정식 빌드의 구조·공존·핵심 로직 | 306건 통과: 구조 17, 공존 11, Modifier 235, Painter 43 |
| Align 공식 소스 회귀 | 45개 테스트 파일 모두 통과. COLMAP·OBJ 입력, 중복 입력, 축·마커·저장·복원 포함 |
| 직접 COLMAP 입력 | 6건 통과. Modifier 없이 확정, 출처 기록, 취소·오류 시 기존 자산 보존 |
| 직접 OBJ 입력 | 실제 Blender 검사 통과. 반복 입력의 독립 메시·재질·축, 폴리곤·UV·텍스처 보존, 선택 문맥 복원 |
| 실패 정리 | 파일 변경, 손상 입력, 축 오류, 가시성·확정 실패를 주입하고 새 자원 정리·기존 페인트 데이터 보존 확인 |
| Modifier 공존 | Modifier를 독립 실행한 상태에서도 Painter 파일 입력·재입력·실패 복구 통과 |
| Painter 생명주기·진행 | 등록 검사 14건, 증분 진행 검사 2건 통과 |
| 기존 카메라·표시·작업공간 | 카메라 검토 19건, 점 표시 3건, 작업공간 12건, 카메라·모델 축 검사 통과 |
| 텍스처 출력 | 실제 베이크를 포함한 34건 통과 |
| 실제 화면 입력 | 폴더 선택 확정 후 실제 모달 가져오기·카메라·촬영 이미지 연결·OBJ 표시·COLMAP 재입력 검사 통과 |
| 기존 저장 파일 | 0.3.72로 만든 10만 점·페인트 레이어 파일을 새 Blender 프로세스에서 복원. 이전 버전 복원 결과와 픽셀 배열 동일 |
| 신규 저장 파일 | 파일 출처·10만 점·페인트 픽셀 복원 통과. 원본 OBJ가 없는 상태에서도 저장된 메시·UV·축 루트 복원 |
| 정식 ZIP | 버전·런타임·등록·실제 정렬·ZIP 구조 검사 통과 |
| 격리 설치 | 새 사용자 영역에 ZIP을 설치하여 등록·속성·실제 CC 정렬·OBJ 입출력·해제 검사 통과 |

실제 화면 검사는 분리된 Blender를 숨김 창으로 실행했다. 사용 중인 Blender 프로젝트를 저장하거나 덮어쓰지 않았다. 새 화면에서 직접 브러시 획을 그리는 검사는 이번에 수행하지 않았으며, 기존 페인트 데이터 보존과 텍스처 출력 검사를 별도로 수행했다.

## 3. 성능 측정

### 동일 COLMAP 입력 비교

비교 자료는 검증 도구가 만든 **TXT 점 100,000개, 이미지 16개, 카메라 내부 파라미터 1개**의 정상 관측 관계와 촬영 이미지다. 사용자 실측 COLMAP 프로젝트의 수치가 아니다. 이전 버전 소스를 별도 디렉터리에 보존해 같은 입력과 같은 Blender로 비교했다.

| 측정 범위 | 이전 구조 | 새 구조 |
|---|---:|---:|
| 최초 전체 입력 | Modifier 1.08초 + Painter 업데이트 2.36초 = **3.43초** | Painter 직접 입력 **3.39초** |
| 두 번째·세 번째 실행 평균 | 이미 준비된 Modifier의 업데이트 **2.32초** | 파일을 다시 읽는 직접 입력 **3.37초** |
| 3회 실행 동안 프로세스 최대 메모리 | **465.8 MiB** | **360.7 MiB** |

최초 전체 입력 시간은 비슷했고, 이 조건에서 최대 메모리는 **22.6% 감소**했다. 이미 메모리에 있는 Modifier 데이터의 업데이트와 새 파일 읽기는 수행 범위가 다르므로, 재입력 시간만으로 동일 비용의 속도 개선을 주장하지 않는다.

최초 실행은 새 Blender 프로세스에서 측정했지만 운영체제 파일 캐시를 비우지는 않았다. 메모리는 Windows 프로세스의 최대 작업 집합이며 파이썬 할당량만 측정한 값이 아니다. 각 구조를 한 프로세스에서 세 번 실행한 최대값을 비교했다.

새 구조 첫 실행의 주요 구간은 공통 읽기 약 0.52초, 점 검증 0.55초, 의미 지문 0.44초, Blender 자산 준비 0.42초다. 확정 직전 재검증에는 읽기·점 검증·지문 계산 약 1.45초가 추가된다. 이 재검증은 원본 변경을 막는 기존 확정 계약을 유지한 결과이며 이번에 생략하지 않았다. 다음 속도 개선을 검토한다면 이 구간의 중복 작업과 검증 계약을 함께 검토해야 한다.

실제 화면의 모달 입력은 같은 10만 점 자료에서 **10.94초**였다. 화면 이벤트·타이머를 포함한 이 수치는 백그라운드 계산 시간 3.39초와 구분한다.

### 실제 대용량 OBJ

| 항목 | 결과 |
|---|---:|
| 파일 | `Taiwan_armadillo_scanHighpoly.obj` |
| 파일 크기 | 921,654,682바이트, 약 922 MB |
| 정점 | 10,173,478개 |
| 면 | 20,128,488개 |
| Painter 가져오기·오브젝트 연결 | **7.16초** |
| 프로세스 최대 메모리 | 5,711,872,000바이트, 약 **5.32 GiB** |

원본 경로는 `C:/Users/gksru/Desktop/temp/Taiwan_bone_reference/Taiwan_armadillo_bone/Taiwan_armadillo_scanHighpoly.obj`다. 실제 파일을 읽기 전용으로 사용했고 정렬용 전체 배열·BVH를 만들지 않았다. 위 시간은 백그라운드 OBJ 입력이며 정렬·브러시 작업·화면 표시 완료까지 포함한 시간은 아니다.

## 4. 이전 버전에서도 재현되는 추가 검사 실패

공식 빌드와 Align 회귀는 통과했다. 추가로 실행한 Painter 회귀 중 아래 15건은 실패했으며, 같은 테스트를 0.3.72 소스에서도 실행해 동일한 실패를 확인했다. 이번 구현의 신규 회귀로 집계하지 않지만 전체 테스트가 모두 통과했다고 보고하지 않는다.

| 테스트 파일 | 실패·오류 | 확인 내용 |
|---|---:|---|
| `test_camera_import_atomicity.py` | 2건 / 9건 | 비활성 작업공간의 카메라 쓰기 권한과 마커 복구 기대값 |
| `test_stencil_resources.py` | 8건 / 25건 | 활성 카메라 세션이 없는 시험 문맥의 스텐실 연결 |
| `test_layer_shader.py` | 4건 / 47건 | 레이어 선택 시험 중 활성 카메라 세션이 필요한 연결 |
| `test_sessionless_state.py` | 1건 / 3건 | 세션 없는 시험 문맥의 스텐실 재연결 |

해당 검사와 기존 카메라 권한 계약의 정비는 이번 입력 구조 변경 범위에서 분리했다. 이를 통과시키기 위해 제품의 권한 검사를 우회하지 않았다. 별도 추가 실행한 Align `test_import_runtime.py`의 기존 축 표시 문구 기대값 2건도 현행 `RC` 표시와 불일치한다. 이 파일은 공식 45개 회귀 목록에 포함되지 않는다.

## 5. 산출물과 재현

- 정식 설치 파일: 저장소 최상위 `colmap_tools.zip`, 내부 버전 **0.3.73**.
- 크기: **48,286,457바이트**. 입력 UI 단순화·기본 페인트 도구 영역 복구와 [페인트 진입·카메라 배경 표시 수정](COLMAP_Painter_페인트_진입_해상도_표시_수정.md), [카메라뷰 원근투영 복귀](COLMAP_Painter_카메라뷰_원근투영_복귀.md), [페인트 대상 단독 표시](COLMAP_Painter_페인트대상_단독표시.md), 별도 카메라 진입 버튼 숨김 및 [참고 점 제한·가져오기 속도 개선](COLMAP_Painter_COLMAP_가져오기_속도개선_구현검증.md), [Modifier 눈금 격리 보완](COLMAP_Modifier_미터눈금_후발레이어_격리.md)까지 반영한 최신 ZIP 기준이다. 아래 설치 이력은 당시 수행 결과이며 최신 ZIP 설치 완료를 뜻하지 않는다.
- SHA-256: `7a7e4cdea75696e6889fa231a2c35613153db01e70b8cdce8a63fcf16bc2de3d`.
- 사용자 매뉴얼: [Painter](https://github.com/hckyeong/Colmap_blender_Tools/blob/f5c816d335cba5ef9808919cacbda73edb90a931/docs/manual/PROJECTION_PAINT.md), [공통 설치·도구 전달](https://github.com/hckyeong/Colmap_blender_Tools/blob/f5c816d335cba5ef9808919cacbda73edb90a931/docs/manual/README.md).
- 원시 검증 결과: `build/verification/painter-file-import-20261005/`. Git에서 제외된 로컬 검사 산출물이다.

주요 결과 파일은 `baseline.json`, `final-benchmark/direct.json`, `large-obj/obj.json`, `gui.json`, `align-source/report.json`, `installed/verification-report.json`이다. 이전 실패 비교는 `baseline-atomicity.log`, `baseline-stencil.log`, `baseline-layer-shader.log`, `baseline-sessionless.log`에 남겼다. 복원 결과는 `legacy-restore-baseline/`, `legacy-restore/`, `new-restore/`, `model-restore/`에 보관한다.

재현용 입력·화면 검사 코드는 `tests/projection_paint/benchmark_file_import.py`, `tests/projection_paint/gui_file_import.py`다. 정식 빌드는 `build_tools/build_extension.ps1`, 설치 검사는 `build_tools/test_installed_extension.ps1`, Align 회귀는 `build_tools/test_align_source.ps1`로 실행했다. 제품에 일회성 계측 로그를 추가하지 않았다.

코드 구현·검증·최상위 ZIP 빌드·격리 설치 검사를 완료했다. 커밋·푸시는 진행하지 않았다.

### 실제 사용자 프로필 설치 · 2026-10-05

후속 설치 요청에 따라 Blender 5.2의 `user_default` 저장소에 있던 0.3.72를 0.3.73으로 교체하고 활성화했다. 설치 전에 Blender가 종료된 상태를 확인하고 기존 확장 폴더와 `userpref.blend`를 `build/verification/painter-file-import-20261005/live-install/backup-0.3.72/`에 백업했다.

Blender의 정식 `extension install-file --repo user_default --enable` 명령으로 설치했다. 새 프로세스에서 버전·활성화·직접 COLMAP·OBJ 입력 등록·이전 업데이트 오퍼레이터 제거를 확인했다. 다른 확장의 활성화 목록과 CPU 스레드 등 기존 환경설정이 유지됐고 설치된 파일 전체가 정식 ZIP과 일치했다. 결과는 같은 폴더의 `before.json`, `after.json`, `install.log`에 기록했다. 사용자 프로젝트는 열거나 저장하지 않았다.

### 입력 UI 단순화 · 2026-10-05

후속 요청에 따라 Dataset의 입력 영역을 **COLMAP 불러오기 / OBJ 불러오기** 두 버튼으로 줄이고 각각 한 줄씩 표시했다. 경로·파일 형식·원본 축·지정 경로 불러오기와 빈 입력 안내를 패널에서 제거했다. 기존 입력 오퍼레이터와 저장 속성은 유지하고, 입력 후 점·카메라 표시 설정도 유지했다.

UI 등록 회귀 14건과 정식 빌드 검사 306건을 통과했다. 0.3.73 설치 ZIP을 갱신했고, 기존 설치본과 ZIP의 차이가 `tools/projection_paint/blender/panels.py` 한 파일뿐인지 확인한 뒤 해당 파일을 백업·교체했다. 현재 열린 Blender에서는 이 패널의 그리기 함수만 갱신했다. 장면 오브젝트·레이어·페인트 상태가 그대로인지 확인하고 실제 화면에서 두 버튼 배치를 확인했다. 프로젝트를 저장하거나 확장 전체를 해제하지 않았다.

추가 검증은 `build/verification/painter-buttons-20261005/`의 `build.log`, `install.json`, `ui.png`에 보관한다. 설치 파일 전체를 갱신 ZIP과 대조해 일치를 확인했다. 최초 설치의 로그와 ZIP 지문은 당시 상태의 기록으로 유지한다.

### 기본 페인트 도구 영역 복구 · 2026-10-05

입력 버튼 변경은 뷰포트 설정을 수정하지 않았다. 기존 Align 기반 Painter 화면 템플릿에서 도구 막대·상단 도구 설정·브러시 선반이 모두 숨겨진 것을 확인했다. 같은 Blender 5.2의 기본 Texture Paint 작업공간은 세 영역 모두 표시 상태였다.

작업공간을 준비할 때 도구 막대와 상단 설정을 한 번 복구한다. 브러시 선반은 오브젝트 모드에서 읽기 전용이므로, 기존 페인트 도구 추적 타이머가 해당 영역을 사용 가능한 상태로 확인한 시점에 한 번 복구한다. 복구 완료 여부를 각각 저장해 이후 사용자의 숨김 선택을 유지한다. 표시 복구는 도구·브러시·모드를 강제 선택하지 않는다.

작업공간 회귀 12건, 브러시 연결 회귀 8건과 정식 빌드 검사 306건을 통과했다. 분리된 실제 Blender 화면에서 COLMAP 자료를 가져온 뒤 텍스처 페인트에 진입해 세 영역과 기본 `builtin.brush` 도구가 일반 Texture Paint와 일치함을 확인했다. 재구성 시 사용자 숨김 상태와 활성 브러시가 유지되는지도 확인했다. 이 화면 검사는 대상 모드 진입 함수와 도구 추적을 직접 호출했으며, Painting Mode 전체 진입 오퍼레이터나 실제 브러시 획의 성공 검증으로 집계하지 않는다.

추가 실행한 `test_paint_runtime_mask_stencil.py`의 26건은 준비 단계의 활성 카메라 세션 검사에서 실패했고, 보존한 0.3.72에서도 같은 26건이 실패했다. 이를 통과시키기 위해 제품의 세션 검사를 변경하지 않았다.

결과와 화면은 `build/verification/painter-buttons-20261005/`의 `native-tools.json`, `native-tools-ui.png`, `native-tools-workspace-tests.log`, `native-tools-bridge-tests.log`, `native-tools-build.log`에 기록한다. 이전 실패 대조는 `native-tools-runtime-tests.log`, `native-tools-runtime-baseline.log`에 남긴다.

Blender가 종료된 상태에서 설치본과 ZIP의 차이가 `paint_workspace.py`, `paint_runtime.py` 두 파일뿐임을 확인하고 백업·교체했다. 설치 파일 1,198개가 갱신 ZIP과 일치했고, 새 사용자 프로필 프로세스에서 수정 코드 로드·확장 활성화·기존 환경설정 보존을 확인했다. 사용자 프로젝트를 열거나 저장하지 않았다. 설치 결과는 `native-tools-install.json`, `native-tools-installed-probe.json`에 보관한다.

### 별도 카메라 진입 버튼 숨김 · 2026-10-06

스텐실 재연결과 카메라 진입 조작이 겹친다는 요청에 따라 Camera Review의 Look Through Camera 버튼 그리기만 주석 처리한다. 해당 행을 생성하지 않아 빈 공간도 없어진다. 내부 카메라 진입 함수·오퍼레이터 등록·프레임 전환·스텐실 재연결은 유지한다. 사용자 매뉴얼에서 숨긴 버튼을 사용하는 절차와 캡처 요구를 정리한다.

UI 등록 회귀 14건과 정식 빌드 검사 306건, 통합 등록·해제 및 CCCoreLib 실제 정렬 검사를 통과했다. 패널 파일 하나만 백업·교체했고 설치 1,198파일이 최신 ZIP과 일치한다. 실행 중인 Blender에서 Camera Review의 그리기 함수만 갱신했다. 현재 497번 프레임과 기존 페인트 이미지·레이어를 보존했고 실제 화면에서 버튼과 해당 행이 사라졌음을 확인했다. 프로젝트 저장과 커밋·푸시는 진행하지 않았다.

결과는 `build/verification/painter-hide-look-button-20261006/`의 `ui-registration.log`, `build.log`, `install.json`, `ui.png`에 보관한다. 제품에 일회성 디버그 로그를 추가하지 않았다.
