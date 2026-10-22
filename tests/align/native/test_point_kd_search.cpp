#include "point_kd.h"
#include <cmath>
#include <iostream>
#include <random>
#include <stdexcept>

using point_kd::Vec3;
// 조건 위반을 예외로 전달하여 Release 빌드에서도 검사를 유지합니다.
void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

// 전수 비교용 좌표를 실제 파일 적재와 같은 연속 배열로 준비합니다.
point_kd::PointBuffer buffer_of(const std::vector<Vec3>& points) {
    std::vector<point_kd::Coordinates> packed;
    packed.reserve(points.size());
    for (const auto& point : points) packed.push_back({point.x(), point.y(), point.z()});
    return point_kd::PointBuffer(std::move(packed));
}

// 원본 버퍼 공유와 편향·중복 입력을 포함한 정확 검색 및 취소를 확인합니다.
int main() {
    try {
        std::mt19937_64 random(71403);
        std::uniform_real_distribution<double> uniform(-5, 5);
        std::vector<Vec3> points;
        for (std::size_t i = 0; i < 2063; ++i) points.emplace_back(uniform(random), uniform(random), uniform(random));
        points.push_back(Vec3::Constant(std::numeric_limits<double>::quiet_NaN()));
        const auto buffer = buffer_of(points);
        point_kd::Tree tree(buffer, []() {});
        require(tree.size() == points.size() - 1, "유효 원본점 수 오류");
        for (std::size_t trial = 0; trial < 500; ++trial) {
            const Vec3 query(uniform(random), uniform(random), uniform(random));
            const double radius = trial % 5 == 0 ? 0.001 : 0.5 + std::abs(uniform(random));
            const auto eligible = [trial](std::size_t i) { return (i + trial) % 7 == 0; };
            const auto actual = tree.nearest(query, radius * radius, eligible);
            auto expected = std::numeric_limits<std::size_t>::max();
            double best = radius * radius;
            bool spatial = false;
            for (std::size_t i = 0; i + 1 < points.size(); ++i) {
                const double d2 = (query - points[i]).squaredNorm();
                if (d2 <= radius * radius) spatial = true;
                if (eligible(i) && (d2 < best || (d2 == best && i < expected))) { expected = i; best = d2; }
            }
            require(actual.index == expected, "전수 적격 최근접 검색 불일치");
            require(actual.spatial_candidate == spatial, "반경 후보 통계 불일치");
            require(std::abs(actual.squared_distance - best) < 1e-12, "거리 불일치");
        }
        const std::vector<Vec3> ties = {Vec3(-1, 0, 0), Vec3(1, 0, 0), Vec3(0, 2, 0)};
        const auto tie_buffer = buffer_of(ties);
        point_kd::Tree tie_tree(tie_buffer, []() {});
        require(tie_tree.nearest(Vec3::Zero(), 1, [](std::size_t) { return true; }).index == 0, "동률 원본 번호 오류");
        require(tie_tree.nearest(Vec3(-1, 0, 0), 9, [](std::size_t i) { return i != 0; }).index == 1, "가장 가까운 부적격점 이후 탐색 오류");
        require(tie_tree.nearest(Vec3(-1, 0, 0), 9, [](std::size_t) { return true; }, 0).index == 1, "간격 측정 자기 제외 오류");
        bool cancelled = false;
        try { point_kd::Tree cancelled_tree(buffer, []() { throw std::runtime_error("취소"); }); }
        catch (const std::runtime_error&) { cancelled = true; }
        require(cancelled, "생성 취소 누락");

        std::vector<point_kd::Coordinates> raw{{1, 2, 3}, {4, 5, 6}};
        auto* original = raw[0].data();
        point_kd::PointBuffer owned(std::move(raw));
        require(owned[0].data() == original, "입력 소유권 이전에서 좌표가 복사됨");
        const auto& read_only = owned;
        require(read_only[0].data() == original, "읽기 전용 Eigen 뷰가 원본 버퍼와 다름");
        owned[0] += Vec3(2, 3, 4);
        require(original[0] == 3 && original[1] == 5 && original[2] == 7, "법선 누적 뷰가 버퍼에 반영되지 않음");

        std::vector<Vec3> skewed;
        for (int i = 0; i < 1000; ++i) skewed.emplace_back(i * .001, (i % 3) * .001, 0);
        skewed.emplace_back(10000, 0, 0);
        const auto skewed_buffer = buffer_of(skewed);
        point_kd::Tree skewed_tree(skewed_buffer, []() {});
        for (std::size_t i = 0; i < skewed.size(); ++i)
            require(skewed_tree.nearest(skewed[i], 0, [](std::size_t) { return true; }).index == i, "편향 분할에서 원본점 누락");

        const point_kd::PointBuffer repeated(std::vector<point_kd::Coordinates>(20000, {1, 2, 3}));
        bool cancel_search = false;
        point_kd::Tree repeated_tree(repeated, [&]() { if (cancel_search) throw std::runtime_error("취소"); });
        require(repeated_tree.size() == 20000, "중복 좌표가 원본 색인에서 제거됨");
        require(repeated_tree.nearest(Vec3(1, 2, 3), 0, [](std::size_t i) { return i == 19999; }).index == 19999, "분리 불가능한 큰 잎 검색 오류");
        cancel_search = true; cancelled = false;
        try { repeated_tree.nearest(Vec3(1, 2, 3), 0, [](std::size_t) { return true; }); }
        catch (const std::runtime_error&) { cancelled = true; }
        require(cancelled, "분리 불가능한 큰 잎에서 취소 누락");
        std::cout << "정확 KD 전수 비교 500회와 버퍼 공유·편향·중복·동률·적격·취소 검사 통과\n";
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
