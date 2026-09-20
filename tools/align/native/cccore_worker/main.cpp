// SPDX-License-Identifier: GPL-3.0-or-later
// CCCoreLib의 공개 API만 사용하는 독립 파일 기반 작업 프로세스입니다.
#include <DistanceComputationTools.h>
#include <DgmOctree.h>
#include <GenericProgressCallback.h>
#include <PointCloud.h>
#include <RegistrationTools.h>
#include <SimpleMesh.h>
#include <json.hpp>
#include "mesh_bvh.h"
#include <windows.h>
#include <psapi.h>
#include <concrt.h>
#include <cwctype>
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using Json = nlohmann::json;
using Clock = std::chrono::steady_clock;
using Cloud = CCCoreLib::PointCloud;
using Transform = CCCoreLib::PointProjectionTools::Transformation;
constexpr const char* kProtocol = "cccore-native-v2";
constexpr const char* kSessionProtocol = "cccore-native-v7";
constexpr const char* kThreadBackend = "oneTBB";
constexpr const char* kTbbVersion = TBB_VERSION_PIN;

struct ThreadSettings {
    unsigned requested = 1;
    unsigned effective = 1;
    unsigned hardware = 1;
};

struct NativeTimings {
    double binaryReadSeconds = 0;
    double conversionValidationSeconds = 0;
    double meshLoadSeconds = 0;
    double octreeSeconds = 0;
    double meshGridSeconds = 0;
    double bvhBuildSeconds = 0;
    double kernelSeconds = 0;
    double serializationSeconds = 0;
};

struct OperationCounters {
    std::uint64_t geometryLoadCount = 0;
    std::uint64_t geometryCacheHits = 0;
    std::uint64_t octreeBuildCount = 0;
    std::uint64_t octreeCacheHits = 0;
    std::uint64_t meshGridBuildCount = 0;
    std::uint64_t meshGridCacheHits = 0;
    std::uint64_t bvhBuildCount = 0;
    std::uint64_t bvhCacheHits = 0;
    std::uint64_t bvhEvictionCount = 0;
};

struct WorkerError : std::runtime_error {
    std::string code;
    int nativeCode;
    // 외부 계약 오류와 네이티브 오류 번호를 함께 보존합니다.
    WorkerError(std::string errorCode, const std::string& message, int native = 0)
        : std::runtime_error(message), code(std::move(errorCode)), nativeCode(native) {}
};

// 조건에 맞지 않는 입력을 한국어 진단과 함께 거부합니다.
void require(bool condition, const char* code, const char* message) {
    if (!condition) throw WorkerError(code, message);
}

// 두 시점 사이의 경과 시간을 0 이상의 초 단위로 반환합니다.
double elapsedSeconds(Clock::time_point started) {
    return std::max(0.0, std::chrono::duration<double>(Clock::now() - started).count());
}

// JSON 객체가 허용된 필드를 빠짐없이 정확히 가지는지 검사합니다.
void requireExactKeys(const Json& value, std::initializer_list<const char*> keys, const char* message) {
    require(value.is_object(), "INVALID_REQUEST", message);
    std::set<std::string> expected;
    for (const char* key : keys) expected.emplace(key);
    require(value.size() == expected.size(), "INVALID_REQUEST", message);
    for (auto iterator = value.begin(); iterator != value.end(); ++iterator)
        require(expected.count(iterator.key()) != 0, "INVALID_REQUEST", message);
}

// 필수 문자열을 길이와 제어문자 없이 검증해 반환합니다.
std::string requiredText(const Json& object, const char* name, size_t maximum = 512) {
    const auto& value = object.at(name);
    require(value.is_string(), "INVALID_REQUEST", "필수 식별자는 문자열이어야 합니다.");
    const auto text = value.get<std::string>();
    require(!text.empty() && text.size() <= maximum && text.find('\0') == std::string::npos,
            "INVALID_REQUEST", "필수 식별자의 길이나 내용이 올바르지 않습니다.");
    return text;
}

// 세션과 지오메트리 ID를 파일명에 안전한 ASCII 식별자로 제한합니다.
std::string safeIdentifier(const Json& object, const char* name) {
    const auto text = requiredText(object, name, 128);
    require(std::all_of(text.begin(), text.end(), [](unsigned char value) {
        return std::isalnum(value) || value == '-' || value == '_';
    }), "INVALID_REQUEST", "세션 또는 지오메트리 ID에 허용하지 않는 문자가 있습니다.");
    return text;
}

// UTF-8 JSON 경로를 Windows 파일시스템 경로로 변환합니다.
fs::path jsonPath(const Json& value) {
    require(value.is_string(), "INVALID_PATH", "파일 경로는 문자열이어야 합니다.");
    auto text = value.get<std::string>();
    require(!text.empty() && text.find('\0') == std::string::npos,
            "INVALID_PATH", "비어 있거나 널 문자가 포함된 경로입니다.");
    return fs::u8path(text);
}

// 요청 폴더 밖 경로와 재분석 지점을 거부하고 내부 상대경로를 확정합니다.
fs::path childPath(const fs::path& root, const Json& value) {
    fs::path rel = jsonPath(value);
    require(!rel.is_absolute() && !rel.has_root_name() && !rel.has_root_directory(),
            "PATH_ESCAPE", "요청 파일에 지정한 경로는 작업 폴더 내부 상대경로여야 합니다.");
    fs::path current = root;
    for (const auto& part : rel) {
        const auto component = part.native();
        require(component != L".." && component != L"." && component.find(L':') == std::wstring::npos,
                "PATH_ESCAPE", "상위 폴더 이동이나 특수 파일 경로는 허용하지 않습니다.");
        current /= part;
        const DWORD attributes = GetFileAttributesW(current.c_str());
        require(attributes == INVALID_FILE_ATTRIBUTES || (attributes & FILE_ATTRIBUTE_REPARSE_POINT) == 0,
                "PATH_ESCAPE", "작업 파일 경로에 연결 또는 재분석 지점을 사용할 수 없습니다.");
    }
    auto resolved = fs::weakly_canonical(current);
    auto relative = resolved.lexically_relative(root);
    require(!relative.empty() && *relative.begin() != L"..", "PATH_ESCAPE", "작업 폴더 밖 경로입니다.");
    return resolved;
}

// JSON 출력을 임시 파일에 기록한 뒤 같은 디렉터리에서 원자 교체합니다.
void atomicWrite(const fs::path& path, const char* bytes, size_t size) {
    fs::path temporary = path;
    temporary += L".tmp-" + std::to_wstring(GetCurrentProcessId());
    HANDLE file = CreateFileW(temporary.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                              FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) throw WorkerError("OUTPUT_IO_ERROR", "출력 임시 파일을 생성하지 못했습니다.", static_cast<int>(GetLastError()));
    bool ok = true;
    DWORD systemError = ERROR_SUCCESS;
    size_t offset = 0;
    while (offset < size && ok) {
        DWORD written = 0;
        const DWORD requested = static_cast<DWORD>(std::min<size_t>(size - offset, 1u << 26));
        ok = WriteFile(file, bytes + offset, requested, &written, nullptr) && written == requested;
        if (!ok) systemError = GetLastError();
        offset += written;
    }
    if (ok && !FlushFileBuffers(file)) { ok = false; systemError = GetLastError(); }
    CloseHandle(file);
    // Windows 읽기 핸들의 짧은 교체 점유만 최대 240ms 동안 다시 시도합니다.
    for (unsigned attempt = 0; ok; ++attempt) {
        if (MoveFileExW(temporary.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) break;
        systemError = GetLastError();
        const bool transient = systemError == ERROR_ACCESS_DENIED || systemError == ERROR_SHARING_VIOLATION || systemError == ERROR_LOCK_VIOLATION;
        if (!transient || attempt == 12) { ok = false; break; }
        Sleep(20);
    }
    if (!ok) {
        DeleteFileW(temporary.c_str());
        throw WorkerError("OUTPUT_IO_ERROR", "출력 파일의 원자 기록을 완료하지 못했습니다.", static_cast<int>(systemError));
    }
}

// UTF-8 JSON 문서를 원자적으로 확정합니다.
void writeJson(const fs::path& path, const Json& value) {
    const auto bytes = value.dump(2) + "\n";
    atomicWrite(path, bytes.data(), bytes.size());
}

// 크기 제한을 먼저 확인한 완성 JSON 객체만 읽습니다.
Json readJsonObject(const fs::path& path) {
    require(fs::is_regular_file(path), "INPUT_IO_ERROR", "요청 JSON 파일이 없습니다.");
    require(fs::file_size(path) <= 1048576, "INVALID_REQUEST", "요청 JSON 크기가 허용 범위를 넘었습니다.");
    std::ifstream stream(path, std::ios::binary);
    require(stream.good(), "INPUT_IO_ERROR", "요청 JSON 파일을 열지 못했습니다.");
    auto value = Json::parse(stream);
    require(value.is_object(), "INVALID_REQUEST", "요청 JSON 최상위 값은 객체여야 합니다.");
    return value;
}

// 명령 요청 파일을 한 번만 소비할 수 있는 감사 파일명으로 원자 이동합니다.
fs::path acceptCommandFile(const fs::path& requestPath) {
    fs::path accepted = requestPath;
    const auto name = requestPath.filename().wstring();
    const std::wstring suffix = L".request.json";
    require(name.size() > suffix.size() && name.substr(name.size() - suffix.size()) == suffix,
            "INVALID_SEQUENCE", "명령 요청 파일명이 순번 계약과 다릅니다.");
    accepted.replace_filename(name.substr(0, name.size() - suffix.size()) + L".accepted.json");
    require(!fs::exists(accepted), "DUPLICATE_SEQUENCE", "이미 소비한 명령 순번이 다시 제출되었습니다.");
    require(MoveFileExW(requestPath.c_str(), accepted.c_str(), MOVEFILE_WRITE_THROUGH) != 0,
            "INPUT_IO_ERROR", "명령 요청 파일을 원자적으로 접수하지 못했습니다.");
    return accepted;
}

class Progress final : public CCCoreLib::GenericProgressCallback {
    fs::path progressPath;
    fs::path cancelPath;
    Json identity;
    std::string operation;
    std::string stage;
    std::string substage = "LOAD";
    unsigned sequence = 0;
    Clock::time_point previous = Clock::time_point::min();
    std::mutex mutex;
    std::atomic_bool outputFailed { false };
    std::atomic_int outputErrorCode { 0 };
    std::atomic_bool cancelLatched { false };
    std::atomic_bool cancelProbeActive { false };
    std::atomic_bool cancelProbeFailed { false };
    std::atomic<DWORD> cancelProbeErrorCode { ERROR_SUCCESS };
    std::atomic<ULONGLONG> nextCancelProbeTick { 0 };
    float latest = 0.0f;

    // 캐시 간격을 선택적으로 무시해 취소 파일 상태를 한 스레드만 조회합니다.
    bool probeCancellation(bool force) {
        if (outputFailed.load(std::memory_order_acquire)
            || cancelLatched.load(std::memory_order_acquire)
            || cancelProbeFailed.load(std::memory_order_acquire)) return true;
        if (cancelPath.empty()) return false;
        const ULONGLONG now = GetTickCount64();
        if (!force && now < nextCancelProbeTick.load(std::memory_order_acquire)) return false;
        bool expected = false;
        while (!cancelProbeActive.compare_exchange_strong(
                   expected, true, std::memory_order_acq_rel, std::memory_order_acquire)) {
            if (!force) {
                return outputFailed.load(std::memory_order_acquire)
                    || cancelLatched.load(std::memory_order_acquire)
                    || cancelProbeFailed.load(std::memory_order_acquire);
            }
            Sleep(0);
            expected = false;
        }
        const ULONGLONG probeTick = GetTickCount64();
        if (force || probeTick >= nextCancelProbeTick.load(std::memory_order_relaxed)) {
            nextCancelProbeTick.store(probeTick + 25, std::memory_order_release);
            const DWORD attributes = GetFileAttributesW(cancelPath.c_str());
            if (attributes != INVALID_FILE_ATTRIBUTES) {
                cancelLatched.store(true, std::memory_order_release);
            } else {
                const DWORD error = GetLastError();
                if (error != ERROR_FILE_NOT_FOUND && error != ERROR_PATH_NOT_FOUND) {
                    cancelProbeErrorCode.store(error, std::memory_order_relaxed);
                    cancelProbeFailed.store(true, std::memory_order_release);
                }
            }
        }
        cancelProbeActive.store(false, std::memory_order_release);
        return outputFailed.load(std::memory_order_acquire)
            || cancelLatched.load(std::memory_order_acquire)
            || cancelProbeFailed.load(std::memory_order_acquire);
    }

    // 취소와 진행 출력 실패를 우선순위에 맞춰 예외로 변환합니다.
    void checkSignals(bool forceCancellationProbe) {
        const bool cancelled = probeCancellation(forceCancellationProbe);
        if (outputFailed.load(std::memory_order_acquire))
            throw WorkerError("OUTPUT_IO_ERROR", "진행 파일을 갱신하지 못했습니다.", outputErrorCode.load(std::memory_order_relaxed));
        if (cancelProbeFailed.load(std::memory_order_acquire))
            throw WorkerError("INPUT_IO_ERROR", "취소 요청 파일 상태를 확인하지 못했습니다.",
                              static_cast<int>(cancelProbeErrorCode.load(std::memory_order_relaxed)));
        if (cancelled) throw WorkerError("CANCELLED", "사용자가 연산을 취소했습니다.", 104);
    }

    // 잠금 상태에서 현재 진행 정보를 원자 파일로 공개합니다.
    void publishLocked(const char* status) {
        Json payload = identity;
        payload["status"] = status;
        payload["percent"] = latest;
        if (sequence != 0) {
            payload["sequence"] = sequence;
            payload["operation"] = operation;
            payload["stage"] = stage;
            payload["substage"] = substage;
        }
        try { writeJson(progressPath, payload); }
        catch (const WorkerError& error) {
            outputErrorCode.store(error.nativeCode, std::memory_order_relaxed);
            outputFailed.store(true, std::memory_order_release);
        }
        catch (...) { outputFailed.store(true, std::memory_order_release); }
    }
public:
    // 작업별 진행 파일과 취소 파일을 연결합니다.
    Progress(fs::path progress, fs::path cancel, Json baseIdentity = {{"protocol_version", kProtocol}},
             unsigned commandSequence = 0, std::string commandOperation = {}, std::string commandStage = {})
        : progressPath(std::move(progress)), cancelPath(std::move(cancel)), identity(std::move(baseIdentity)),
          operation(std::move(commandOperation)), stage(std::move(commandStage)), sequence(commandSequence) {}
    // 지나치게 잦은 기록을 제한하며 유한한 진행률을 전달합니다.
    void update(float percent) override {
        std::lock_guard<std::mutex> lock(mutex);
        if (std::isfinite(percent)) latest = std::clamp(percent, 0.0f, 99.0f);
        const auto now = Clock::now();
        if (previous != Clock::time_point::min() && now - previous < std::chrono::milliseconds(100)) return;
        previous = now;
        publishLocked("running");
    }
    // 현재 명령의 세부 단계를 즉시 진행 파일에 반영합니다.
    void setSubstage(const char* value) {
        std::lock_guard<std::mutex> lock(mutex);
        substage = value;
        previous = Clock::time_point::min();
        publishLocked("running");
    }
    // 코어 내부 제목 대신 외부 작업 계약의 고정 진행 문구를 사용합니다.
    void setMethodTitle(const char*) override {}
    // 코어 진단 문자열을 사용자 진행 파일에 혼입하지 않습니다.
    void setInfo(const char*) override {}
    // 코어 연산 시작을 진행 파일에 반영합니다.
    void start() override { update(0.0f); }
    // 전체 결과 확정 전에는 완료 상태를 표시하지 않습니다.
    void stop() override {}
    // 코어에서 지원하는 중단 지점마다 취소 파일을 확인합니다.
    bool isCancelRequested() override {
        return probeCancellation(false);
    }
    // 불필요한 코어 문자열 생성을 비활성화합니다.
    bool textCanBeEdited() const override { return false; }
    // 취소와 진행 출력 실패를 구분해 호출자에게 전달합니다.
    void check() {
        checkSignals(false);
    }
    // 응답 기록 직전에 현재 명령 진행을 완료로 표시합니다.
    void finish(const char* status, bool requireUncancelled = true) {
        if (requireUncancelled) checkSignals(true);
        {
            std::lock_guard<std::mutex> lock(mutex);
            latest = 100.0f;
            publishLocked(status);
        }
        if (requireUncancelled) checkSignals(true);
        else if (outputFailed.load(std::memory_order_acquire))
            throw WorkerError("OUTPUT_IO_ERROR", "진행 파일을 갱신하지 못했습니다.", outputErrorCode.load(std::memory_order_relaxed));
    }
};

// 부호 없는 정수 개수와 API에서 표현 가능한 상한을 검증합니다.
unsigned unsignedField(const Json& object, const char* name, unsigned minimum, unsigned maximum) {
    const auto& value = object.at(name);
    require(value.is_number_unsigned() || value.is_number_integer(), "INVALID_COUNT", "개수 매개변수는 정수여야 합니다.");
    const auto count = value.get<std::int64_t>();
    require(count >= minimum && static_cast<std::uint64_t>(count) <= maximum,
            "INVALID_COUNT", "개수 매개변수가 허용 범위를 벗어났습니다.");
    return static_cast<unsigned>(count);
}

// Windows 프로세서 그룹 전체의 활성 논리 프로세서 수를 양의 값으로 확정합니다.
unsigned hardwareThreadCount() {
    const DWORD detected = GetActiveProcessorCount(ALL_PROCESSOR_GROUPS);
    return std::max(1u, static_cast<unsigned>(detected));
}

// 요청 스레드 수를 검증하고 현재 하드웨어 상한으로 제한합니다.
ThreadSettings resolveThreadSettings(const Json& request) {
    ThreadSettings settings;
    settings.hardware = hardwareThreadCount();
    settings.requested = unsignedField(request.at("parameters"), "max_threads", 1, 1024);
    settings.effective = std::min(settings.requested, settings.hardware);
    return settings;
}

// MSVC PPL 기본 스케줄러를 작업의 실제 스레드 상한으로 한 번 설정합니다.
void configureScheduler(unsigned maxThreads) {
    Concurrency::Scheduler::SetDefaultSchedulerPolicy(
        Concurrency::SchedulerPolicy(2, Concurrency::MinConcurrency, 1,
                                     Concurrency::MaxConcurrency, maxThreads));
}

// 실수 매개변수가 유한한 값인지 검사합니다.
double finiteField(const Json& object, const char* name) {
    const auto& value = object.at(name);
    require(value.is_number(), "INVALID_PARAMETER", "수치 매개변수는 숫자여야 합니다.");
    const auto number = value.get<double>();
    require(std::isfinite(number), "INVALID_PARAMETER", "수치 매개변수는 유한해야 합니다.");
    return number;
}

// 파일 길이를 먼저 검증하고 명시된 리틀엔디언 고정폭 원소만 읽습니다.
template <typename T> std::vector<T> readBinary(const fs::path& path, size_t count) {
    require(fs::is_regular_file(path), "INPUT_IO_ERROR", "입력 이진 파일이 없습니다.");
    require(count <= std::numeric_limits<size_t>::max() / sizeof(T), "INVALID_COUNT", "입력 개수가 너무 큽니다.");
    const auto bytes = count * sizeof(T);
    require(fs::file_size(path) == bytes, "BINARY_LENGTH_MISMATCH", "입력 파일 길이가 선언한 개수와 다릅니다.");
    std::vector<T> values(count);
    std::ifstream stream(path, std::ios::binary);
    stream.read(reinterpret_cast<char*>(values.data()), static_cast<std::streamsize>(bytes));
    require(static_cast<size_t>(stream.gcount()) == bytes, "INPUT_IO_ERROR", "입력 이진 파일을 끝까지 읽지 못했습니다.");
    return values;
}

// 등록에 필요한 비공선 점 세트가 존재하는지 실제 코어 좌표로 검사합니다.
void validateRank(Cloud& cloud) {
    require(cloud.size() >= 3, "DEGENERATE_INPUT", "등록하려면 세 개 이상의 점이 필요합니다.");
    const CCVector3d origin(*cloud.getPoint(0));
    CCVector3d direction;
    double longest = 0;
    for (unsigned i = 1; i < cloud.size(); ++i) {
        const CCVector3d delta = CCVector3d(*cloud.getPoint(i)) - origin;
        if (delta.norm2() > longest) { longest = delta.norm2(); direction = delta; }
    }
    double maxAreaSquared = 0;
    for (unsigned i = 1; i < cloud.size(); ++i) {
        const CCVector3d delta = CCVector3d(*cloud.getPoint(i)) - origin;
        maxAreaSquared = std::max(maxAreaSquared, (direction.cross(delta)).norm2());
    }
    require(longest > 0 && maxAreaSquared > longest * longest * 1.0e-16,
            "DEGENERATE_INPUT", "점군이 한 점 또는 직선으로 퇴화했습니다.");
}

// 배정도 파일 좌표를 검사한 뒤 코어의 단정도 좌표로 명시적으로 변환합니다.
std::unique_ptr<Cloud> loadCloud(
    const Json& entity, const fs::path& root, bool registration, Progress& progress,
    NativeTimings* timings = nullptr
) {
    const auto count = unsignedField(entity, "point_count", registration ? 3u : 1u, 0x7fffffffu);
    progress.setSubstage("LOAD");
    const auto readStarted = Clock::now();
    auto coordinates = readBinary<double>(childPath(root, entity.at("positions_file")), size_t(count) * 3);
    if (timings) timings->binaryReadSeconds += elapsedSeconds(readStarted);
    progress.setSubstage("VALIDATE");
    const auto conversionStarted = Clock::now();
    auto cloud = std::make_unique<Cloud>();
    require(cloud->reserve(count), "OUT_OF_MEMORY", "점군 메모리를 확보하지 못했습니다.");
    const double limit = std::sqrt(double(std::numeric_limits<PointCoordinateType>::max())) / 1024.0;
    for (unsigned i = 0; i < count; ++i) {
        CCVector3 point;
        for (unsigned axis = 0; axis < 3; ++axis) {
            const double value = coordinates[size_t(i) * 3 + axis];
            require(std::isfinite(value) && std::abs(value) <= limit, "INVALID_COORDINATE", "좌표가 유한 범위를 벗어났습니다. 공통 좌표 정규화가 필요합니다.");
            point.u[axis] = static_cast<PointCoordinateType>(value);
        }
        cloud->addPoint(point);
        if ((i & 65535u) == 0) progress.check();
    }
    require(cloud->enableScalarField(), "OUT_OF_MEMORY", "거리 필드 메모리를 확보하지 못했습니다.");
    cloud->setCurrentScalarField(0);
    if (registration) validateRank(*cloud);
    if (timings) timings->conversionValidationSeconds += elapsedSeconds(conversionStarted);
    return cloud;
}

// 면 인덱스와 단정도 변환 후 넓이를 확인하고 원본 삼각면 연결을 보존합니다.
std::unique_ptr<CCCoreLib::SimpleMesh> loadMesh(
    const Json& entity, const fs::path& root, Cloud& cloud, Progress& progress,
    NativeTimings* timings = nullptr
) {
    if (!entity.contains("triangles_file")) {
        require(!entity.contains("triangle_count"), "INVALID_MESH", "삼각형 개수만 있고 면 파일이 없습니다.");
        return nullptr;
    }
    const auto count = unsignedField(entity, "triangle_count", 1, 0x7fffffffu);
    progress.setSubstage("MESH_LOAD");
    const auto meshStarted = Clock::now();
    const auto readStarted = Clock::now();
    auto indices = readBinary<std::uint32_t>(childPath(root, entity.at("triangles_file")), size_t(count) * 3);
    if (timings) timings->binaryReadSeconds += elapsedSeconds(readStarted);
    auto mesh = std::make_unique<CCCoreLib::SimpleMesh>(&cloud, false);
    require(mesh->reserve(count), "OUT_OF_MEMORY", "삼각형 메모리를 확보하지 못했습니다.");
    for (unsigned i = 0; i < count; ++i) {
        const auto a = indices[size_t(i) * 3], b = indices[size_t(i) * 3 + 1], c = indices[size_t(i) * 3 + 2];
        require(a < cloud.size() && b < cloud.size() && c < cloud.size(), "INVALID_TRIANGLE_INDEX", "삼각형 꼭짓점 인덱스가 점군 범위를 벗어났습니다.");
        const CCVector3d u = CCVector3d(*cloud.getPoint(b)) - CCVector3d(*cloud.getPoint(a));
        const CCVector3d v = CCVector3d(*cloud.getPoint(c)) - CCVector3d(*cloud.getPoint(a));
        require(a != b && b != c && a != c && u.cross(v).norm2() > u.norm2() * v.norm2() * 1e-16,
                "DEGENERATE_TRIANGLE", "중복 꼭짓점 또는 넓이가 없는 삼각형입니다.");
        mesh->addTriangle(a, b, c);
        if ((i & 65535u) == 0) progress.check();
    }
    if (timings) timings->meshLoadSeconds += elapsedSeconds(meshStarted);
    return mesh;
}

// 등록 매개변수를 검증하고 과거 단발 요청에만 기존 기본값을 유지합니다.
CCCoreLib::ICPRegistrationTools::Parameters parameters(const Json& value, unsigned dataCount, unsigned modelCount, bool mesh, unsigned maxThreads, bool legacy = false) {
    CCCoreLib::ICPRegistrationTools::Parameters params;
    require(value.at("adjust_scale").is_boolean(), "INVALID_PARAMETER", "스케일 조정 여부는 논리값이어야 합니다.");
    params.adjustScale = value.at("adjust_scale").get<bool>();
    params.minScale = finiteField(value, "min_scale");
    params.maxScale = finiteField(value, "max_scale");
    require(params.minScale > 0 && params.minScale <= 1 && params.maxScale >= 1,
            "INVALID_SCALE_RANGE", "추가 변환의 스케일 범위는 양수이고 항등 배율 1을 포함해야 합니다.");
    params.finalOverlapRatio = finiteField(value, "overlap_ratio");
    require((legacy ? params.finalOverlapRatio > 0 : params.finalOverlapRatio >= 0.1) && params.finalOverlapRatio <= 1 && std::floor(dataCount * params.finalOverlapRatio) >= 3,
            "INVALID_OVERLAP", "겹침 비율은 0.1 이상 1 이하이며 최소 세 점을 유지해야 합니다.");
    params.nbMaxIterations = unsignedField(value, "max_iterations", 1, 100000);
    params.convType = CCCoreLib::ICPRegistrationTools::MAX_ITER_CONVERGENCE;
    params.samplingLimit = unsignedField(value, "sampling_limit", 3, 0x7fffffffu);
    require(dataCount <= params.samplingLimit && (mesh || modelCount <= params.samplingLimit),
            "SAMPLING_LIMIT_EXCEEDED", "결정적 표본화를 먼저 수행해 점 수를 표본 제한 이내로 줄여야 합니다.");
    require(unsignedField(value, "max_threads", 1, 1024) >= maxThreads,
            "INVALID_PARAMETER", "실제 스레드 수가 요청 상한을 벗어났습니다.");
    params.maxThreadCount = static_cast<int>(maxThreads);
    params.useC2MSignedDistances = false;
    require((legacy && !value.contains("filter_out_farthest_points")) ||
                (value.contains("filter_out_farthest_points") && value.at("filter_out_farthest_points").is_boolean()),
            "INVALID_PARAMETER", "최원거리점 필터 여부는 명시적인 논리값이어야 합니다.");
    params.filterOutFarthestPoints = value.value("filter_out_farthest_points", false);
    return params;
}

// 코어의 실제 회전·이동·배율을 배정도 행 우선 4×4 행렬로 변환합니다.
Json matrixJson(const Transform& transform) {
    Json matrix = Json::array();
    for (unsigned row = 0; row < 4; ++row) {
        Json values = Json::array();
        for (unsigned column = 0; column < 4; ++column) {
            double value = row == column ? 1.0 : 0.0;
            if (row < 3 && column < 3) value = transform.s * (transform.R.isValid() ? transform.R.getValue(row, column) : (row == column ? 1.0 : 0.0));
            else if (row < 3) value = transform.T.u[row];
            require(std::isfinite(value), "INVALID_NATIVE_RESULT", "코어가 유한하지 않은 변환을 반환했습니다.");
            values.push_back(value);
        }
        matrix.push_back(values);
    }
    return matrix;
}

struct SearchDomain {
    std::string id;
    CCVector3 min;
    CCVector3 max;
    Json declaration;
};

// 전체 검색 경계를 검증하고 float32 변환 오차를 바깥 방향으로 보정합니다.
std::optional<SearchDomain> parseSearchDomain(const Json& value) {
    if (value.is_null()) return std::nullopt;
    require(value.is_object() && value.size() == 3 && value.contains("domain_id")
            && value.contains("min") && value.contains("max"),
            "INVALID_SEARCH_DOMAIN", "검색 범위 필드가 고정 계약과 다릅니다.");
    require(value.at("domain_id").is_string(), "INVALID_SEARCH_DOMAIN", "검색 범위 식별자는 문자열이어야 합니다.");
    SearchDomain result;
    result.id = value.at("domain_id").get<std::string>();
    require(!result.id.empty() && result.id.size() <= 128
            && std::none_of(result.id.begin(), result.id.end(), [](unsigned char c) {
                // 식별자 안의 제어 문자를 거부합니다.
                return c < 32 || c == 127;
            }), "INVALID_SEARCH_DOMAIN", "검색 범위 식별자가 유효하지 않습니다.");
    // Python 문자열 공백 규칙과 맞춰 유니코드 공백만 있는 식별자도 거부합니다.
    wchar_t identifier[128];
    const int identifierLength = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
        result.id.data(), static_cast<int>(result.id.size()), identifier, 128);
    require(identifierLength > 0 && std::wstring(identifier, identifierLength).find_first_not_of(
                L" \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000") != std::wstring::npos,
            "INVALID_SEARCH_DOMAIN", "검색 범위 식별자는 공백만 포함할 수 없습니다.");
    result.declaration = value;
    require(value.at("min").is_array() && value.at("max").is_array()
            && value.at("min").size() == 3 && value.at("max").size() == 3,
            "INVALID_SEARCH_DOMAIN", "검색 범위는 각 축의 최솟값과 최댓값 세 개여야 합니다.");
    for (unsigned axis = 0; axis < 3; ++axis) {
        require(value.at("min")[axis].is_number() && value.at("max")[axis].is_number(),
                "INVALID_SEARCH_DOMAIN", "검색 범위 좌표는 유한한 숫자여야 합니다.");
        const double low = value.at("min")[axis].get<double>();
        const double high = value.at("max")[axis].get<double>();
        const double limit = double(std::numeric_limits<PointCoordinateType>::max());
        require(std::isfinite(low) && std::isfinite(high) && low <= high
                && low >= -limit && high <= limit,
                "INVALID_SEARCH_DOMAIN", "검색 범위 순서 또는 float32 표현 범위가 유효하지 않습니다.");
        auto roundedLow = static_cast<PointCoordinateType>(low);
        auto roundedHigh = static_cast<PointCoordinateType>(high);
        if (double(roundedLow) > low)
            roundedLow = std::nextafter(roundedLow, -std::numeric_limits<PointCoordinateType>::infinity());
        if (double(roundedHigh) < high)
            roundedHigh = std::nextafter(roundedHigh, std::numeric_limits<PointCoordinateType>::infinity());
        require(std::isfinite(roundedLow) && std::isfinite(roundedHigh),
                "INVALID_SEARCH_DOMAIN", "보수적으로 변환한 검색 범위가 float32 범위를 벗어났습니다.");
        result.min.u[axis] = roundedLow;
        result.max.u[axis] = roundedHigh;
    }
    return result;
}

// 검사점과 대상 전체 정점이 선언된 검색 경계 안에 포함되는지 확인합니다.
void validateDomainContains(const SearchDomain& domain, Cloud& cloud) {
    CCVector3 low, high;
    cloud.getBoundingBox(low, high);
    for (unsigned axis = 0; axis < 3; ++axis)
        require(low.u[axis] >= domain.min.u[axis] && high.u[axis] <= domain.max.u[axis],
                "INVALID_SEARCH_DOMAIN", "실제 지오메트리가 선언된 검색 범위를 벗어났습니다.");
}

// 불변 메시의 검색 단계와 격자를 재사용하고 조회점의 정확 거리는 매번 계산합니다.
int computeDistances(
    Cloud& data, Cloud& model, CCCoreLib::SimpleMesh* mesh, Progress* progress,
    unsigned maxThreads, NativeTimings* timings = nullptr, OperationCounters* counters = nullptr,
    CCCoreLib::DistanceComputationTools::Cloud2MeshDistancesCache* meshGrid = nullptr,
    std::optional<unsigned char>* meshLevel = nullptr, const SearchDomain* domain = nullptr
) {
    if (mesh) {
        unsigned char requestedLevel;
        if (meshLevel && meshLevel->has_value()) {
            requestedLevel = meshLevel->value();
        } else {
            progress->setSubstage("OCTREE");
            const auto octreeStarted = Clock::now();
            CCCoreLib::DgmOctree dataOctree(&data);
            CCCoreLib::DgmOctree modelOctree(&model);
            if (dataOctree.build(progress) < static_cast<int>(data.size())
                || modelOctree.build(progress) < static_cast<int>(model.size())) {
                progress->check();
                throw WorkerError("OCTREE_BUILD_FAILED", "삼각면 거리 평가를 위한 옥트리를 생성하지 못했습니다.");
            }
            if (timings) timings->octreeSeconds += elapsedSeconds(octreeStarted);
            if (counters) counters->octreeBuildCount += 2;
            progress->check();
            requestedLevel = dataOctree.findBestLevelForComparisonWithOctree(&modelOctree);
            if (meshLevel) *meshLevel = requestedLevel;
        }
        progress->check();
        CCCoreLib::DistanceComputationTools::Cloud2MeshDistancesComputationParams params;
        params.octreeLevel = requestedLevel;
        params.signedDistances = false;
        params.useDistanceMap = false;
        params.multiThread = maxThreads > 1;
        params.maxThreadCount = static_cast<int>(maxThreads);
        progress->setSubstage("KERNEL");
        const auto kernelStarted = Clock::now();
        const auto code = domain
            ? CCCoreLib::DistanceComputationTools::computeCloud2MeshDistancesWithCacheAndBounds(
                &data, mesh, params, progress, nullptr, meshGrid, domain->min, domain->max)
            : CCCoreLib::DistanceComputationTools::computeCloud2MeshDistancesWithCache(
                &data, mesh, params, progress, nullptr, meshGrid);
        // 메모리 예산으로 낮아진 실제 단계를 유지하여 가용량 회복 때 재생성하지 않습니다.
        if (meshLevel && meshGrid && code >= CCCoreLib::DistanceComputationTools::SUCCESS)
            *meshLevel = meshGrid->level;
        if (timings) {
            const double gridSeconds = meshGrid ? meshGrid->buildSeconds : 0;
            timings->meshGridSeconds += gridSeconds;
            timings->kernelSeconds += std::max(0.0, elapsedSeconds(kernelStarted) - gridSeconds);
        }
        if (counters) {
            if (meshGrid && meshGrid->cacheHit) ++counters->meshGridCacheHits;
            else ++counters->meshGridBuildCount;
        }
        return code;
    }
    CCCoreLib::DistanceComputationTools::Cloud2CloudDistancesComputationParams params;
    params.multiThread = maxThreads > 1;
    params.maxThreadCount = static_cast<int>(maxThreads);
    progress->setSubstage("KERNEL");
    const auto kernelStarted = Clock::now();
    const auto code = CCCoreLib::DistanceComputationTools::computeCloud2CloudDistances(&data, &model, params, progress);
    if (timings) timings->kernelSeconds += elapsedSeconds(kernelStarted);
    return code;
}

// 모든 결과에 프로토콜과 코어의 재현 가능한 빌드 식별자를 붙입니다.
Json baseResult(const ThreadSettings& threads, const char* protocol = kProtocol) {
    return {{"protocol_version", protocol}, {"core_commit", CORE_COMMIT}, {"core_patch", CORE_PATCH},
            {"thread_backend", kThreadBackend}, {"tbb_version", kTbbVersion},
            {"requested_max_threads", threads.requested}, {"effective_max_threads", threads.effective},
            {"hardware_thread_count", threads.hardware},
            {"distance_multithread_support", CCCoreLib::DistanceComputationTools::MultiThreadSupport()},
            {"octree_multithread_support", CCCoreLib::DgmOctree::MultiThreadSupport()},
            {"coordinate_type", "float32"}, {"matrix_scalar_type", "float64"}};
}

// 메시 등록의 내부 누적 계측만 응답으로 변환합니다.
Json icpStatisticsJson(const CCCoreLib::ICPMeshRegistrationContext::Statistics& stats) {
    return {{"iterations_attempted", stats.iterations_attempted},
            {"iterations_accepted", stats.iterations_accepted},
            {"distance_call_count", stats.distance_call_count},
            {"surface_search_backend", stats.surface_search_backend},
            {"bvh_query_count", stats.bvh_query_count},
            {"mesh_grid_build_count", stats.mesh_grid_build_count},
            {"mesh_grid_cache_hits", stats.mesh_grid_cache_hits},
            {"mesh_grid_expansion_count", stats.mesh_grid_expansion_count},
            {"mesh_grid_level_reduction_count", stats.mesh_grid_level_reduction_count},
            {"query_octree_build_count", stats.query_octree_build_count},
            {"mesh_grid_peak_estimated_bytes", stats.mesh_grid_peak_estimated_bytes},
            {"model_level_seconds", stats.model_level_seconds},
            {"query_octree_seconds", stats.query_octree_seconds},
            {"mesh_grid_seconds", stats.mesh_grid_seconds},
            {"closest_point_seconds", stats.closest_point_seconds},
            {"filter_rms_seconds", stats.filter_rms_seconds},
            {"transform_solve_seconds", stats.transform_solve_seconds},
            {"transform_apply_seconds", stats.transform_apply_seconds},
            {"total_seconds", stats.total_seconds},
            {"stop_reason", stats.stop_reason}};
}

// 작업 계약에 따라 한 번의 등록 또는 거리 평가를 수행합니다.
Json execute(const Json& request, const fs::path& root, Progress& progress, const ThreadSettings& threads) {
    require(request.at("protocol_version") == kProtocol, "PROTOCOL_MISMATCH", "지원하지 않는 네이티브 프로토콜입니다.");
    const auto operation = request.at("operation").get<std::string>();
    require(operation == "register" || operation == "distance", "INVALID_OPERATION", "지원하지 않는 작업 종류입니다.");
    progress.check();
    auto data = loadCloud(request.at("data"), root, operation == "register", progress);
    auto model = loadCloud(request.at("model"), root, operation == "register", progress);
    require(!request.at("data").contains("triangles_file"), "INVALID_MESH", "이동 데이터에는 점 좌표만 전달해야 합니다.");
    auto mesh = loadMesh(request.at("model"), root, *model, progress);
    progress.check();
    Json result = baseResult(threads);
    result["status"] = "completed";
    result["operation"] = operation;
    if (operation == "register") {
        auto params = parameters(request.at("parameters"), data->size(), model->size(), bool(mesh), threads.effective, true);
        Transform transform;
        double rms = -1;
        unsigned count = 0;
        const auto code = CCCoreLib::ICPRegistrationTools::Register(model.get(), mesh.get(), data.get(), params, transform, rms, count, &progress);
        progress.check();
        if (code >= CCCoreLib::ICPRegistrationTools::ICP_ERROR)
            throw WorkerError("NATIVE_REGISTRATION_ERROR", "CCCoreLib 등록 연산이 실패했습니다.", code);
        require(std::isfinite(rms) && rms >= 0 && count >= 3 && std::isfinite(transform.s) && transform.s > 0,
                "INVALID_NATIVE_RESULT", "코어 등록 결과의 거리·개수·배율이 유효하지 않습니다.");
        require(!params.adjustScale || (transform.s >= params.minScale - 1e-12 && transform.s <= params.maxScale + 1e-12),
                "INVALID_NATIVE_RESULT", "코어의 반환 배율이 요청한 경계를 벗어났습니다.");
        result["matrix_4x4"] = matrixJson(transform);
        result["scale"] = transform.s;
        result["final_rms"] = rms;
        result["final_point_count"] = count;
        result["native_code"] = code;
    } else {
        const auto code = computeDistances(*data, *model, mesh.get(), &progress, threads.effective);
        progress.check();
        if (code < CCCoreLib::DistanceComputationTools::SUCCESS)
            throw WorkerError("NATIVE_DISTANCE_ERROR", "CCCoreLib 거리 평가가 실패했습니다.", code);
        std::vector<double> distances(data->size());
        for (unsigned i = 0; i < data->size(); ++i) {
            if ((i & 65535u) == 0) progress.check();
            distances[i] = double(data->getPointScalarValue(i));
            require(std::isfinite(distances[i]) && distances[i] >= 0, "INVALID_NATIVE_RESULT", "코어가 유효하지 않은 거리를 반환했습니다.");
        }
        const Json distanceName = request.value("distances_file", Json("distances.bin"));
        atomicWrite(childPath(root, distanceName), reinterpret_cast<const char*>(distances.data()), distances.size() * sizeof(double));
        result["distances_file"] = distanceName;
        result["count"] = distances.size();
        result["native_code"] = code;
    }
    return result;
}

// 모든 입출력·취소·진행 경로의 충돌을 연산 전에 거부합니다.
void validatePaths(const Json& request, const fs::path& root, const fs::path& requestPath) {
    std::set<fs::path> paths;
    std::wstring foldedRequest = requestPath.native();
    std::transform(foldedRequest.begin(), foldedRequest.end(), foldedRequest.begin(), ::towlower);
    paths.insert(fs::path(foldedRequest));
    std::vector<Json> names = {request.at("output_file"), request.at("progress_file"), request.at("cancel_file"),
                              request.at("data").at("positions_file"), request.at("model").at("positions_file")};
    if (request.at("model").contains("triangles_file")) names.push_back(request.at("model").at("triangles_file"));
    if (request.value("operation", "") == "distance") names.push_back(request.value("distances_file", Json("distances.bin")));
    for (const auto& name : names) {
        auto path = childPath(root, name);
        std::wstring folded = path.native();
        std::transform(folded.begin(), folded.end(), folded.begin(), ::towlower);
        require(paths.insert(fs::path(folded)).second, "PATH_COLLISION", "입력·출력·진행·취소 파일 경로가 서로 겹칩니다.");
    }
}

// 설치된 공유 라이브러리와 실제 비대칭 점군 등록 결과를 확인합니다.
Json checkEnvironment(const ThreadSettings& threads) {
    require(CCCoreLib::DistanceComputationTools::MultiThreadSupport(), "THREAD_BACKEND_MISSING", "CCCoreLib 거리 멀티스레드 지원이 비활성화되어 있습니다.");
    require(CCCoreLib::DgmOctree::MultiThreadSupport(), "THREAD_BACKEND_MISSING", "CCCoreLib 옥트리 멀티스레드 지원이 비활성화되어 있습니다.");
    Cloud data, model;
    const std::array<CCVector3, 8> points = {CCVector3(-1,-1,-1), CCVector3(2,0,0), CCVector3(0,3,0), CCVector3(0,0,4),
                                          CCVector3(1,2,1), CCVector3(-2,1,3), CCVector3(3,-1,2), CCVector3(2,3,-2)};
    require(data.reserve(8) && model.reserve(8), "OUT_OF_MEMORY", "자체 검사 메모리를 확보하지 못했습니다.");
    for (const auto& point : points) { data.addPoint(point); model.addPoint(point + CCVector3(0.02f,-0.01f,0.03f)); }
    require(data.enableScalarField() && model.enableScalarField(), "OUT_OF_MEMORY", "자체 검사 거리 필드를 확보하지 못했습니다.");
    data.setCurrentScalarField(0); model.setCurrentScalarField(0);
    CCCoreLib::ICPRegistrationTools::Parameters params;
    params.convType = CCCoreLib::ICPRegistrationTools::MAX_ITER_CONVERGENCE;
    params.nbMaxIterations = 30; params.maxThreadCount = static_cast<int>(threads.effective);
    Transform transform;
    double rms = -1; unsigned count = 0;
    const auto code = CCCoreLib::ICPRegistrationTools::Register(&model, nullptr, &data, params, transform, rms, count);
    require(code < CCCoreLib::ICPRegistrationTools::ICP_ERROR && rms < 1e-5 && count == 8 &&
            (transform.T - CCVector3d(0.02,-0.01,0.03)).norm() < 1e-5,
            "SELF_TEST_FAILED", "공유 라이브러리의 실제 합성 등록 검사가 실패했습니다.");
    wchar_t libraryPath[32768];
    const HMODULE library = GetModuleHandleW(L"CCCoreLib.dll");
    require(library && GetModuleFileNameW(library, libraryPath, 32768), "LIBRARY_MISSING", "CCCoreLib 공유 라이브러리를 확인하지 못했습니다.");
    wchar_t tbbLibraryPath[32768];
    const HMODULE tbbLibrary = GetModuleHandleW(L"tbb12.dll");
    require(tbbLibrary && GetModuleFileNameW(tbbLibrary, tbbLibraryPath, 32768), "THREAD_BACKEND_MISSING", "oneTBB 공유 라이브러리를 확인하지 못했습니다.");
    auto result = baseResult(threads, kSessionProtocol);
    result["status"] = "completed"; result["self_test"] = "passed";
    result["library_path"] = fs::path(libraryPath).u8string();
    result["tbb_library_path"] = fs::path(tbbLibraryPath).u8string();
    result["final_rms"] = rms; result["final_point_count"] = count;
    return result;
}

// 현재 프로세스의 최고 작업 집합을 운영체제 계수에서 읽습니다.
std::uint64_t peakWorkingSetBytes() {
    PROCESS_MEMORY_COUNTERS counters {};
    counters.cb = sizeof(counters);
    if (!GetProcessMemoryInfo(GetCurrentProcess(), &counters, sizeof(counters))) return 0;
    return static_cast<std::uint64_t>(counters.PeakWorkingSetSize);
}

// 명령별 단계 시간을 고정된 결과 필드로 변환합니다.
Json timingJson(const NativeTimings& timings) {
    return {{"binary_read_seconds", timings.binaryReadSeconds},
            {"conversion_validation_seconds", timings.conversionValidationSeconds},
            {"mesh_load_seconds", timings.meshLoadSeconds}, {"octree_seconds", timings.octreeSeconds},
            {"bvh_build_seconds", timings.bvhBuildSeconds}, {"mesh_grid_seconds", timings.meshGridSeconds}, {"kernel_seconds", timings.kernelSeconds},
            {"serialization_seconds", timings.serializationSeconds}};
}

// 명령별 적재·가속구조 생성과 캐시 적중 횟수를 고정 필드로 변환합니다.
Json counterJson(const OperationCounters& counters) {
    return {{"geometry_load_count", counters.geometryLoadCount},
            {"geometry_cache_hits", counters.geometryCacheHits},
            {"octree_build_count", counters.octreeBuildCount},
            {"octree_cache_hits", counters.octreeCacheHits},
            {"mesh_grid_build_count", counters.meshGridBuildCount},
            {"mesh_grid_cache_hits", counters.meshGridCacheHits},
            {"bvh_build_count", counters.bvhBuildCount}, {"bvh_cache_hits", counters.bvhCacheHits},
            {"bvh_eviction_count", counters.bvhEvictionCount}};
}

struct SessionStatistics {
    OperationCounters counters;
    std::uint64_t peakWorkingSet = 0;
    std::uint64_t cachePeakEstimated = 0;
    std::uint64_t cacheResidentEstimated = 0;
    std::uint64_t bvhResidentBytes = 0;
    std::uint64_t bvhPeakBuildBytes = 0;

    // 직접 계측한 명령 가속구조 횟수를 세션 합계에 더합니다.
    void addOperation(const OperationCounters& operation) {
        counters.octreeBuildCount += operation.octreeBuildCount;
        counters.octreeCacheHits += operation.octreeCacheHits;
        counters.meshGridBuildCount += operation.meshGridBuildCount;
        counters.meshGridCacheHits += operation.meshGridCacheHits;
        counters.bvhBuildCount += operation.bvhBuildCount;
        counters.bvhCacheHits += operation.bvhCacheHits;
        counters.bvhEvictionCount += operation.bvhEvictionCount;
        peakWorkingSet = std::max(peakWorkingSet, peakWorkingSetBytes());
    }

    // 세션 전체 캐시·가속구조·메모리 통계를 반환합니다.
    Json toJson() const {
        Json result = counterJson(counters);
        result["peak_working_set_bytes"] = peakWorkingSet;
        result["cache_peak_estimated_bytes"] = cachePeakEstimated;
        result["cache_resident_estimated_bytes"] = cacheResidentEstimated;
        result["bvh_resident_bytes"] = bvhResidentBytes;
        result["bvh_peak_build_bytes"] = bvhPeakBuildBytes;
        return result;
    }
};

struct CachedGeometry {
    Json descriptor;
    // PointCloud를 먼저 선언해 뒤의 SimpleMesh가 먼저 파괴되도록 수명을 보장합니다.
    std::unique_ptr<Cloud> cloud;
    std::unique_ptr<CCCoreLib::SimpleMesh> mesh;
    std::unique_ptr<ccalign::MeshBvh> bvh;
    std::uint64_t bvhLastUse = 0;
    bool rankValidated = false;
    std::uint64_t estimatedBytes = 0;
    std::uint64_t cacheHits = 0;
    NativeTimings loadTimings;
    std::string cacheScope;
};

class GeometryRegistry {
    fs::path root;
    SessionStatistics& statistics;
    std::map<std::string, std::unique_ptr<CachedGeometry>> entries;
    std::set<std::string> releasedIdentifiers;
    Json releasedStatistics = Json::array();
    std::uint64_t estimatedBytes = 0;
    std::map<std::string, Json> domainDeclarations;
    std::uint64_t bvhClock = 0;

    // 지오메트리 설명의 필수 필드와 메시 필드 쌍을 검사합니다.
    static void validateDescriptor(const Json& entity) {
        require(entity.is_object(), "INVALID_GEOMETRY", "지오메트리 설명은 객체여야 합니다.");
        const bool mesh = entity.contains("triangles_file") || entity.contains("triangle_count");
        if (mesh) {
            requireExactKeys(entity, {"geometry_id", "positions_file", "point_count", "triangles_file", "triangle_count", "cache_scope"},
                             "메시 지오메트리 설명 필드가 고정 계약과 다릅니다.");
        } else {
            requireExactKeys(entity, {"geometry_id", "positions_file", "point_count", "cache_scope"},
                             "점 지오메트리 설명 필드가 고정 계약과 다릅니다.");
        }
        const auto scope = requiredText(entity, "cache_scope", 16);
        require(scope == "SESSION" || scope == "COMMAND", "INVALID_GEOMETRY",
                "지오메트리 캐시 범위는 SESSION 또는 COMMAND여야 합니다.");
    }

    // 지오메트리의 적재·캐시·수명 통계를 종료 요약 객체로 변환합니다.
    static Json statistic(const std::string& identifier, const CachedGeometry& value, bool released) {
        return {{"geometry_id", identifier}, {"point_count", value.cloud->size()},
                {"triangle_count", value.mesh ? value.mesh->size() : 0},
                {"load_count", 1}, {"cache_hits", value.cacheHits},
                {"binary_read_seconds", value.loadTimings.binaryReadSeconds},
                {"conversion_validation_seconds", value.loadTimings.conversionValidationSeconds},
                {"mesh_load_seconds", value.loadTimings.meshLoadSeconds},
                {"estimated_bytes", value.estimatedBytes}, {"cache_scope", value.cacheScope},
                {"released_after_command", released}};
    }

    // 새 설명을 PointCloud와 선택적 SimpleMesh로 한 번만 적재합니다.
    std::unique_ptr<CachedGeometry> load(
        const Json& entity, bool registration, OperationCounters& counters,
        NativeTimings& timings, Progress& progress
    ) {
        auto result = std::make_unique<CachedGeometry>();
        result->descriptor = entity;
        result->cacheScope = requiredText(entity, "cache_scope", 16);
        const auto points = unsignedField(entity, "point_count", registration ? 3u : 1u, 0x7fffffffu);
        result->cloud = loadCloud(entity, root, registration, progress, &result->loadTimings);
        result->rankValidated = registration;
        if (entity.contains("triangles_file"))
            result->mesh = loadMesh(entity, root, *result->cloud, progress, &result->loadTimings);
        result->estimatedBytes = std::uint64_t(points) *
            (3u * sizeof(PointCoordinateType) + sizeof(ScalarType));
        if (entity.contains("triangle_count"))
            result->estimatedBytes += std::uint64_t(unsignedField(entity, "triangle_count", 1, 0x7fffffffu))
                * 3u * sizeof(std::uint32_t);
        timings.binaryReadSeconds += result->loadTimings.binaryReadSeconds;
        timings.conversionValidationSeconds += result->loadTimings.conversionValidationSeconds;
        timings.meshLoadSeconds += result->loadTimings.meshLoadSeconds;
        ++counters.geometryLoadCount;
        ++statistics.counters.geometryLoadCount;
        return result;
    }

public:
    // 세션 작업 루트와 누적 통계를 지오메트리 레지스트리에 연결합니다.
    GeometryRegistry(fs::path jobRoot, SessionStatistics& sessionStatistics)
        : root(std::move(jobRoot)), statistics(sessionStatistics) {}

    // 물리 메모리와 커밋 잔여량의 작은 값으로 전체 BVH 예산을 제한합니다.
    static std::uint64_t bvhBudget() {
        MEMORYSTATUSEX status {};
        status.dwLength = sizeof(status);
        require(GlobalMemoryStatusEx(&status) != FALSE, "BVH_MEMORY_BUDGET", "가용 메모리를 확인하지 못했습니다.");
        return std::min<std::uint64_t>(1ull << 30, std::min(status.ullAvailPhys, status.ullAvailPageFile) / 4);
    }

    // 현재 대상의 트리를 보호하면서 가장 오래 사용하지 않은 트리부터 해제합니다.
    std::uint64_t reserveBvh(std::uint64_t extraBytes, CachedGeometry& protectedModel, OperationCounters& counters) {
        const auto budget = bvhBudget();
        while (statistics.bvhResidentBytes > budget || extraBytes > budget - statistics.bvhResidentBytes) {
            CachedGeometry* oldest = nullptr;
            for (auto& item : entries) {
                auto* candidate = item.second.get();
                if (candidate != &protectedModel && candidate->bvh && (!oldest || candidate->bvhLastUse < oldest->bvhLastUse))
                    oldest = candidate;
            }
            require(oldest != nullptr, "BVH_MEMORY_BUDGET", "정확 표면 검색에 필요한 BVH 메모리 예산이 부족합니다.");
            const auto bytes = oldest->bvh->residentBytes();
            oldest->bvh.reset();
            oldest->estimatedBytes -= bytes;
            estimatedBytes -= bytes;
            statistics.bvhResidentBytes -= bytes;
            statistics.cacheResidentEstimated = estimatedBytes;
            ++counters.bvhEvictionCount;
        }
        return budget - statistics.bvhResidentBytes;
    }

    // 검색 범위와 재사용을 유지하며 생성·예산 산정에 같은 CPU 상한을 적용합니다.
    ccalign::MeshBvh* prepareBvh(CachedGeometry& model, const SearchDomain* domain,
                               NativeTimings& timings, OperationCounters& counters, Progress& progress,
                               unsigned maxThreads) {
        if (domain) {
            const auto known = domainDeclarations.find(domain->id);
            require(known == domainDeclarations.end() || known->second == domain->declaration,
                    "INVALID_SEARCH_DOMAIN", "같은 검색 범위 식별자에 다른 경계를 지정할 수 없습니다.");
            domainDeclarations.emplace(domain->id, domain->declaration);
        }
        if (!model.mesh) return nullptr;
        model.bvhLastUse = ++bvhClock;
        if (model.bvh) {
            ++counters.bvhCacheHits;
            return model.bvh.get();
        }
        const auto peak = ccalign::MeshBvh::estimatePeakBuildBytes(model.mesh->size(), maxThreads);
        const auto availableBytes = reserveBvh(peak, model, counters);
        auto tree = std::make_unique<ccalign::MeshBvh>();
        progress.setSubstage("BVH");
        const auto started = Clock::now();
        const bool built = tree->build(*model.mesh, *model.cloud, availableBytes, &progress, maxThreads);
        timings.bvhBuildSeconds += elapsedSeconds(started);
        progress.check();
        if (!built) throw WorkerError("BVH_BUILD_FAILED", "정확 표면 검색 트리 생성 실패: " + tree->lastError());
        statistics.bvhPeakBuildBytes = std::max(statistics.bvhPeakBuildBytes,
                                                statistics.bvhResidentBytes + tree->peakBuildBytes());
        const auto bytes = tree->residentBytes();
        model.estimatedBytes += bytes;
        estimatedBytes += bytes;
        statistics.bvhResidentBytes += bytes;
        statistics.cacheResidentEstimated = estimatedBytes;
        statistics.cachePeakEstimated = std::max(statistics.cachePeakEstimated, estimatedBytes);
        model.bvh = std::move(tree);
        ++counters.bvhBuildCount;
        return model.bvh.get();
    }

    // 원래 조회 순서와 최근접 점 계약을 유지하며 메모리 예산 안에서 BVH 거리를 계산합니다.
    int queryBvh(CachedGeometry& model, CCCoreLib::GenericIndexedCloudPersist& data, Cloud* cpSet,
                 unsigned threads, CCCoreLib::GenericProgressCallback* progress, OperationCounters& counters) {
        const auto availableBytes = reserveBvh(ccalign::MeshBvh::estimateQueryBytes(data.size(), threads, cpSet != nullptr), model, counters);
        model.bvhLastUse = ++bvhClock;
        return model.bvh->computeDistances(data, cpSet, threads, progress, model.bvh->residentBytes() + availableBytes);
    }

    // 같은 ID·설명은 캐시하고 같은 ID의 다른 설명은 세션 오류로 거부합니다.
    CachedGeometry& resolve(
        const Json& entity, bool registration, OperationCounters& counters,
        NativeTimings& timings, Progress& progress
    ) {
        validateDescriptor(entity);
        const auto identifier = safeIdentifier(entity, "geometry_id");
        require(releasedIdentifiers.count(identifier) == 0, "GEOMETRY_LIFETIME_EXPIRED",
                "명령 종료로 해제한 지오메트리 ID를 다시 사용할 수 없습니다.");
        auto found = entries.find(identifier);
        if (found != entries.end()) {
            require(found->second->descriptor == entity, "GEOMETRY_DESCRIPTOR_MISMATCH",
                    "같은 지오메트리 ID가 다른 파일 설명으로 다시 제출되었습니다.");
            ++found->second->cacheHits;
            ++counters.geometryCacheHits;
            ++statistics.counters.geometryCacheHits;
            if (registration && !found->second->rankValidated) {
                progress.setSubstage("VALIDATE");
                const auto started = Clock::now();
                validateRank(*found->second->cloud);
                const auto seconds = elapsedSeconds(started);
                found->second->loadTimings.conversionValidationSeconds += seconds;
                timings.conversionValidationSeconds += seconds;
                found->second->rankValidated = true;
            }
            return *found->second;
        }
        auto loaded = load(entity, registration, counters, timings, progress);
        estimatedBytes += loaded->estimatedBytes;
        statistics.cacheResidentEstimated = estimatedBytes;
        statistics.cachePeakEstimated = std::max(statistics.cachePeakEstimated, estimatedBytes);
        statistics.peakWorkingSet = std::max(statistics.peakWorkingSet, peakWorkingSetBytes());
        auto [iterator, inserted] = entries.emplace(identifier, std::move(loaded));
        require(inserted, "GEOMETRY_DESCRIPTOR_MISMATCH", "지오메트리 ID를 캐시에 등록하지 못했습니다.");
        return *iterator->second;
    }

    // 현재 명령 전용 점군을 통계에 남기고 다음 명령 전에 즉시 해제합니다.
    void releaseCommandScoped() {
        for (auto iterator = entries.begin(); iterator != entries.end();) {
            if (iterator->second->cacheScope != "COMMAND") {
                ++iterator;
                continue;
            }
            releasedStatistics.push_back(statistic(iterator->first, *iterator->second, true));
            releasedIdentifiers.insert(iterator->first);
            if (iterator->second->bvh) statistics.bvhResidentBytes -= iterator->second->bvh->residentBytes();
            estimatedBytes -= iterator->second->estimatedBytes;
            statistics.cacheResidentEstimated = estimatedBytes;
            iterator = entries.erase(iterator);
        }
    }

    // 세션 종료 결과에 형상별 적재 시간·크기·적중 횟수를 기록합니다.
    Json summary() const {
        Json result = releasedStatistics;
        for (const auto& item : entries) {
            const auto& value = *item.second;
            result.push_back(statistic(item.first, value, false));
        }
        return result;
    }
};

// 세션 식별자를 네이티브 기본 결과에 병합합니다.
Json sessionResultBase(const Json& identity, const ThreadSettings& threads) {
    Json result = baseResult(threads, kSessionProtocol);
    for (auto iterator = identity.begin(); iterator != identity.end(); ++iterator)
        result[iterator.key()] = iterator.value();
    return result;
}

// 한 번의 등록 또는 거리 명령을 세션 지오메트리 캐시로 수행합니다.
Json executeSessionOperation(
    const Json& request, const fs::path& root, Progress& progress,
    const ThreadSettings& threads, GeometryRegistry& registry, SessionStatistics& sessionStatistics
) {
    const auto commandStarted = Clock::now();
    const auto operation = requiredText(request, "operation", 32);
    require(operation == "register" || operation == "distance", "INVALID_OPERATION", "지원하지 않는 작업 종류입니다.");
    require(unsignedField(request.at("parameters"), "max_threads", 1, 1024) == threads.requested,
            "INVALID_PARAMETER", "명령 스레드 수가 세션 시작 값과 다릅니다.");
    NativeTimings timings;
    OperationCounters counters;
    progress.check();
    auto& data = registry.resolve(request.at("data"), operation == "register", counters, timings, progress);
    auto& model = registry.resolve(request.at("model"), operation == "register", counters, timings, progress);
    require(!data.mesh, "INVALID_MESH", "이동 데이터에는 점 좌표만 전달해야 합니다.");
    const auto domain = parseSearchDomain(request.at("search_domain"));
    if (domain) {
        require(operation == "distance" && request.at("stage") == "PREPARING" && bool(model.mesh),
                "INVALID_SEARCH_DOMAIN", "고정 검색 범위는 준비 단계의 메시 거리 조회에만 사용할 수 있습니다.");
        validateDomainContains(*domain, *data.cloud);
        validateDomainContains(*domain, *model.cloud);
    }
    progress.check();
    Json identity = {{"protocol_version", kSessionProtocol}, {"session_id", request.at("session_id")},
                     {"job_id", request.at("job_id")}, {"input_fingerprint", request.at("input_fingerprint")}};
    Json result = sessionResultBase(identity, threads);
    result["icp_statistics"] = nullptr;
    result["status"] = "completed";
    result["operation"] = operation;
    result["stage"] = request.at("stage");
    result["sequence"] = request.at("sequence");
    result["data_geometry_id"] = request.at("data").at("geometry_id");
    result["model_geometry_id"] = request.at("model").at("geometry_id");
    registry.prepareBvh(model, domain ? &*domain : nullptr, timings, counters, progress, threads.effective);
    if (operation == "register") {
        auto params = parameters(request.at("parameters"), data.cloud->size(), model.cloud->size(), bool(model.mesh), threads.effective);
        Transform transform;
        double rms = -1;
        unsigned count = 0;
        progress.setSubstage("KERNEL");
        const auto kernelStarted = Clock::now();
        CCCoreLib::ICPMeshRegistrationContext icpContext;
        struct QueryContext { GeometryRegistry* registry; CachedGeometry* model; OperationCounters* counters; };
        QueryContext queryContext { &registry, &model, &counters };
        // CCCoreLib가 관리하는 표본과 최근접 점 저장소를 레지스트리의 동일 트리로 조회합니다.
        icpContext.exactMeshDistances = [](void* opaque, CCCoreLib::GenericIndexedCloudPersist* query,
                                          Cloud* cpSet, unsigned threadCount, CCCoreLib::GenericProgressCallback* callback) -> int {
            auto& state = *static_cast<QueryContext*>(opaque);
            return state.registry->queryBvh(*state.model, *query, cpSet, threadCount, callback, *state.counters);
        };
        icpContext.exactMeshDistancesData = &queryContext;
        const auto code = model.mesh
            ? CCCoreLib::ICPRegistrationTools::RegisterWithMeshContext(
                model.cloud.get(), model.mesh.get(), data.cloud.get(), params, transform, rms, count, icpContext, &progress)
            : CCCoreLib::ICPRegistrationTools::Register(
                model.cloud.get(), nullptr, data.cloud.get(), params, transform, rms, count, &progress);
        timings.kernelSeconds += elapsedSeconds(kernelStarted);
        progress.check();
        if (code >= CCCoreLib::ICPRegistrationTools::ICP_ERROR)
            throw WorkerError("NATIVE_REGISTRATION_ERROR", "CCCoreLib 등록 연산이 실패했습니다.", code);
        require(std::isfinite(rms) && rms >= 0 && count >= 3 && std::isfinite(transform.s) && transform.s > 0,
                "INVALID_NATIVE_RESULT", "코어 등록 결과의 거리·개수·배율이 유효하지 않습니다.");
        require(!params.adjustScale || (transform.s >= params.minScale - 1e-12 && transform.s <= params.maxScale + 1e-12),
                "INVALID_NATIVE_RESULT", "코어의 반환 배율이 요청한 경계를 벗어났습니다.");
        progress.setSubstage("SERIALIZE");
        const auto serializationStarted = Clock::now();
        result["matrix_4x4"] = matrixJson(transform);
        result["scale"] = transform.s;
        result["final_rms"] = rms;
        result["final_point_count"] = count;
        if (model.mesh) result["icp_statistics"] = icpStatisticsJson(icpContext.statistics);
        result["native_code"] = code;
        timings.serializationSeconds += elapsedSeconds(serializationStarted);
    } else {
        progress.setSubstage("KERNEL");
        const auto kernelStarted = Clock::now();
        const auto code = model.mesh
            ? registry.queryBvh(model, *data.cloud, nullptr, threads.effective, &progress, counters)
            : computeDistances(*data.cloud, *model.cloud, nullptr, &progress, threads.effective);
        timings.kernelSeconds += elapsedSeconds(kernelStarted);
        progress.check();
        if (code < CCCoreLib::DistanceComputationTools::SUCCESS)
            throw WorkerError("NATIVE_DISTANCE_ERROR", "CCCoreLib 거리 평가가 실패했습니다.", code);
        progress.setSubstage("SERIALIZE");
        const auto serializationStarted = Clock::now();
        std::vector<double> distances(data.cloud->size());
        for (unsigned index = 0; index < data.cloud->size(); ++index) {
            if ((index & 65535u) == 0) progress.check();
            distances[index] = double(data.cloud->getPointScalarValue(index));
            require(std::isfinite(distances[index]) && distances[index] >= 0,
                    "INVALID_NATIVE_RESULT", "코어가 유효하지 않은 거리를 반환했습니다.");
        }
        const Json distanceName = request.at("distances_file");
        atomicWrite(childPath(root, distanceName), reinterpret_cast<const char*>(distances.data()),
                    distances.size() * sizeof(double));
        result["distances_file"] = distanceName;
        result["count"] = distances.size();
        result["native_code"] = code;
        timings.serializationSeconds += elapsedSeconds(serializationStarted);
    }
    sessionStatistics.addOperation(counters);
    result["timings"] = timingJson(timings);
    result["operation_statistics"] = counterJson(counters);
    result["elapsed_seconds"] = elapsedSeconds(commandStarted);
    return result;
}

// 세션 명령 파일명에서 양의 4자리 순번만 해석합니다.
std::optional<unsigned> commandSequence(const fs::path& path) {
    static const std::wregex pattern(LR"(^command-([0-9]{4})\.request\.json$)");
    std::wsmatch match;
    const auto name = path.filename().wstring();
    if (!std::regex_match(name, match, pattern)) return std::nullopt;
    const auto sequence = static_cast<unsigned>(std::stoul(match[1].str()));
    require(sequence >= 1, "INVALID_SEQUENCE", "명령 순번은 1부터 시작해야 합니다.");
    return sequence;
}

// 단조 순번에 해당하는 고정 명령 파일 상대경로를 만듭니다.
std::string commandName(unsigned sequence, const char* suffix) {
    std::ostringstream stream;
    stream << "native/command-" << std::setw(4) << std::setfill('0') << sequence << suffix;
    return stream.str();
}

// 세션·작업·입력 식별자와 현재 명령 순번이 모두 같은지 검사합니다.
void validateCommandIdentity(const Json& command, const Json& identity, unsigned expectedSequence) {
    require(command.at("protocol_version") == kSessionProtocol, "PROTOCOL_MISMATCH",
            "세션에 v2 요청이나 다른 네이티브 프로토콜을 혼용할 수 없습니다.");
    for (const char* name : {"session_id", "job_id", "input_fingerprint"})
        require(command.at(name) == identity.at(name), "SESSION_IDENTITY_MISMATCH",
                "명령의 세션·작업 식별자가 시작 요청과 다릅니다.");
    require(unsignedField(command, "sequence", 1, 9999) == expectedSequence,
            "INVALID_SEQUENCE", "명령 JSON 순번이 파일 순번과 다릅니다.");
}

// 현재 순번 외 요청 파일을 누락·중복·역순 오류로 거부합니다.
std::optional<fs::path> findExpectedCommand(const fs::path& directory, unsigned expectedSequence) {
    std::optional<fs::path> expected;
    for (const auto& item : fs::directory_iterator(directory)) {
        if (!item.is_regular_file()) continue;
        const auto sequence = commandSequence(item.path());
        if (!sequence.has_value()) continue;
        require(*sequence == expectedSequence,
                *sequence < expectedSequence ? "DUPLICATE_SEQUENCE" : "INVALID_SEQUENCE",
                *sequence < expectedSequence ? "이미 처리한 명령 순번이 다시 제출되었습니다."
                                             : "앞선 명령이 누락되었거나 명령 순서가 뒤바뀌었습니다.");
        require(!expected.has_value(), "DUPLICATE_SEQUENCE", "같은 명령 순번이 중복 제출되었습니다.");
        expected = item.path();
    }
    return expected;
}

// 세션 명령의 고정 출력 경로와 입력 경로 충돌을 실행 전에 검사합니다.
void validateSessionCommandPaths(const Json& command, const fs::path& root, unsigned sequence) {
    const auto expectedResponse = childPath(root, commandName(sequence, ".response.json"));
    const auto expectedProgress = childPath(root, commandName(sequence, ".progress.json"));
    require(childPath(root, command.at("response_file")) == expectedResponse,
            "INVALID_PATH", "명령 응답 경로가 순번별 고정 경로와 다릅니다.");
    require(childPath(root, command.at("progress_file")) == expectedProgress,
            "INVALID_PATH", "명령 진행 경로가 순번별 고정 경로와 다릅니다.");
    require(!fs::exists(expectedResponse) && !fs::exists(expectedProgress),
            "DUPLICATE_SEQUENCE", "명령 출력 파일이 실행 전에 이미 존재합니다.");
    if (command.at("operation") == "close") return;
    const auto expectedDistances = childPath(root, commandName(sequence, ".distances.bin"));
    require(childPath(root, command.at("distances_file")) == expectedDistances,
            "INVALID_PATH", "거리 출력 경로가 순번별 고정 경로와 다릅니다.");
    require(!fs::exists(expectedDistances), "DUPLICATE_SEQUENCE", "거리 출력 파일이 실행 전에 이미 존재합니다.");
    std::set<fs::path> paths {expectedResponse, expectedProgress, expectedDistances};
    for (const char* role : {"data", "model"}) {
        const auto& entity = command.at(role);
        require(paths.insert(childPath(root, entity.at("positions_file"))).second,
                "PATH_COLLISION", "명령 입력·출력 파일 경로가 서로 겹칩니다.");
        if (entity.contains("triangles_file"))
            require(paths.insert(childPath(root, entity.at("triangles_file"))).second,
                    "PATH_COLLISION", "명령 입력·출력 파일 경로가 서로 겹칩니다.");
    }
}

// 세션 작업 하나를 ready부터 close까지 단일 프로세스에서 처리합니다.
std::pair<Json, int> runSession(const fs::path& sessionRequestPath) {
    const auto sessionStarted = Clock::now();
    ThreadSettings threads;
    threads.hardware = hardwareThreadCount();
    Json identity = {{"protocol_version", kSessionProtocol}};
    fs::path sessionResultPath;
    try {
        require(sessionRequestPath.is_absolute(), "INVALID_PATH", "세션 요청 파일은 절대경로여야 합니다.");
        const auto canonicalRequest = fs::canonical(sessionRequestPath);
        require(canonicalRequest.filename() == L"native-session-request.json", "INVALID_PATH",
                "세션 요청 파일명이 고정 계약과 다릅니다.");
        const auto root = canonicalRequest.parent_path();
        auto request = readJsonObject(canonicalRequest);
        requireExactKeys(request, {"protocol_version", "session_id", "job_id", "input_fingerprint", "parameters",
                                   "ready_file", "result_file", "cancel_file", "command_directory"},
                         "세션 시작 요청 필드가 고정 계약과 다릅니다.");
        require(request.at("protocol_version") == kSessionProtocol, "PROTOCOL_MISMATCH",
                "세션 시작 요청은 v5 네이티브 프로토콜이어야 합니다.");
        identity["session_id"] = safeIdentifier(request, "session_id");
        identity["job_id"] = safeIdentifier(request, "job_id");
        identity["input_fingerprint"] = safeIdentifier(request, "input_fingerprint");
        require(root.filename().u8string() == identity.at("job_id").get<std::string>(),
                "SESSION_IDENTITY_MISMATCH", "세션 작업 ID와 작업 폴더 이름이 다릅니다.");
        requireExactKeys(request.at("parameters"), {"max_threads"},
                         "세션 스레드 설정 필드가 고정 계약과 다릅니다.");
        threads = resolveThreadSettings(request);
        configureScheduler(threads.effective);
        require(request.at("ready_file") == "native/session-ready.json"
                    && request.at("result_file") == "native/session-result.json"
                    && request.at("cancel_file") == "cancel.request"
                    && request.at("command_directory") == "native",
                "INVALID_PATH", "세션 제어 파일 경로가 고정 계약과 다릅니다.");
        const auto readyPath = childPath(root, request.at("ready_file"));
        sessionResultPath = childPath(root, request.at("result_file"));
        const auto cancelPath = childPath(root, request.at("cancel_file"));
        const auto commandDirectory = childPath(root, request.at("command_directory"));
        require(fs::is_directory(commandDirectory), "INVALID_PATH", "세션 명령 디렉터리가 없습니다.");
        require(readyPath != sessionResultPath && readyPath != cancelPath && sessionResultPath != cancelPath,
                "PATH_COLLISION", "세션 ready·결과·취소 경로가 서로 겹칩니다.");
        require(!fs::exists(readyPath) && !fs::exists(sessionResultPath),
                "DUPLICATE_SESSION", "세션 출력 파일이 시작 전에 이미 존재합니다.");
        SessionStatistics statistics;
        statistics.peakWorkingSet = peakWorkingSetBytes();
        GeometryRegistry registry(root, statistics);
        Json ready = sessionResultBase(identity, threads);
        ready["status"] = "ready";
        ready["process_id"] = GetCurrentProcessId();
        ready["elapsed_seconds"] = elapsedSeconds(sessionStarted);
        ready["process_startup_seconds"] = ready["elapsed_seconds"];
        writeJson(readyPath, ready);
        Progress sessionCancellation({}, cancelPath);
        unsigned expectedSequence = 1;
        while (true) {
            sessionCancellation.check();
            const auto commandPath = findExpectedCommand(commandDirectory, expectedSequence);
            if (!commandPath.has_value()) {
                Sleep(10);
                continue;
            }
            const auto acceptedPath = acceptCommandFile(*commandPath);
            auto command = readJsonObject(acceptedPath);
            const auto operation = requiredText(command, "operation", 32);
            if (operation == "close") {
                requireExactKeys(command, {"protocol_version", "session_id", "job_id", "input_fingerprint",
                                           "sequence", "operation", "stage", "response_file", "progress_file"},
                                 "세션 close 명령 필드가 고정 계약과 다릅니다.");
            } else {
                requireExactKeys(command, {"protocol_version", "session_id", "job_id", "input_fingerprint",
                                           "sequence", "operation", "stage", "data", "model", "parameters",
                                           "response_file", "progress_file", "distances_file", "search_domain"},
                                 "세션 계산 명령 필드가 고정 계약과 다릅니다.");
            }
            validateCommandIdentity(command, identity, expectedSequence);
            require(operation == "close" || operation == "register" || operation == "distance",
                    "INVALID_OPERATION", "지원하지 않는 세션 명령 종류입니다.");
            const auto stage = requiredText(command, "stage", 64);
            validateSessionCommandPaths(command, root, expectedSequence);
            const auto responsePath = childPath(root, command.at("response_file"));
            const auto progressPath = childPath(root, command.at("progress_file"));
            Progress progress(progressPath, cancelPath, identity, expectedSequence, operation, stage);
            progress.start();
            if (operation == "close") {
                Json closed = sessionResultBase(identity, threads);
                closed["status"] = "closed";
                closed["operation"] = "close";
                closed["stage"] = stage;
                closed["sequence"] = expectedSequence;
                closed["statistics"] = statistics.toJson();
                closed["geometry_statistics"] = registry.summary();
                closed["session_elapsed_seconds"] = elapsedSeconds(sessionStarted);
                closed["session_exit_reason"] = "closed";
                closed["elapsed_seconds"] = closed["session_elapsed_seconds"];
                progress.finish("closed");
                writeJson(responsePath, closed);
                writeJson(sessionResultPath, closed);
                return {closed, 0};
            }
            try {
                auto result = executeSessionOperation(command, root, progress, threads, registry, statistics);
                progress.finish("completed");
                writeJson(responsePath, result);
                registry.releaseCommandScoped();
            } catch (const WorkerError& error) {
                Json failure = sessionResultBase(identity, threads);
                failure["status"] = error.code == "CANCELLED" ? "cancelled" : "error";
                failure["operation"] = operation;
                failure["stage"] = stage;
                failure["sequence"] = expectedSequence;
                failure["code"] = error.code;
                failure["message"] = error.what();
                failure["native_code"] = error.nativeCode;
                failure["elapsed_seconds"] = elapsedSeconds(sessionStarted);
                try {
                    const auto status = failure["status"].get<std::string>();
                    progress.finish(status.c_str(), false);
                    writeJson(responsePath, failure);
                } catch (...) {}
                throw;
            }
            ++expectedSequence;
        }
    } catch (const WorkerError& error) {
        Json result = sessionResultBase(identity, threads);
        result["status"] = error.code == "CANCELLED" ? "cancelled" : "error";
        result["code"] = error.code;
        result["message"] = error.what();
        result["native_code"] = error.nativeCode;
        result["session_elapsed_seconds"] = elapsedSeconds(sessionStarted);
        result["session_exit_reason"] = error.code == "CANCELLED" ? "cancelled" : "error";
        result["elapsed_seconds"] = result["session_elapsed_seconds"];
        try { if (!sessionResultPath.empty()) writeJson(sessionResultPath, result); } catch (...) {}
        return {result, error.code == "CANCELLED" ? 2 : 1};
    } catch (const std::bad_alloc&) {
        Json result = sessionResultBase(identity, threads);
        result["status"] = "error";
        result["code"] = "OUT_OF_MEMORY";
        result["message"] = "연산에 필요한 메모리가 부족합니다.";
        result["session_elapsed_seconds"] = elapsedSeconds(sessionStarted);
        result["session_exit_reason"] = "error";
        result["elapsed_seconds"] = result["session_elapsed_seconds"];
        try { if (!sessionResultPath.empty()) writeJson(sessionResultPath, result); } catch (...) {}
        return {result, 1};
    } catch (const Json::exception&) {
        Json result = sessionResultBase(identity, threads);
        result["status"] = "error";
        result["code"] = "INVALID_REQUEST";
        result["message"] = "요청 JSON의 형식 또는 필수 필드가 올바르지 않습니다.";
        result["session_elapsed_seconds"] = elapsedSeconds(sessionStarted);
        result["session_exit_reason"] = "error";
        result["elapsed_seconds"] = result["session_elapsed_seconds"];
        try { if (!sessionResultPath.empty()) writeJson(sessionResultPath, result); } catch (...) {}
        return {result, 1};
    } catch (const std::exception&) {
        Json result = sessionResultBase(identity, threads);
        result["status"] = "error";
        result["code"] = "IO_OR_RUNTIME_ERROR";
        result["message"] = "세션 파일 접근 또는 네이티브 실행 중 오류가 발생했습니다.";
        result["session_elapsed_seconds"] = elapsedSeconds(sessionStarted);
        result["session_exit_reason"] = "error";
        result["elapsed_seconds"] = result["session_elapsed_seconds"];
        try { if (!sessionResultPath.empty()) writeJson(sessionResultPath, result); } catch (...) {}
        return {result, 1};
    }
}

// 독립 워커에서 기존 오류 모드를 보존하면서 치명적 오류의 대화상자만 억제합니다.
void configureHeadlessErrorReporting() {
    SetErrorMode(GetErrorMode() | SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
}

// Windows 유니코드 인자를 받아 독립 작업과 환경 검사를 실행합니다.
int wmain(int argc, wchar_t** argv) {
    configureHeadlessErrorReporting();
    const auto started = Clock::now();
    fs::path output, progressPath;
    Json result;
    int exitCode = 0;
    ThreadSettings threads;
    threads.hardware = hardwareThreadCount();
    try {
        if (argc == 2 && std::wstring(argv[1]) == L"--check-environment") {
            threads.requested = threads.hardware;
            threads.effective = threads.hardware;
            configureScheduler(threads.effective);
            result = checkEnvironment(threads);
        }
        else if (argc == 3 && std::wstring(argv[1]) == L"--session") {
            auto outcome = runSession(fs::path(argv[2]));
            return outcome.second;
        }
        else {
            require(argc == 3 && std::wstring(argv[1]) == L"--request", "INVALID_ARGUMENT",
                    "사용법: --session 절대경로, --request v2절대경로 또는 --check-environment");
            fs::path requestPath(argv[2]);
            require(requestPath.is_absolute(), "INVALID_PATH", "요청 파일은 절대경로로 지정해야 합니다.");
            requestPath = fs::canonical(requestPath);
            require(fs::file_size(requestPath) <= 1048576, "INVALID_REQUEST", "요청 JSON 크기가 허용 범위를 넘었습니다.");
            std::ifstream stream(requestPath, std::ios::binary);
            auto request = Json::parse(stream);
            threads = resolveThreadSettings(request);
            configureScheduler(threads.effective);
            const auto root = requestPath.parent_path();
            validatePaths(request, root, requestPath);
            output = childPath(root, request.at("output_file"));
            progressPath = childPath(root, request.at("progress_file"));
            Progress progress(progressPath, childPath(root, request.at("cancel_file")));
            progress.start();
            result = execute(request, root, progress, threads);
        }
    } catch (const WorkerError& error) {
        result = baseResult(threads);
        result["status"] = error.code == "CANCELLED" ? "cancelled" : "error";
        result["code"] = error.code; result["message"] = error.what(); result["native_code"] = error.nativeCode;
        exitCode = error.code == "CANCELLED" ? 2 : 1;
    } catch (const std::bad_alloc&) {
        result = baseResult(threads); result["status"] = "error"; result["code"] = "OUT_OF_MEMORY";
        result["message"] = "연산에 필요한 메모리가 부족합니다."; exitCode = 1;
    } catch (const Json::exception&) {
        result = baseResult(threads); result["status"] = "error"; result["code"] = "INVALID_REQUEST";
        result["message"] = "요청 JSON의 형식 또는 필수 필드가 올바르지 않습니다."; exitCode = 1;
    } catch (const std::exception&) {
        result = baseResult(threads); result["status"] = "error"; result["code"] = "IO_OR_RUNTIME_ERROR";
        result["message"] = "작업 파일 접근 또는 네이티브 실행 중 오류가 발생했습니다."; exitCode = 1;
    }
    result["elapsed_seconds"] = std::chrono::duration<double>(Clock::now() - started).count();
    try {
        if (!progressPath.empty()) writeJson(progressPath, {{"protocol_version", kProtocol}, {"status", result["status"]}, {"percent", exitCode == 0 ? 100 : 0}});
        if (!output.empty()) writeJson(output, result);
    } catch (const WorkerError& error) {
        exitCode = 1; result["status"] = "error"; result["code"] = "OUTPUT_IO_ERROR";
        result["message"] = "최종 상태 파일을 기록하지 못했습니다."; result["native_code"] = error.nativeCode;
        // 진행 기록이 실패해도 최종 오류를 결과 파일과 표준 출력에 동일하게 남깁니다.
        try { if (!output.empty()) writeJson(output, result); } catch (const std::exception&) {}
    } catch (const std::exception&) {
        exitCode = 1; result["status"] = "error"; result["code"] = "OUTPUT_IO_ERROR";
        result["message"] = "최종 결과를 직렬화하지 못했습니다.";
    }
    std::cout << result.dump() << std::endl;
    return exitCode;
}
