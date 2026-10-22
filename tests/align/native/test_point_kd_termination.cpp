#include "relative_delta.h"
#include <iostream>
#include <stdexcept>
#include <string>

using Vec3 = Eigen::Vector3d;
using Mat4 = Eigen::Matrix4d;

// 릴리스 빌드에서도 검사 실패를 종료 코드로 전달합니다.
void require(bool value, const char* name) {
    if (!value) throw std::runtime_error(name);
}

// 실제 Artec 합성 호출과 같은 Z축 회전·이동 갱신을 구성합니다.
Mat4 increment(Vec3 translation, double angle) {
    Mat4 value = Mat4::Identity();
    value.topLeftCorner<3, 3>() = Eigen::AngleAxisd(angle, Vec3::UnitZ()).toRotationMatrix();
    value.topRightCorner<3, 1>() = translation;
    return value;
}

// 실제 Artec 반환값 10개와 경계·이력·회전 부호 계약을 독립 검증합니다.
int main() {
    struct Case { double current, previous, old_t, old_r, next_t, next_r; bool expected; };
    const Case cases[] = {
        {100.05,100,0,0,1,.1,true}, {100.05,100,2,.5,1,.1,true},
        {110,100,1,.1,1.0005,.10005,true}, {110,100,1,.1,1.0005,.101,false},
        {110,100,1,.1,1.01,.10005,false}, {110,100,1,.1,1.01,.101,false},
        {5e-13,0,0,0,1,.1,true}, {2e-12,0,0,0,1,.1,false},
        {110,100,1000,.1,1000.5,.10005,true}, {110,100,.001,.1,.0015,.10005,false}
    };
    const double tolerance = point_kd::relative_delta_default_tolerance;
    int checked = 0;
    for (const auto& test : cases) {
        point_kd::RelativeDeltaState state;
        state.previous_translation = Vec3(test.old_t, 0, 0);
        state.previous_rotation = Vec3(0, 0, test.old_r);
        const auto decision = state.evaluate(test.current, test.previous, increment(Vec3(test.next_t, 0, 0), test.next_r), tolerance, tolerance, tolerance);
        require(decision.converged == test.expected, "Artec 실측 사례의 반환값 불일치");
        if (std::string(decision.branch) == "ERROR_STABLE") {
            require(state.previous_translation == Vec3(test.old_t, 0, 0) && state.previous_rotation == Vec3(0, 0, test.old_r), "오차 조기 종료가 이력을 변경함");
            require(!decision.translation && !decision.rotation, "오차 조기 종료 후 변환 조건이 평가됨");
        } else {
            require(state.previous_translation == Vec3(test.next_t, 0, 0), "이동 이력 갱신 누락");
            require((state.previous_rotation - Vec3(0, 0, test.next_r)).norm() < 1e-13, "회전 벡터 이력 불일치");
        }
        ++checked;
    }
    point_kd::RelativeDeltaState boundary;
    require(!boundary.evaluate(1e-9, 0, increment(Vec3(1,0,0), .1), 1, 0, 0).converged, "오차 경계에 미만 대신 이하 사용");
    boundary = {};
    require(!boundary.evaluate(1, 0, increment(Vec3(1e-9,0,0), 0), 0, 1, 1).converged, "이동 경계에 미만 대신 이하 사용");
    boundary = {};
    require(boundary.evaluate(1, 0, increment(Vec3(1e-9,0,0), 0), 0, std::nextafter(1.0, 2.0), 1).converged, "이동 경계 직상 허용치 실패");
    boundary = {};
    require(boundary.evaluate(0, 0, Mat4::Identity(), tolerance, tolerance, tolerance).converged, "영 오차 분모 보호 실패");
    for (double angle : {0.0, 0.1, -0.1, 2.7, -2.7, 3.141592653589793}) {
        const auto value = point_kd::rotation_vector(increment(Vec3::Zero(), angle).topLeftCorner<3,3>());
        require((value - Vec3(0,0,angle)).norm() < 1e-13, "회전 축 또는 최단각 부호 불일치");
    }
    boundary.previous_rotation = Vec3(0, 0, .1);
    boundary.previous_translation = Vec3(1, 0, 0);
    require(!boundary.evaluate(110, 100, increment(Vec3(1,0,0), -.1), tolerance, tolerance, tolerance).converged, "같은 크기의 반대 회전을 안정으로 판정함");
    std::cout << "Artec 실측 " << checked << "건 및 경계·이력·회전 검증 통과\n";
}
