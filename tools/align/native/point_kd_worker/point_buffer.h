#pragma once
#include <Eigen/Core>
#include <array>
#include <utility>
#include <vector>

namespace point_kd {
using Vec3 = Eigen::Vector3d;
using Coordinates = std::array<double, 3>;
static_assert(sizeof(Coordinates) == 3 * sizeof(double), "좌표 저장 형식은 연속된 배정도 세 성분이어야 합니다.");

// 파일에서 읽은 좌표 버퍼를 소유하고 Eigen에는 복사 없는 원소 뷰를 제공합니다.
class PointBuffer {
public:
    // 비어 있는 좌표 버퍼를 준비합니다.
    PointBuffer() = default;
    // 적재한 연속 배열의 소유권만 이전합니다.
    explicit PointBuffer(std::vector<Coordinates>&& values) : values_(std::move(values)) {}
    // 누적 법선에 사용할 영벡터 배열을 준비합니다.
    explicit PointBuffer(std::size_t count) : values_(count, Coordinates{0, 0, 0}) {}
    // 원소 수를 반환합니다.
    std::size_t size() const { return values_.size(); }
    // 좌표 배열을 변경하지 않는 Eigen 뷰를 반환합니다.
    Eigen::Map<const Vec3> operator[](std::size_t index) const { return Eigen::Map<const Vec3>(values_[index].data()); }
    // 소유 버퍼의 법선을 누적하거나 정규화할 때 사용하는 뷰를 반환합니다.
    Eigen::Map<Vec3> operator[](std::size_t index) { return Eigen::Map<Vec3>(values_[index].data()); }
private:
    std::vector<Coordinates> values_;
};
}
