# 고정 버전 재빌드

Windows x64와 Visual Studio 2022 C++17 도구, CMake 3.24 이상이 필요합니다. 의존성 URL·버전·원문 SHA-256은 `dependencies.lock.json`에 고정합니다. 버전이 다른 최신 의존성을 대신 사용하지 않습니다.

- Eigen 3.4.0: 공식 GitLab 태그 압축의 SHA-256은 `8586084f71f9bde545ee7fa6d00288b264a2b7ac3607b974e54d13e7162c1c72`입니다. 추출한 `eigen-3.4.0` 폴더가 포함 경로입니다.
- nlohmann JSON 3.11.3: 잠금 커밋의 단일 `json.hpp`를 사용합니다. CMake가 헤더 SHA-256을 다시 검사합니다.
- oneTBB 2022.0.0: 고정 Windows 압축을 사용합니다. `find_package(TBB 2022.0.0 EXACT)`로 구성하며 `tbb12.dll`을 실행 파일 옆으로 복사합니다.

저장소의 준비 도구로 런타임과 원문 묶음을 만들 수 있습니다.

```powershell
python -X utf8 build_tools/point_kd_distribution.py prepare
```

도구의 실제 인수는 `--help`로 확인합니다. 직접 CMake를 실행할 때는 검증해 추출한 경로를 명시합니다.

```powershell
$cmake = 'C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
& $cmake -S tools/align/native/point_kd_worker -B .point-kd-runtime/build `
  -G 'Visual Studio 17 2022' -A x64 `
  -DEIGEN_INCLUDE_DIR='<검증된 Eigen 3.4.0 폴더>' `
  -DJSON_INCLUDE_DIR='<고정 json.hpp가 있는 폴더>' `
  -DTBB_DIR='<oneTBB 2022.0.0>/lib/cmake/tbb' `
  -DCOLMAP_BUILD_POINT_KD_TESTS=ON
& $cmake --build .point-kd-runtime/build --config Release --parallel 4
& .point-kd-runtime/build/bin/colmap_point_kd_worker.exe --check-environment
& .point-kd-runtime/build/bin/test_point_kd_search.exe
& .point-kd-runtime/build/bin/test_point_kd_termination.exe
python -X utf8 tests/align/native/test_point_kd_worker.py `
  --worker .point-kd-runtime/build/bin/colmap_point_kd_worker.exe `
  --report build/verification/point-kd-native-tests.json
```

배포에서는 `colmap_point_kd_worker.exe`, `tbb12.dll`과 Visual C++ 재배포 런타임을 함께 제공해야 합니다. Eigen과 JSON은 헤더 전용이며 별도 DLL을 요구하지 않습니다. `licenses/`에 Eigen의 `COPYING.MPL2`, JSON의 `LICENSE.MIT`, oneTBB의 `LICENSE.txt`와 `third-party-programs.txt`를 보존합니다.

독립 검색 검사는 정확 KD 결과를 전수 검색 500회와 비교하고 반경·적격점·동률·자기 제외·취소를 검사합니다. Python 실행 검사는 알려진 강체 변환, 두 풀이 방식, 1·4 스레드 재현성, 큰 좌표 원점, 변환 후 삼각형 법선, 퇴화·누락·입력 오류·반경 경계, 원본 불변성, 실행 중 취소와 Windows 진행 파일 잠금 해제를 검사합니다.
