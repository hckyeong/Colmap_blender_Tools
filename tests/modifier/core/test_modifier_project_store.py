"""가짜 Text 컬렉션으로 Modifier 프로젝트 2-slot 저장 계약을 검증합니다."""

from __future__ import annotations

import unittest
from dataclasses import replace

from tools.modifier.blender import modifier_project_store as store
from tools.modifier.core.domain_persistence import (
    DomainPersistenceHeader,
    DomainPersistencePayload,
    DomainSourceMetadata,
    encode_domain_payload,
)
from tools.modifier.core.edit_replay import history_hash


_DOMAIN_UUID = "87654321-4321-4678-9234-876543218765"
_DATASET_UUID = "12345678-1234-5678-9234-567812345678"
_SOURCE_FINGERPRINT = "a" * 64


class _FakeText:
    """Blender Text의 본문·custom property·실패 지점을 흉내 냅니다."""

    def __init__(self, name: str) -> None:
        """빈 Text와 한 번만 동작하는 기록 실패 flag를 준비합니다."""

        self.name = name
        self.use_fake_user = False
        self.body = ""
        self.properties: dict[str, object] = {}
        self.fail_next_clear = False
        self.fail_next_write = False
        self.corrupt_next_write = False

    def get(self, key: str, default=None):
        """Blender ID custom property의 dict형 조회를 흉내 냅니다."""

        return self.properties.get(key, default)

    def __setitem__(self, key: str, value: object) -> None:
        """Blender ID custom property 기록을 흉내 냅니다."""

        self.properties[key] = value

    def clear(self) -> None:
        """Text 본문을 비우거나 예약된 단일 실패를 발생시킵니다."""

        if self.fail_next_clear:
            self.fail_next_clear = False
            raise RuntimeError("테스트용 clear 실패")
        self.body = ""

    def write(self, value: str) -> None:
        """Text 끝에 문자열을 쓰거나 예약된 실패·변조를 적용합니다."""

        if self.fail_next_write:
            self.fail_next_write = False
            raise RuntimeError("테스트용 write 실패")
        if self.corrupt_next_write:
            self.corrupt_next_write = False
            self.body += value + "손상"
            return
        self.body += value

    def as_string(self) -> str:
        """현재 Text 본문 전체를 반환합니다."""

        return self.body


class _FakeTexts:
    """Blender의 이름 기반 Text 컬렉션 최소 계약을 흉내 냅니다."""

    def __init__(self) -> None:
        """빈 Text 이름 사전을 준비합니다."""

        self.items: dict[str, _FakeText] = {}
        self.fail_write_on_new_names: set[str] = set()

    def get(self, name: str):
        """이름과 일치하는 Text 또는 None을 반환합니다."""

        return self.items.get(name)

    def new(self, name: str) -> _FakeText:
        """중복 이름을 거부하고 새 Text를 생성합니다."""

        if name in self.items:
            raise RuntimeError("테스트 Text 이름 중복")
        text = _FakeText(name)
        if name in self.fail_write_on_new_names:
            text.fail_next_write = True
        self.items[name] = text
        return text

    def remove(self, text: _FakeText) -> None:
        """동일 객체인 Text만 컬렉션에서 제거합니다."""

        if self.items.get(text.name) is not text:
            raise RuntimeError("제거할 테스트 Text가 없습니다")
        del self.items[text.name]


def _payload(
    generation: int,
    *,
    domain_uuid: str = _DOMAIN_UUID,
) -> DomainPersistencePayload:
    """빈 확정 이력을 가진 유효한 도메인 payload를 생성합니다."""

    return DomainPersistencePayload(
        header=DomainPersistenceHeader(
            schema_version=1,
            domain_uuid=domain_uuid,
            domain_generation=generation,
            dataset_uuid=_DATASET_UUID,
            edit_revision=0,
            history_hash=history_hash(()),
        ),
        source=DomainSourceMetadata(
            dataset_root="C:\\dataset",
            sparse_root="C:\\dataset\\sparse",
            model_directory="C:\\dataset\\sparse",
            images_directory="C:\\dataset\\images",
            input_format="binary",
            layout="legacy",
            selected_files=(
                "C:\\dataset\\sparse\\cameras.bin",
                "C:\\dataset\\sparse\\images.bin",
                "C:\\dataset\\sparse\\points3D.bin",
            ),
            source_fingerprint=_SOURCE_FINGERPRINT,
            schema_version=1,
        ),
        operations=(),
    )


def _tag_owned(text: _FakeText) -> None:
    """가짜 Text에 제품과 같은 owner·schema marker를 기록합니다."""

    text[store.TEXT_OWNER_KEY] = store.TEXT_OWNER_VALUE
    text[store.TEXT_SCHEMA_KEY] = 1


class ModifierProjectStoreTests(unittest.TestCase):
    """ACTIVE/STAGING 원자 저장과 fail-closed 읽기를 검증합니다."""

    def setUp(self) -> None:
        """각 테스트에 독립된 가짜 Blender Text 컬렉션을 준비합니다."""

        self.texts = _FakeTexts()

    def test_write_round_trip_uses_fixed_owned_slots(self) -> None:
        """STAGING 검증 뒤 ACTIVE가 같은 canonical payload와 marker를 가집니다."""

        payload = _payload(3)
        result = store.write_active_payload(payload, self.texts)
        active = self.texts.get(store.ACTIVE_TEXT_NAME)
        staging = self.texts.get(store.STAGING_TEXT_NAME)

        self.assertEqual(result, payload)
        self.assertEqual(store.read_active_payload(self.texts), payload)
        self.assertEqual(active.as_string(), encode_domain_payload(payload))
        self.assertEqual(staging.as_string(), encode_domain_payload(payload))
        for text in (active, staging):
            self.assertEqual(text.get(store.TEXT_OWNER_KEY), store.TEXT_OWNER_VALUE)
            self.assertEqual(text.get(store.TEXT_SCHEMA_KEY), 1)
            self.assertTrue(text.use_fake_user)

    def test_read_does_not_promote_valid_staging(self) -> None:
        """ACTIVE가 없으면 유효한 STAGING이 있어도 자동 승격하지 않습니다."""

        staging = self.texts.new(store.STAGING_TEXT_NAME)
        _tag_owned(staging)
        staging.write(encode_domain_payload(_payload(1)))

        self.assertIsNone(store.read_active_payload(self.texts))
        self.assertIsNone(self.texts.get(store.ACTIVE_TEXT_NAME))

    def test_staging_write_failure_preserves_existing_active(self) -> None:
        """STAGING 기록 실패는 기존 ACTIVE 본문과 marker를 변경하지 않습니다."""

        first = _payload(1)
        store.write_active_payload(first, self.texts)
        active = self.texts.get(store.ACTIVE_TEXT_NAME)
        staging = self.texts.get(store.STAGING_TEXT_NAME)
        original_body = active.as_string()
        original_properties = dict(active.properties)
        staging.fail_next_write = True

        with self.assertRaises(store.ModifierProjectStoreError):
            store.write_active_payload(_payload(2), self.texts)

        self.assertEqual(active.as_string(), original_body)
        self.assertEqual(active.properties, original_properties)
        self.assertEqual(staging.as_string(), "")
        self.assertEqual(store.read_active_payload(self.texts), first)

    def test_active_write_failure_rolls_back_body_and_markers(self) -> None:
        """ACTIVE clear 이후 write 실패도 이전 검증 상태로 되돌립니다."""

        first = _payload(4)
        store.write_active_payload(first, self.texts)
        active = self.texts.get(store.ACTIVE_TEXT_NAME)
        original_body = active.as_string()
        original_properties = dict(active.properties)
        active.fail_next_write = True

        with self.assertRaises(store.ModifierProjectStoreError):
            store.write_active_payload(_payload(5), self.texts)

        self.assertEqual(active.as_string(), original_body)
        self.assertEqual(active.properties, original_properties)
        self.assertEqual(store.read_active_payload(self.texts), first)

    def test_active_post_write_corruption_rolls_back(self) -> None:
        """ACTIVE 기록 직후 원문 변조가 감지되면 이전 payload를 복구합니다."""

        first = _payload(6)
        store.write_active_payload(first, self.texts)
        active = self.texts.get(store.ACTIVE_TEXT_NAME)
        active.corrupt_next_write = True

        with self.assertRaises(store.ModifierProjectStoreError):
            store.write_active_payload(_payload(7), self.texts)

        self.assertEqual(store.read_active_payload(self.texts), first)

    def test_new_active_failure_restores_slot_absence(self) -> None:
        """최초 ACTIVE 승격 실패는 불완전한 ACTIVE Text를 남기지 않습니다."""

        self.texts.fail_write_on_new_names.add(store.ACTIVE_TEXT_NAME)

        with self.assertRaises(store.ModifierProjectStoreError):
            store.write_active_payload(_payload(1), self.texts)

        self.assertIsNone(store.read_active_payload(self.texts))
        self.assertIsNone(self.texts.get(store.ACTIVE_TEXT_NAME))

    def test_corrupt_active_fails_closed_before_staging_change(self) -> None:
        """손상된 ACTIVE는 읽기·쓰기 모두 거부하고 STAGING을 건드리지 않습니다."""

        store.write_active_payload(_payload(1), self.texts)
        active = self.texts.get(store.ACTIVE_TEXT_NAME)
        staging = self.texts.get(store.STAGING_TEXT_NAME)
        staging_body = staging.as_string()
        active.body += "손상"

        with self.assertRaises(store.ModifierProjectStoreError):
            store.read_active_payload(self.texts)
        with self.assertRaises(store.ModifierProjectStoreError):
            store.write_active_payload(_payload(2), self.texts)

        self.assertTrue(active.as_string().endswith("손상"))
        self.assertEqual(staging.as_string(), staging_body)

    def test_foreign_or_unknown_schema_slots_are_never_overwritten(self) -> None:
        """고정 이름 충돌과 알 수 없는 marker schema를 자동 소유하지 않습니다."""

        cases = ((None, None), (store.TEXT_OWNER_VALUE, 999))
        for owner, schema in cases:
            with self.subTest(owner=owner, schema=schema):
                texts = _FakeTexts()
                active = texts.new(store.ACTIVE_TEXT_NAME)
                active.write("사용자 본문")
                if owner is not None:
                    active[store.TEXT_OWNER_KEY] = owner
                if schema is not None:
                    active[store.TEXT_SCHEMA_KEY] = schema

                with self.assertRaises(store.ModifierProjectStoreError):
                    store.read_active_payload(texts)
                with self.assertRaises(store.ModifierProjectStoreError):
                    store.write_active_payload(_payload(1), texts)
                self.assertEqual(active.as_string(), "사용자 본문")

    def test_generation_regression_and_same_generation_fork_are_rejected(self) -> None:
        """같은 도메인의 낮은 generation과 동일 세대 분기 저장을 거부합니다."""

        current = _payload(5)
        store.write_active_payload(current, self.texts)
        active_body = self.texts.get(store.ACTIVE_TEXT_NAME).as_string()

        with self.assertRaises(store.ModifierProjectStoreError):
            store.write_active_payload(_payload(4), self.texts)

        forked_source = replace(current.source, source_fingerprint="b" * 64)
        forked = replace(current, source=forked_source)
        with self.assertRaises(store.ModifierProjectStoreError):
            store.write_active_payload(forked, self.texts)

        self.assertEqual(self.texts.get(store.ACTIVE_TEXT_NAME).as_string(), active_body)

    def test_new_domain_can_replace_active_after_staging_validation(self) -> None:
        """명시적으로 새 UUID인 도메인은 generation 0부터 ACTIVE를 교체할 수 있습니다."""

        store.write_active_payload(_payload(9), self.texts)
        replacement = _payload(
            0,
            domain_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

        store.write_active_payload(replacement, self.texts)

        self.assertEqual(store.read_active_payload(self.texts), replacement)

    def test_clear_staging_only_clears_owned_staging_body(self) -> None:
        """명시적 STAGING 정리는 ACTIVE를 보존하고 foreign Text는 거부합니다."""

        payload = _payload(1)
        store.write_active_payload(payload, self.texts)
        self.assertTrue(store.clear_staging_payload(self.texts))
        self.assertEqual(self.texts.get(store.STAGING_TEXT_NAME).as_string(), "")
        self.assertEqual(store.read_active_payload(self.texts), payload)

        empty = _FakeTexts()
        self.assertFalse(store.clear_staging_payload(empty))
        foreign = empty.new(store.STAGING_TEXT_NAME)
        foreign.write("사용자 본문")
        with self.assertRaises(store.ModifierProjectStoreError):
            store.clear_staging_payload(empty)
        self.assertEqual(foreign.as_string(), "사용자 본문")

    def test_injected_collection_avoids_bpy_and_runtime_snapshots(self) -> None:
        """가짜 컬렉션 경로는 bpy 없이 동작하며 Text에는 작은 payload만 기록합니다."""

        self.assertNotIn("bpy", store.__dict__)
        store.write_active_payload(_payload(1), self.texts)
        body = self.texts.get(store.ACTIVE_TEXT_NAME).as_string()
        forbidden = (
            "baseline_reconstruction",
            "current_reconstruction",
            "inverse_delta",
            "point_dictionary",
            "camera_dictionary",
        )
        for field_name in forbidden:
            with self.subTest(field_name=field_name):
                self.assertNotIn(field_name, body)


if __name__ == "__main__":
    unittest.main()
