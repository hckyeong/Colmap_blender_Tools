"""Modifier 도메인 영속 코덱의 무손실·fail-closed 계약을 검증합니다."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tools.modifier.core.domain_persistence import (
    DomainChecksumError,
    DomainPersistenceError,
    DomainPersistenceHeader,
    DomainPersistencePayload,
    DomainSourceIdentityError,
    DomainSourceMetadata,
    UnsupportedDomainSchemaError,
    compare_domain_generations,
    decode_domain_payload,
    encode_domain_payload,
    make_domain_payload,
    operations_from_payload,
    source_descriptor_from_payload,
)
from tools.modifier.core.edit_replay import history_hash
from tools.modifier.core.edit_types import (
    EDIT_SCHEMA_VERSION,
    REMOVE_POINTS_OPERATION,
    EditOperation,
)
from tools.modifier.core.model import SourceDescriptor


_DATASET_UUID = "12345678-1234-5678-9234-567812345678"
_DOMAIN_UUID = "87654321-4321-4678-9234-876543218765"
_SOURCE_FINGERPRINT = "a" * 64


def _canonical_json(value) -> str:
    """테스트 envelope를 제품 코덱과 같은 canonical JSON으로 만듭니다."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _resign(envelope: dict) -> str:
    """의도적으로 바꾼 payload에 유효한 checksum을 다시 부여합니다."""

    canonical_payload = _canonical_json(envelope["payload"]).encode("utf-8")
    envelope["checksum"] = hashlib.sha256(canonical_payload).hexdigest()
    return _canonical_json(envelope)


def _operation(
    revision: int = 0,
    *,
    dataset_uuid: str = _DATASET_UUID,
    source_fingerprint: str = _SOURCE_FINGERPRINT,
) -> EditOperation:
    """지정 revision의 유효한 Point 제거 operation을 생성합니다."""

    point_id = revision + 1
    return EditOperation(
        schema_version=EDIT_SCHEMA_VERSION,
        operation_id=f"00000000-0000-4000-8000-{revision + 1:012d}",
        operation_type=REMOVE_POINTS_OPERATION,
        dataset_uuid=dataset_uuid,
        source_fingerprint=source_fingerprint,
        requested_point3d_ids=(point_id,),
        requested_image_ids=(),
        removed_point3d_ids=(point_id,),
        removed_image_ids=(),
        removed_camera_ids=(),
        removed_frame_ids=(),
        removed_rig_ids=(),
        previous_revision=revision,
        resulting_revision=revision + 1,
        created_at_utc=f"2026-09-02T00:00:0{revision}+00:00",
    )


class DomainPersistenceTests(unittest.TestCase):
    """도메인 payload의 encode·decode와 무결성 검증을 확인합니다."""

    def setUp(self) -> None:
        """각 테스트에 절대경로 SourceDescriptor와 편집 한 건을 준비합니다."""

        self._temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self._temporary_directory.name).resolve()
        model_directory = root / "sparse" / "0"
        self.source = SourceDescriptor(
            dataset_root=root,
            sparse_root=model_directory.parent,
            model_directory=model_directory,
            images_directory=root / "images",
            input_format="text",
            layout="legacy",
            selected_files=(
                model_directory / "cameras.txt",
                model_directory / "images.txt",
                model_directory / "points3D.txt",
            ),
            source_fingerprint=_SOURCE_FINGERPRINT,
            dataset_uuid=_DATASET_UUID,
            schema_version=1,
        )
        self.operations = (_operation(),)
        self.payload = make_domain_payload(
            self.source,
            self.operations,
            domain_uuid=_DOMAIN_UUID,
            domain_generation=7,
            edit_revision=1,
            history_hash_value=history_hash(self.operations),
        )

    def tearDown(self) -> None:
        """테스트용 임시 디렉터리를 정리합니다."""

        self._temporary_directory.cleanup()

    def test_round_trip_reconstructs_source_and_operations(self) -> None:
        """canonical envelope 왕복이 payload와 source·operation을 보존합니다."""

        encoded = encode_domain_payload(self.payload)
        decoded = decode_domain_payload(encoded.encode("utf-8"))

        self.assertEqual(decoded, self.payload)
        self.assertEqual(source_descriptor_from_payload(decoded), self.source)
        self.assertEqual(operations_from_payload(decoded), self.operations)

    def test_json_key_order_is_deterministic(self) -> None:
        """입력이 같으면 key 순서와 UTF-8 JSON 바이트가 항상 같습니다."""

        first = encode_domain_payload(self.payload)
        second = encode_domain_payload(self.payload)

        self.assertEqual(first, second)
        self.assertEqual(first, _canonical_json(json.loads(first)))
        self.assertIn("경로", _canonical_json({"경로": "데이터"}))

    def test_checksum_tampering_is_rejected(self) -> None:
        """checksum을 갱신하지 않은 payload 변조는 decode에서 거부됩니다."""

        envelope = json.loads(encode_domain_payload(self.payload))
        envelope["payload"]["header"]["domain_generation"] = 8

        with self.assertRaises(DomainChecksumError):
            decode_domain_payload(_canonical_json(envelope))

    def test_unknown_domain_and_source_schema_are_rejected(self) -> None:
        """알 수 없는 domain·source schema는 자동 변환 없이 거부됩니다."""

        for section, schema_version in (("header", 999), ("source", 999)):
            with self.subTest(section=section):
                envelope = json.loads(encode_domain_payload(self.payload))
                envelope["payload"][section]["schema_version"] = schema_version
                with self.assertRaises(UnsupportedDomainSchemaError):
                    decode_domain_payload(_resign(envelope))

    def test_invalid_uuid_generation_and_revision_are_rejected(self) -> None:
        """비표준 UUID와 음수 generation·revision은 encode 전에 거부됩니다."""

        invalid_headers = (
            replace(
                self.payload.header,
                domain_uuid="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            ),
            replace(self.payload.header, dataset_uuid="not-a-uuid"),
            replace(self.payload.header, domain_generation=-1),
            replace(self.payload.header, edit_revision=-1),
            replace(self.payload.header, edit_revision=2),
        )
        for header in invalid_headers:
            with self.subTest(header=header):
                with self.assertRaises(DomainPersistenceError):
                    encode_domain_payload(replace(self.payload, header=header))

    def test_decode_rejects_invalid_uuid_generation_and_revision(self) -> None:
        """유효한 checksum 안의 잘못된 식별자·세대·revision도 decode 후 거부됩니다."""

        invalid_values = (
            ("domain_uuid", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
            ("dataset_uuid", "not-a-uuid"),
            ("domain_generation", -1),
            ("edit_revision", -1),
            ("edit_revision", 2),
        )
        for field_name, value in invalid_values:
            with self.subTest(field_name=field_name, value=value):
                envelope = json.loads(encode_domain_payload(self.payload))
                envelope["payload"]["header"][field_name] = value
                with self.assertRaises(DomainPersistenceError):
                    decode_domain_payload(_resign(envelope))

    def test_operation_revision_must_be_contiguous_from_zero(self) -> None:
        """operation 자체는 유효해도 도메인 revision 0부터 연속해야 합니다."""

        operation = _operation(1)
        payload = replace(
            self.payload,
            header=replace(
                self.payload.header,
                edit_revision=1,
                history_hash=history_hash((operation,)),
            ),
            operations=(operation,),
        )

        with self.assertRaisesRegex(DomainPersistenceError, "연속"):
            encode_domain_payload(payload)

    def test_history_hash_mismatch_is_rejected(self) -> None:
        """저장된 history_hash가 operation 목록과 다르면 거부됩니다."""

        payload = replace(
            self.payload,
            header=replace(self.payload.header, history_hash="b" * 64),
        )

        with self.assertRaisesRegex(DomainPersistenceError, "history_hash"):
            encode_domain_payload(payload)

    def test_operation_source_identity_mismatch_is_rejected(self) -> None:
        """operation의 dataset·fingerprint가 source token과 다르면 거부됩니다."""

        cases = (
            _operation(dataset_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            _operation(source_fingerprint="b" * 64),
        )
        for operation in cases:
            with self.subTest(operation=operation):
                payload = replace(
                    self.payload,
                    header=replace(
                        self.payload.header,
                        history_hash=history_hash((operation,)),
                    ),
                    operations=(operation,),
                )
                with self.assertRaises(DomainSourceIdentityError):
                    encode_domain_payload(payload)

    def test_source_paths_must_be_absolute_and_selected_files_stay_in_boundary(self) -> None:
        """상대경로와 model source 밖 selected_files는 모두 거부됩니다."""

        relative_source = replace(self.payload.source, dataset_root="relative/dataset")
        outside_files = replace(
            self.payload.source,
            selected_files=(
                str(self.source.model_directory / "cameras.txt"),
                str(self.source.model_directory / "images.txt"),
                str(self.source.dataset_root / "outside" / "points3D.txt"),
            ),
        )
        for source in (relative_source, outside_files):
            with self.subTest(source=source):
                with self.assertRaises(DomainPersistenceError):
                    encode_domain_payload(replace(self.payload, source=source))

    def test_windows_absolute_paths_are_preserved_by_pathlib_validation(self) -> None:
        """Windows drive 절대경로를 host 문자열 변환 없이 검증·보존합니다."""

        source = DomainSourceMetadata(
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
        )
        payload = DomainPersistencePayload(
            header=DomainPersistenceHeader(
                schema_version=1,
                domain_uuid=_DOMAIN_UUID,
                domain_generation=0,
                dataset_uuid=_DATASET_UUID,
                edit_revision=0,
                history_hash=history_hash(()),
            ),
            source=source,
            operations=(),
        )

        decoded = decode_domain_payload(encode_domain_payload(payload))

        self.assertEqual(decoded.source, source)

    def test_runtime_snapshot_and_slot_names_are_not_serialized(self) -> None:
        """Runtime 전체 상태·inverse delta·active/staging 이름은 payload에 없습니다."""

        encoded = encode_domain_payload(self.payload)
        forbidden = (
            "baseline_reconstruction",
            "current_reconstruction",
            "indexes",
            "inverse_delta",
            "undo_commands",
            "redo",
            "active_slot",
            "staging_slot",
        )

        for field_name in forbidden:
            with self.subTest(field_name=field_name):
                self.assertNotIn(field_name, encoded)

    def test_generation_comparison_rejects_conflicts(self) -> None:
        """같은 도메인의 generation만 비교하고 동일 세대 분기는 거부합니다."""

        newer = replace(
            self.payload,
            header=replace(self.payload.header, domain_generation=8),
        )
        self.assertEqual(compare_domain_generations(self.payload, newer), -1)
        self.assertEqual(compare_domain_generations(newer, self.payload), 1)
        self.assertEqual(compare_domain_generations(self.payload, self.payload), 0)

        conflicting = replace(
            self.payload,
            header=replace(self.payload.header, history_hash="b" * 64),
        )
        with self.assertRaises(DomainPersistenceError):
            compare_domain_generations(self.payload, conflicting)


if __name__ == "__main__":
    unittest.main()
