"""제품 설정을 바꾸지 않고 사전 고정한 ICP 조기 종료 후보를 분석합니다."""

import argparse
import json
import math
from pathlib import Path
import struct
import tempfile


POLICY = {
    "minimum_accepted_iterations": 20,
    "consecutive_stable_iterations": 5,
    "rms_absolute_decrease_max": 1e-7,
    "rms_relative_decrease_max": 1e-5,
    "rms_relative_denominator_min": 1e-12,
    "step_translation_norm_max": 1e-6,
    "step_rotation_radians_max": 1e-5,
    "step_scale_change_max": 1e-6,
}


def identity_matrix():
    """누적 변환의 초기 항등행렬을 생성합니다."""
    return [[float(row == column) for column in range(4)] for row in range(4)]


def validate_matrix(value):
    """유한한 4×4 아핀 행렬만 분석 입력으로 허용합니다."""
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("변환 행렬은 4×4 배열이어야 합니다.")
    if any(not isinstance(row, list) or len(row) != 4 for row in value):
        raise ValueError("변환 행렬은 4×4 배열이어야 합니다.")
    if any(type(item) not in (int, float) or not math.isfinite(item) for row in value for item in row):
        raise ValueError("변환 행렬에 유한하지 않은 수 또는 잘못된 값이 있습니다.")
    if any(abs(value[3][column] - float(column == 3)) > 1e-12 for column in range(4)):
        raise ValueError("변환 행렬의 마지막 행은 아핀 형식이어야 합니다.")
    return [[float(item) for item in row] for row in value]


def multiply_matrices(left, right):
    """채택된 단계 변환을 이전 누적 변환의 왼쪽에 곱합니다."""
    return [[math.fsum(left[row][inner] * right[inner][column] for inner in range(4))
             for column in range(4)] for row in range(4)]


def step_metrics(matrix):
    """행렬식으로 균일 배율을 분리하고 단계 이동량과 회전각을 구합니다."""
    a, b, c = (matrix[row][:3] for row in range(3))
    determinant = (a[0] * (b[1] * c[2] - b[2] * c[1])
                   - a[1] * (b[0] * c[2] - b[2] * c[0])
                   + a[2] * (b[0] * c[1] - b[1] * c[0]))
    if not math.isfinite(determinant) or determinant <= 0:
        raise ValueError("단계 변환의 행렬식은 유한한 양수여야 합니다.")
    scale = determinant ** (1.0 / 3.0)
    rotation = [[matrix[row][column] / scale for column in range(3)] for row in range(3)]
    orthogonality_error = max(abs(math.fsum(rotation[k][row] * rotation[k][column] for k in range(3))
                                  - float(row == column)) for row in range(3) for column in range(3))
    if orthogonality_error > 1e-7:
        raise ValueError("단계 변환은 회전과 균일 배율로 구성되어야 합니다.")
    cosine = (math.fsum(rotation[index][index] for index in range(3)) - 1.0) / 2.0
    return {
        "translation_norm": math.sqrt(math.fsum(matrix[row][3] ** 2 for row in range(3))),
        "rotation_radians": math.acos(max(-1.0, min(1.0, cosine))),
        "scale": scale,
        "scale_change": abs(scale - 1.0),
    }


def analyze_records(records):
    """고정 조건의 연속 만족 여부와 채택 단계 누적으로 첫 수렴 후보를 찾습니다."""
    total = identity_matrix()
    accepted_count = 0
    stable_count = 0
    candidate = None
    previous = None
    diagnostics = []
    last_accepted_rms = None
    last_accepted_count = None
    for record in records:
        iteration = record.get("iteration")
        point_count = record.get("point_count")
        rms = record.get("rms")
        accepted = record.get("accepted")
        if type(iteration) is not int or iteration < 0 or (previous and iteration != previous["iteration"] + 1):
            raise ValueError("반복 번호는 0부터 빠짐없이 증가해야 합니다.")
        if previous is None and (iteration != 0 or accepted is not False):
            raise ValueError("첫 기록은 미채택 상태의 초기 평가여야 합니다.")
        if type(point_count) is not int or point_count < 0:
            raise ValueError("평가 점 수는 음수가 아닌 정수여야 합니다.")
        if type(rms) not in (int, float) or not math.isfinite(rms) or rms < 0 or type(accepted) is not bool:
            raise ValueError("RMS와 채택 여부가 유효하지 않습니다.")
        matrix = validate_matrix(record.get("step_matrix_4x4"))
        metrics = step_metrics(matrix)
        resets = []
        decrease = relative = None
        if iteration == 0:
            last_accepted_rms, last_accepted_count = rms, point_count
            resets.append("INITIAL_EVALUATION")
        elif not accepted:
            resets.append("NOT_ACCEPTED")
        else:
            accepted_count += 1
            total = multiply_matrices(matrix, total)
            last_accepted_rms, last_accepted_count = rms, point_count
            decrease = previous["rms"] - rms
            relative = decrease / max(abs(previous["rms"]), POLICY["rms_relative_denominator_min"])
            if accepted_count < POLICY["minimum_accepted_iterations"]:
                resets.append("BEFORE_MINIMUM_ITERATIONS")
            if point_count != previous["point_count"]:
                resets.append("POINT_COUNT_CHANGED")
            if decrease < 0:
                resets.append("RMS_INCREASED")
            if decrease > POLICY["rms_absolute_decrease_max"] or relative > POLICY["rms_relative_decrease_max"]:
                resets.append("RMS_CHANGE_TOO_LARGE")
            if metrics["translation_norm"] > POLICY["step_translation_norm_max"]:
                resets.append("TRANSLATION_TOO_LARGE")
            if metrics["rotation_radians"] > POLICY["step_rotation_radians_max"]:
                resets.append("ROTATION_TOO_LARGE")
            if metrics["scale_change"] > POLICY["step_scale_change_max"]:
                resets.append("SCALE_CHANGE_TOO_LARGE")
        stable_count = 0 if resets else stable_count + 1
        diagnostic = {"iteration": iteration, "accepted": accepted, "accepted_iterations": accepted_count,
                      "point_count": point_count, "rms": rms, "rms_decrease": decrease,
                      "relative_rms_decrease": relative, "stable_streak": stable_count,
                      "reset_reasons": resets, **metrics}
        diagnostics.append(diagnostic)
        if candidate is None and stable_count >= POLICY["consecutive_stable_iterations"]:
            candidate = {**diagnostic, "matrix_4x4": [row[:] for row in total]}
        previous = record
    if previous is None:
        raise ValueError("반복 관찰 파일이 비어 있습니다.")
    return {"experimental_only": True, "thresholds_fixed_before_analysis": True, "policy": dict(POLICY),
            "earliest_possible_candidate_iteration": 24, "candidate": candidate,
            "final_trace_iteration": previous["iteration"], "iterations_attempted": previous["iteration"],
            "iterations_accepted": accepted_count, "final_trace_rms": last_accepted_rms,
            "final_trace_point_count": last_accepted_count, "final_trace_matrix_4x4": total,
            "iterations": diagnostics}


def displacement_summary(positions, candidate, final_matrix):
    """원래 정규화 표본 전체에서 두 변환의 위치 차이 RMS와 최대값을 구합니다."""
    if positions.stat().st_size % 24:
        raise ValueError("표본 파일은 좌표당 float64인 XYZ 레코드로 구성되어야 합니다.")
    count = 0
    square_sum = 0.0
    maximum = 0.0
    delta = [[candidate[row][column] - final_matrix[row][column] for column in range(4)] for row in range(3)]
    with positions.open("rb") as stream:
        while block := stream.read(24 * 8192):
            for point in struct.iter_unpack("<ddd", block):
                if not all(math.isfinite(value) for value in point):
                    raise ValueError("표본에 유한하지 않은 좌표가 있습니다.")
                # 실제 네이티브 입력과 같은 단정도 좌표로 변환합니다.
                point = struct.unpack("<fff", struct.pack("<fff", *point))
                if not all(math.isfinite(value) for value in point):
                    raise ValueError("표본 좌표가 네이티브 단정도 범위를 벗어났습니다.")
                square = math.fsum(
                    (math.fsum(delta[row][column] * point[column] for column in range(3))
                     + delta[row][3]) ** 2 for row in range(3))
                square_sum += square
                maximum = max(maximum, math.sqrt(square))
                count += 1
    if not count:
        raise ValueError("변위 평가 표본이 비어 있습니다.")
    return {"point_count": count, "rms": math.sqrt(square_sum / count), "maximum": maximum,
            "coordinate_space": "normalized", "native_input_cast": "float32"}


def run_self_tests():
    """작은 합성 관찰 기록으로 안정 초기화와 변환 누적 순서를 검증합니다."""
    def records(count=35):
        """RMS와 단계 변환이 일정한 최소 합성 관찰 기록을 만듭니다."""
        return [{"iteration": index, "point_count": 100, "rms": 1.0, "accepted": index > 0,
                 "step_matrix_4x4": identity_matrix()} for index in range(count + 1)]

    checks = 0
    assert analyze_records(records())["candidate"]["iteration"] == 24
    checks += 1
    changed = records()
    for row in changed[23:]:
        row["point_count"] = 99
    assert analyze_records(changed)["candidate"]["iteration"] == 28
    checks += 1
    rejected = records()
    rejected[23]["accepted"] = False
    assert analyze_records(rejected)["candidate"]["iteration"] == 28
    checks += 1
    moving = records()
    moving[23]["step_matrix_4x4"][0][3] = 1e-4
    assert analyze_records(moving)["candidate"]["iteration"] == 28
    checks += 1
    for initial_rms, decrease in ((100.0, 2e-7), (1e-4, 2e-9)):
        changing_rms = records()
        for row in changing_rms:
            row["rms"] = initial_rms - (decrease if row["iteration"] >= 23 else 0)
        assert analyze_records(changing_rms)["candidate"]["iteration"] == 28
        checks += 1
    composed = records(3)
    composed[1]["step_matrix_4x4"] = [[0., -2., 0., 1.], [2., 0., 0., 2.], [0., 0., 2., 3.], [0., 0., 0., 1.]]
    composed[2]["step_matrix_4x4"] = [[.5, 0., 0., 4.], [0., .5, 0., -1.], [0., 0., .5, 2.], [0., 0., 0., 1.]]
    composed[3]["accepted"] = False
    composed[3]["step_matrix_4x4"][0][3] = 999.
    result = analyze_records(composed)
    assert result["final_trace_matrix_4x4"] == [[0., -1., 0., 4.5], [1., 0., 0., 0.], [0., 0., 1., 3.5], [0., 0., 0., 1.]]
    assert result["iterations_attempted"] == 3 and result["iterations_accepted"] == 2
    assert result["candidate"] is None
    assert abs(step_metrics(composed[1]["step_matrix_4x4"])["rotation_radians"] - math.pi / 2) < 1e-12
    checks += 1
    with tempfile.TemporaryDirectory(prefix="icp-convergence-") as temporary:
        positions = Path(temporary) / "points.bin"
        positions.write_bytes(struct.pack("<6d", 1., 2., 3., 4., 5., 6.))
        translated = identity_matrix()
        translated[0][3], translated[1][3] = 3., 4.
        displacement = displacement_summary(positions, translated, identity_matrix())
        assert displacement["point_count"] == 2 and displacement["rms"] == displacement["maximum"] == 5.0
    checks += 1
    return {"self_tests": "passed", "checks": checks}


def main():
    """고정 임계값으로 관찰 파일을 분석하고 선택적으로 원래 결과와 표본 변위를 비교합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, nargs="?", help="초기 평가와 단계 변환을 기록한 JSONL 파일")
    parser.add_argument("--result", type=Path, help="원래 전체 등록의 matrix_4x4가 있는 결과 JSON")
    parser.add_argument("--positions", type=Path, help="정규화된 전체 등록 표본 XYZ float64 바이너리")
    parser.add_argument("--output", type=Path, help="분석 결과를 저장할 JSON 파일")
    parser.add_argument("--self-test", action="store_true", help="작은 합성 관찰 기록만 검증")
    args = parser.parse_args()
    try:
        if args.self_test:
            report = run_self_tests()
        else:
            if args.trace is None:
                parser.error("관찰 파일 또는 --self-test가 필요합니다.")
            with args.trace.open(encoding="utf-8") as stream:
                report = analyze_records(json.loads(line) for line in stream if line.strip())
            final_matrix = report["final_trace_matrix_4x4"]
            if args.result:
                original = json.loads(args.result.read_text(encoding="utf-8"))
                final_matrix = validate_matrix(original["matrix_4x4"])
                report["original_final_rms"] = original.get("final_rms")
                report["trace_vs_original_matrix_max_difference"] = max(
                    abs(final_matrix[row][column] - report["final_trace_matrix_4x4"][row][column])
                    for row in range(4) for column in range(4))
            report["comparison_final_matrix_4x4"] = final_matrix
            report["candidate_vs_final"] = None
            if report["candidate"]:
                candidate_matrix = report["candidate"]["matrix_4x4"]
                report["candidate_vs_final"] = {"matrix_max_difference": max(
                    abs(candidate_matrix[row][column] - final_matrix[row][column])
                    for row in range(4) for column in range(4))}
                if args.positions:
                    report["candidate_vs_final"]["sample_displacement"] = displacement_summary(
                        args.positions, candidate_matrix, final_matrix)
            report["trace_file"] = str(args.trace.resolve())
        encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        else:
            print(encoded, end="")
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
