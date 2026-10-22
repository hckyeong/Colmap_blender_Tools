#pragma once
#include "point_buffer.h"
#include <array>
#include <cstdint>
#include <functional>
#include <limits>
#include <vector>

namespace point_kd {
struct Node {
    Vec3 low = Vec3::Zero(), high = Vec3::Zero();
    std::size_t begin = 0, end = 0;
    int left = -1, right = -1;
};
struct Neighbor {
    std::size_t index = std::numeric_limits<std::size_t>::max();
    double squared_distance = 0.0;
    bool spatial_candidate = false;
};
class Tree {
public:
    // 좌표를 복사하지 않고 원본 번호로 공간 중간값 KD 트리를 생성합니다.
    Tree(const PointBuffer& positions, std::function<void()> cancellation);
    // 임시 소유 버퍼를 참조해 수명이 끝나는 사용을 금지합니다.
    Tree(PointBuffer&& positions, std::function<void()> cancellation) = delete;
    // 반경과 적격 조건을 동시에 만족하는 정확 최근접 원본점을 찾습니다.
    Neighbor nearest(const Vec3& query, double squared_radius, const std::function<bool(std::size_t)>& eligible, std::size_t excluded = std::numeric_limits<std::size_t>::max()) const;
    // 트리에 포함한 유효 원본점 수를 반환합니다.
    std::size_t size() const { return indices_.size(); }
private:
    // 최장축의 공간 중간 위치로 분할하며 분리 불가능한 구간은 잎으로 둡니다.
    int build(std::size_t begin, std::size_t end);
    // 경계상자 하한으로 가지를 제거하면서 적격점을 탐색합니다.
    void search(int node, const Vec3& query, double radius, const std::function<bool(std::size_t)>& eligible, std::size_t excluded, Neighbor& best, std::size_t& visited) const;
    const PointBuffer& positions_;
    std::function<void()> cancellation_;
    std::vector<std::size_t> indices_;
    std::vector<Node> nodes_;
};
}
