#include "mesh_cleanup.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>
#include <limits>
#include <thread>

#include <oneapi/tbb/blocked_range.h>
#include <oneapi/tbb/parallel_for.h>
#include <oneapi/tbb/task_arena.h>

#if defined(_WIN32)
#include <Windows.h>
#endif

namespace {
constexpr std::uint64_t block_size = 65'536;

// 요청 스레드 수를 운영체제의 전체 활성 논리 프로세서 수로 제한합니다.
int thread_limit(std::uint32_t requested)
{
#if defined(_WIN32)
    const auto available = GetActiveProcessorCount(ALL_PROCESSOR_GROUPS);
#else
    const auto available = std::thread::hardware_concurrency();
#endif
    const auto bounded = std::min<std::uint64_t>(requested, std::max<std::uint64_t>(1, available));
    return static_cast<int>(std::min<std::uint64_t>(bounded, std::numeric_limits<int>::max()));
}

// 세 성분 제곱합을 넘파이 축 합산과 같은 왼쪽 결합 순서로 계산합니다.
double squared_sum(const double* value)
{
    return (value[0] * value[0] + value[1] * value[1]) + value[2] * value[2];
}

// 면 하나의 좌표와 변을 검사하고 원래의 정밀도별 퇴화 판정을 수행합니다.
std::uint32_t inspect_face(const double* positions, const std::uint32_t* face,
                          bool native_precision, std::uint8_t& valid)
{
    double corners[3][3];
    for (unsigned corner = 0; corner < 3; ++corner) {
        for (unsigned axis = 0; axis < 3; ++axis) {
            const double value = positions[static_cast<std::size_t>(face[corner]) * 3 + axis];
            corners[corner][axis] = native_precision ? static_cast<double>(static_cast<float>(value)) : value;
            if (!std::isfinite(corners[corner][axis])) {
                return 1;
            }
        }
    }
    double first[3], second[3];
    for (unsigned axis = 0; axis < 3; ++axis) {
        first[axis] = corners[1][axis] - corners[0][axis];
        second[axis] = corners[2][axis] - corners[0][axis];
        if (!std::isfinite(first[axis]) || !std::isfinite(second[axis])) {
            return 2;
        }
    }
    if (!native_precision) {
        double scale = 0.0;
        for (unsigned axis = 0; axis < 3; ++axis) {
            scale = std::max(scale, std::max(std::abs(first[axis]), std::abs(second[axis])));
        }
        if (scale == 0.0) {
            scale = 1.0;
        }
        for (unsigned axis = 0; axis < 3; ++axis) {
            first[axis] /= scale;
            second[axis] /= scale;
        }
    }
    const double cross[3] = {
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0]
    };
    if (native_precision) {
        valid = squared_sum(cross) > (squared_sum(first) * squared_sum(second)) * 1e-16;
    } else {
        const double maximum = std::max(std::max(std::abs(cross[0]), std::abs(cross[1])), std::abs(cross[2]));
        valid = maximum > 32.0 * std::numeric_limits<double>::epsilon();
    }
    return 0;
}
}

// 메시 검사 호출 규약의 버전을 반환합니다.
std::uint32_t colmap_mesh_cleanup_abi()
{
    return 1;
}

// 제한된 면 묶음마다 병렬 검사하고 호출 스레드에서 참조 마스크와 취소를 처리합니다.
std::int32_t colmap_mesh_cleanup(
    const double* positions, std::uint64_t point_count,
    const std::uint32_t* triangles, std::uint64_t face_count,
    std::uint32_t native_precision, std::uint32_t max_threads,
    std::uint8_t* valid, std::uint8_t* used, int (*cancel_probe)(void))
{
    try {
        if (point_count > std::numeric_limits<std::uint32_t>::max()
            || point_count > std::numeric_limits<std::size_t>::max() / (3 * sizeof(double))
            || face_count > std::numeric_limits<std::size_t>::max() / (3 * sizeof(std::uint32_t))
            || (point_count && (!positions || !used))
            || (face_count && (!triangles || !valid))
            || native_precision > 1 || max_threads == 0) {
            return 1;
        }
        if (face_count) {
            std::memset(valid, 0, static_cast<std::size_t>(face_count));
        }
        if (point_count) {
            std::memset(used, 0, static_cast<std::size_t>(point_count));
        }
        for (std::uint64_t start = 0; start < point_count; start += block_size) {
            if (cancel_probe && cancel_probe()) {
                return 6;
            }
            const auto end = std::min(point_count, start + block_size);
            for (auto point = start; point < end; ++point) {
                for (unsigned axis = 0; axis < 3; ++axis) {
                    if (!std::isfinite(positions[point * 3 + axis])) {
                        return 2;
                    }
                }
            }
        }
        oneapi::tbb::task_arena arena(thread_limit(max_threads));
        for (std::uint64_t start = 0; start < face_count; start += block_size) {
            if (cancel_probe && cancel_probe()) {
                return 6;
            }
            const auto end = std::min(face_count, start + block_size);
            for (auto face = start; face < end; ++face) {
                for (unsigned corner = 0; corner < 3; ++corner) {
                    if (triangles[face * 3 + corner] >= point_count) {
                        return 3;
                    }
                }
            }
            std::atomic<std::uint32_t> errors{0};
            // 호출별 실행 영역 안에서 서로 다른 면 출력만 병렬로 기록합니다.
            arena.execute([&] {
                // 연속된 면 범위를 검사하고 오류 종류를 원자적으로 합칩니다.
                oneapi::tbb::parallel_for(oneapi::tbb::blocked_range<std::uint64_t>(start, end, 256),
                    [&](const oneapi::tbb::blocked_range<std::uint64_t>& range) {
                        std::uint32_t local_errors = 0;
                        for (auto face = range.begin(); face != range.end(); ++face) {
                            local_errors |= inspect_face(positions, triangles + face * 3,
                                                         native_precision != 0, valid[face]);
                        }
                        if (local_errors) {
                            errors.fetch_or(local_errors, std::memory_order_relaxed);
                        }
                    });
            });
            const auto block_errors = errors.load(std::memory_order_relaxed);
            if (block_errors & 1) {
                return 4;
            }
            if (block_errors & 2) {
                return 5;
            }
            for (auto face = start; face < end; ++face) {
                if (valid[face]) {
                    for (unsigned corner = 0; corner < 3; ++corner) {
                        used[triangles[face * 3 + corner]] = 1;
                    }
                }
            }
        }
        if (cancel_probe && cancel_probe()) {
            return 6;
        }
        return 0;
    } catch (...) {
        return 7;
    }
}
