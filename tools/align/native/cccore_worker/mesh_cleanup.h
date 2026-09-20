#pragma once

#include <cstdint>

#if defined(_WIN32)
#define COLMAP_MESH_EXPORT __declspec(dllexport)
#else
#define COLMAP_MESH_EXPORT __attribute__((visibility("default")))
#endif

extern "C" {
// 메시 검사 호출 규약의 버전을 반환합니다.
COLMAP_MESH_EXPORT std::uint32_t colmap_mesh_cleanup_abi();

// 원본 순서의 유효면과 참조 정점 마스크를 채우고 검사 상태를 반환합니다.
COLMAP_MESH_EXPORT std::int32_t colmap_mesh_cleanup(
    const double* positions, std::uint64_t point_count,
    const std::uint32_t* triangles, std::uint64_t face_count,
    std::uint32_t native_precision, std::uint32_t max_threads,
    std::uint8_t* valid, std::uint8_t* used, int (*cancel_probe)(void));
}
