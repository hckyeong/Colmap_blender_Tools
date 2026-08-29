"""순수 KDB-tree의 균형·결정성·정확한 최근접 검색을 검증합니다."""

from __future__ import annotations

from math import dist
import random
import unittest

from tools.projection_paint.core.kdb_tree import (
    KDBPointPage,
    KDBRegionPage,
    KDBSourcePoint,
    KDBTreeError,
    build_kdb_tree,
    find_nearest,
    validate_kdb_tree,
)


def _leaf_depths(page, depth: int = 0) -> tuple[int, ...]:
    """테스트용으로 모든 Point Page의 깊이를 반환합니다."""

    if isinstance(page, KDBPointPage):
        return (depth,)
    return tuple(
        leaf_depth
        for entry in page.entries
        for leaf_depth in _leaf_depths(entry.child, depth + 1)
    )


def _page_sizes(page) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """테스트용으로 Region Page와 Point Page의 항목 수를 반환합니다."""

    if isinstance(page, KDBPointPage):
        return (), (len(page.entries),)
    child_sizes = tuple(_page_sizes(entry.child) for entry in page.entries)
    return (
        (len(page.entries),)
        + tuple(size for region_sizes, _point_sizes in child_sizes for size in region_sizes),
        tuple(size for _region_sizes, point_sizes in child_sizes for size in point_sizes),
    )


class KDBTreeTests(unittest.TestCase):
    """KDB-tree가 입력 분포와 관계없이 계약을 지키는지 검증합니다."""

    def test_pages_are_height_balanced_and_within_capacity(self) -> None:
        """모든 Leaf 깊이가 같고 각 Page가 지정 용량을 넘지 않습니다."""

        points = tuple(
            KDBSourcePoint((float(index), float(index % 7), 0.0), index, index)
            for index in range(305)
        )
        tree = build_kdb_tree(points, point_page_capacity=16, region_page_capacity=8)
        region_sizes, point_sizes = _page_sizes(tree.root)
        self.assertEqual(set(_leaf_depths(tree.root)), {tree.height})
        self.assertTrue(all(1 <= size <= 8 for size in region_sizes))
        self.assertTrue(all(1 <= size <= 16 for size in point_sizes))
        validate_kdb_tree(tree)

    def test_input_order_does_not_change_nearest_results(self) -> None:
        """입력 순서를 섞어도 동일한 Record 순서로 검색됩니다."""

        points = [
            KDBSourcePoint((float(index % 9), float(index // 9), 0.0), index, 100 - index)
            for index in range(81)
        ]
        shuffled = list(points)
        random.Random(42).shuffle(shuffled)
        first = build_kdb_tree(points, point_page_capacity=4, region_page_capacity=4)
        second = build_kdb_tree(shuffled, point_page_capacity=4, region_page_capacity=4)
        for origin in ((0.1, 0.2, 0.0), (4.3, 5.2, 1.0), (8.0, 8.0, 0.0)):
            self.assertEqual(find_nearest(first, origin), find_nearest(second, origin))

    def test_nearest_matches_brute_force_for_random_points(self) -> None:
        """무작위 Query의 최근접 8개가 완전탐색 결과와 정확히 일치합니다."""

        generator = random.Random(19)
        points = tuple(
            KDBSourcePoint(
                tuple(generator.uniform(-20.0, 20.0) for _axis in range(3)),
                index,
                1000 - index,
            )
            for index in range(305)
        )
        tree = build_kdb_tree(points)
        for _query_index in range(50):
            origin = tuple(generator.uniform(-25.0, 25.0) for _axis in range(3))
            expected = tuple(
                point.record_index
                for point in sorted(
                    points,
                    key=lambda point: (
                        dist(point.position, origin),
                        point.tie_break,
                        point.record_index,
                    ),
                )[:8]
            )
            actual = tuple(result.record_index for result in find_nearest(tree, origin, 8))
            self.assertEqual(actual, expected)

    def test_duplicate_positions_keep_deterministic_record_order(self) -> None:
        """같은 위치의 Camera Record를 Tie-break와 Record Index 순으로 반환합니다."""

        points = (
            KDBSourcePoint((1.0, 2.0, 3.0), 7, 30),
            KDBSourcePoint((1.0, 2.0, 3.0), 8, 10),
            KDBSourcePoint((1.0, 2.0, 3.0), 9, 10),
            KDBSourcePoint((8.0, 2.0, 3.0), 10, 1),
        )
        tree = build_kdb_tree(points, point_page_capacity=1, region_page_capacity=2)
        results = find_nearest(tree, (1.0, 2.0, 3.0), 3)
        self.assertEqual(tuple(result.record_index for result in results), (8, 9, 7))

    def test_single_point_and_invalid_inputs_are_explicit(self) -> None:
        """단일 Point는 검색되고 빈 값·비유한 값·중복 Record는 거부됩니다."""

        tree = build_kdb_tree((KDBSourcePoint((1.0, 2.0, 3.0), 5, 50),))
        self.assertEqual(find_nearest(tree, (0.0, 0.0, 0.0))[0].record_index, 5)
        with self.assertRaisesRegex(KDBTreeError, "Point가 없습니다"):
            build_kdb_tree(())
        with self.assertRaisesRegex(KDBTreeError, "유한한"):
            build_kdb_tree((KDBSourcePoint((float("nan"), 0.0, 0.0), 1, 1),))
        with self.assertRaisesRegex(KDBTreeError, "중복"):
            build_kdb_tree(
                (
                    KDBSourcePoint((0.0, 0.0, 0.0), 1, 1),
                    KDBSourcePoint((1.0, 0.0, 0.0), 1, 2),
                )
            )


if __name__ == "__main__":
    unittest.main()
