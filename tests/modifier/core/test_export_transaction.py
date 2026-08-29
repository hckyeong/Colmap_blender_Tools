from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tools.modifier.core import export_backup, export_transaction, export_validation
from tools.modifier.core.binary_io import read_model_binary
from tools.modifier.core.export_backup import create_backup_snapshot
from tools.modifier.core.export_transaction import execute_export
from tools.modifier.core.export_types import ExportTransactionError
from tools.modifier.core.export_validation import validate_export_request
from tools.modifier.core.fingerprint import source_fingerprint
from tools.modifier.core.loader import load_reconstruction
from tools.modifier.core.model import ImageRecord, Point2DRecord, Reconstruction, ResolvedLayout
from tools.modifier.core.text_io import read_model_text
from tests.modifier.fixture_utils import (
    make_legacy_binary,
    make_legacy_text,
    make_modern_binary,
    make_modern_text,
)


def _loaded_text(root: Path):
    model_directory, images_directory = make_legacy_text(root)
    return load_reconstruction(
        root,
        model_directory,
        images_directory,
        "TEXT",
    )


def _loaded_binary(root: Path):
    model_directory, images_directory = make_legacy_binary(root)
    return load_reconstruction(
        root,
        model_directory,
        images_directory,
        "BINARY",
    )


def _point_removed(model: Reconstruction) -> Reconstruction:
    removed_ids = set(model.points3d)
    images: dict[int, ImageRecord] = {}
    for image_id, image in model.images.items():
        points2d = tuple(
            Point2DRecord(
                point.x,
                point.y,
                None if point.point3d_id in removed_ids else point.point3d_id,
            )
            for point in image.points2d
        )
        images[image_id] = replace(image, points2d=points2d)
    return Reconstruction(
        cameras=dict(model.cameras),
        images=images,
        points3d={},
        rigs=dict(model.rigs),
        frames=dict(model.frames),
    )


def _fingerprint_from_source(source) -> str:
    return source_fingerprint(
        ResolvedLayout(
            source.model_directory,
            source.input_format,
            source.layout,
            source.selected_files,
        )
    )


class ExportTransactionSuccessTests(unittest.TestCase):
    def test_backup_disabled_overwrites_only_selected_text_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            edited = _point_removed(loaded.reconstruction)
            model_directory = loaded.source.model_directory
            (model_directory / "cameras.bin").write_bytes(b"opposite format")
            (model_directory / "notes.keep").write_text("사용자 파일", encoding="utf-8")

            plan = validate_export_request(
                loaded.source,
                edited,
                backup_enabled=False,
            )
            result = execute_export(plan, edited)

            self.assertEqual(result.loaded.reconstruction, edited)
            self.assertEqual(read_model_text(model_directory, modern=False), edited)
            self.assertEqual(result.source_fingerprint, _fingerprint_from_source(result.loaded.source))
            self.assertIsNone(result.backup_snapshot)
            self.assertFalse((root / "sparse_backup").exists())
            self.assertEqual((model_directory / "cameras.bin").read_bytes(), b"opposite format")
            self.assertEqual(
                (model_directory / "notes.keep").read_text(encoding="utf-8"),
                "사용자 파일",
            )
            self.assertEqual(
                tuple(path.name for path in result.replaced_files),
                ("cameras.txt", "images.txt", "points3D.txt"),
            )

    def test_text_source_can_be_converted_to_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            edited = _point_removed(loaded.reconstruction)
            model_directory = loaded.source.model_directory
            (model_directory / "cameras.bin").write_bytes(
                b"stale binary"
            )

            result = execute_export(
                validate_export_request(
                    loaded.source,
                    edited,
                    backup_enabled=False,
                    output_format="BINARY",
                ),
                edited,
            )

            self.assertEqual(
                read_model_binary(model_directory, modern=False),
                edited,
            )
            self.assertEqual(result.loaded.source.input_format, "binary")
            self.assertEqual(
                tuple(
                    path.name
                    for path in result.loaded.source.selected_files
                ),
                ("cameras.bin", "images.bin", "points3D.bin"),
            )
            self.assertFalse(list(model_directory.glob("*.txt")))
            self.assertEqual(
                tuple(path.name for path in result.removed_files),
                ("cameras.txt", "images.txt", "points3D.txt"),
            )

    def test_binary_source_can_be_converted_to_text_with_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_binary(root)
            original = loaded.reconstruction
            edited = _point_removed(original)
            model_directory = loaded.source.model_directory
            stale_text = "stale opposite format\n"
            (model_directory / "cameras.txt").write_text(
                stale_text,
                encoding="utf-8",
            )

            result = execute_export(
                validate_export_request(
                    loaded.source,
                    edited,
                    backup_enabled=True,
                    output_format="TEXT",
                ),
                edited,
            )

            self.assertEqual(
                read_model_text(model_directory, modern=False),
                edited,
            )
            self.assertEqual(result.loaded.source.input_format, "text")
            self.assertFalse(list(model_directory.glob("*.bin")))
            snapshot = result.backup_snapshot
            assert snapshot is not None
            self.assertEqual(
                read_model_binary(snapshot / "0", modern=False),
                original,
            )
            self.assertFalse((snapshot / "0" / "cameras.txt").exists())
            self.assertEqual(
                (
                    snapshot
                    / "0"
                    / ".format_conversion_rollback"
                    / "cameras.txt"
                ).read_text(encoding="utf-8"),
                stale_text,
            )

    def test_backup_enabled_creates_plain_modern_binary_snapshot_under_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_directory, images_directory = make_modern_binary(root)
            loaded = load_reconstruction(
                root,
                model_directory,
                images_directory,
                "BINARY",
            )
            original = loaded.reconstruction
            edited = _point_removed(original)
            plan = validate_export_request(loaded.source, edited, backup_enabled=True)

            result = execute_export(plan, edited)

            self.assertIsNotNone(result.backup_snapshot)
            snapshot = result.backup_snapshot
            assert snapshot is not None
            self.assertEqual(snapshot.parent, root / "sparse_backup")
            self.assertTrue(snapshot.name.endswith(loaded.source.source_fingerprint[:8]))
            self.assertEqual(read_model_binary(snapshot / "0", modern=True), original)
            self.assertEqual(read_model_binary(model_directory, modern=True), edited)
            self.assertFalse((snapshot / "backup_manifest.json").exists())

    def test_direct_sparse_model_places_backup_files_at_snapshot_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_directory, images_directory = make_legacy_text(root)
            sparse_root = model_directory.parent
            for path in tuple(model_directory.iterdir()):
                path.rename(sparse_root / path.name)
            model_directory.rmdir()
            loaded = load_reconstruction(root, sparse_root, images_directory, "TEXT")
            edited = _point_removed(loaded.reconstruction)

            result = execute_export(
                validate_export_request(loaded.source, edited, backup_enabled=True),
                edited,
            )

            snapshot = result.backup_snapshot
            assert snapshot is not None
            self.assertTrue((snapshot / "cameras.txt").is_file())
            self.assertTrue((snapshot / "images.txt").is_file())
            self.assertTrue((snapshot / "points3D.txt").is_file())
            self.assertFalse((snapshot / "sparse").exists())

    def test_modern_text_transaction_preserves_five_file_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_directory, images_directory = make_modern_text(root)
            loaded = load_reconstruction(
                root,
                model_directory,
                images_directory,
                "TEXT",
            )
            edited = _point_removed(loaded.reconstruction)

            result = execute_export(
                validate_export_request(loaded.source, edited, backup_enabled=False),
                edited,
            )

            self.assertEqual(read_model_text(model_directory, modern=True), edited)
            self.assertEqual(
                tuple(path.name for path in result.replaced_files),
                ("rigs.txt", "cameras.txt", "frames.txt", "images.txt", "points3D.txt"),
            )

    def test_backup_name_collision_creates_new_snapshot_without_mutating_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            plan = validate_export_request(
                loaded.source,
                loaded.reconstruction,
                backup_enabled=True,
            )

            with patch(
                "tools.modifier.core.export_backup._snapshot_base_name",
                return_value="fixed_snapshot",
            ):
                first = create_backup_snapshot(plan)
                first_bytes = {
                    path.relative_to(first.snapshot_path): path.read_bytes()
                    for path in first.snapshot_path.rglob("*")
                    if path.is_file()
                }
                second = create_backup_snapshot(plan)

            self.assertEqual(first.snapshot_path.name, "fixed_snapshot")
            self.assertEqual(second.snapshot_path.name, "fixed_snapshot_01")
            self.assertEqual(
                first_bytes,
                {
                    path.relative_to(first.snapshot_path): path.read_bytes()
                    for path in first.snapshot_path.rglob("*")
                    if path.is_file()
                },
            )

    def test_success_cleanup_failure_returns_warning_without_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_binary(root)
            edited = _point_removed(loaded.reconstruction)
            plan = validate_export_request(loaded.source, edited, backup_enabled=False)

            with patch(
                "tools.modifier.core.export_transaction._cleanup_transaction_directory",
                side_effect=OSError("injected cleanup failure"),
            ):
                result = execute_export(plan, edited)

            self.assertEqual(result.loaded.reconstruction, edited)
            self.assertEqual(read_model_binary(loaded.source.model_directory, False), edited)
            self.assertEqual(len(result.cleanup_warnings), 1)


class ExportTransactionFailureTests(unittest.TestCase):
    def test_format_conversion_failure_restores_source_and_old_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            original = loaded.reconstruction
            model_directory = loaded.source.model_directory
            stale_binary = b"preexisting opposite format"
            (model_directory / "cameras.bin").write_bytes(stale_binary)
            edited = _point_removed(original)
            plan = validate_export_request(
                loaded.source,
                edited,
                backup_enabled=False,
                output_format="BINARY",
            )

            with patch(
                "tools.modifier.core.export_transaction._remove_obsolete_file",
                side_effect=OSError("injected conversion cleanup failure"),
            ):
                with self.assertRaises(ExportTransactionError) as caught:
                    execute_export(plan, edited)

            self.assertTrue(caught.exception.rollback_attempted)
            self.assertTrue(caught.exception.rollback_succeeded)
            self.assertEqual(
                read_model_text(model_directory, modern=False),
                original,
            )
            self.assertEqual(
                (model_directory / "cameras.bin").read_bytes(),
                stale_binary,
            )
            self.assertFalse((model_directory / "images.bin").exists())
            self.assertFalse((model_directory / "points3D.bin").exists())

    def test_source_change_after_validation_is_overwritten_without_hash_precheck(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            edited = _point_removed(loaded.reconstruction)
            plan = validate_export_request(loaded.source, edited, backup_enabled=False)
            cameras = loaded.source.model_directory / "cameras.txt"
            cameras.write_text(
                cameras.read_text(encoding="utf-8") + "# external change\n",
                encoding="utf-8",
            )

            result = execute_export(plan, edited)

            self.assertEqual(result.loaded.reconstruction, edited)
            self.assertEqual(
                read_model_text(loaded.source.model_directory, modern=False),
                edited,
            )
            self.assertFalse((root / "sparse_backup").exists())

    def test_every_commit_failure_point_restores_original_fingerprint(self):
        for failure_index in range(3):
            with (
                self.subTest(failure_index=failure_index),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                loaded = _loaded_text(root)
                edited = _point_removed(loaded.reconstruction)
                plan = validate_export_request(loaded.source, edited, backup_enabled=False)
                call_index = 0

                def fail_selected_replace(source: Path, destination: Path):
                    nonlocal call_index
                    current = call_index
                    call_index += 1
                    if current == failure_index:
                        raise OSError(f"injected commit failure {failure_index}")
                    os.replace(source, destination)

                with (
                    patch(
                        "tools.modifier.core.export_transaction._replace_staged_file",
                        side_effect=fail_selected_replace,
                    ),
                    self.assertRaises(ExportTransactionError) as caught,
                ):
                    execute_export(plan, edited)

                error = caught.exception
                self.assertEqual(error.stage, "commit")
                self.assertEqual(
                    read_model_text(loaded.source.model_directory, modern=False),
                    loaded.reconstruction,
                )
                self.assertEqual(_fingerprint_from_source(loaded.source), plan.expected_source_fingerprint)
                self.assertFalse(error.source_changed)
                self.assertEqual(len(error.replaced_files), failure_index)
                self.assertEqual(error.rollback_attempted, failure_index > 0)
                self.assertFalse((root / "sparse_backup").exists())

    def test_staging_parser_failure_leaves_source_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_binary(root)
            edited = _point_removed(loaded.reconstruction)
            plan = validate_export_request(loaded.source, edited, backup_enabled=False)

            with (
                patch(
                    "tools.modifier.core.export_transaction._read_model",
                    side_effect=ValueError("injected staging parser failure"),
                ),
                self.assertRaises(ExportTransactionError) as caught,
            ):
                execute_export(plan, edited)

            self.assertEqual(caught.exception.stage, "staging")
            self.assertFalse(caught.exception.rollback_attempted)
            self.assertFalse(caught.exception.replaced_files)
            self.assertEqual(
                read_model_binary(loaded.source.model_directory, modern=False),
                loaded.reconstruction,
            )
            self.assertEqual(_fingerprint_from_source(loaded.source), plan.expected_source_fingerprint)

    def test_rollback_failure_preserves_recovery_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            edited = _point_removed(loaded.reconstruction)
            plan = validate_export_request(loaded.source, edited, backup_enabled=False)
            call_index = 0

            def fail_second_commit(source: Path, destination: Path):
                nonlocal call_index
                current = call_index
                call_index += 1
                if current == 1:
                    raise OSError("injected second commit failure")
                os.replace(source, destination)

            with (
                patch(
                    "tools.modifier.core.export_transaction._replace_staged_file",
                    side_effect=fail_second_commit,
                ),
                patch(
                    "tools.modifier.core.export_transaction._restore_original_file",
                    side_effect=OSError("injected rollback failure"),
                ),
                self.assertRaises(ExportTransactionError) as caught,
            ):
                execute_export(plan, edited)

            error = caught.exception
            self.assertTrue(error.source_changed)
            self.assertTrue(error.rollback_attempted)
            self.assertFalse(error.rollback_succeeded)
            self.assertIsNotNone(error.recovery_directory)
            recovery = error.recovery_directory
            assert recovery is not None
            self.assertTrue((recovery / "rollback" / "cameras.txt").is_file())
            self.assertTrue((recovery / "rollback" / "images.txt").is_file())
            self.assertTrue((recovery / "rollback" / "points3D.txt").is_file())

    def test_backup_copy_failure_leaves_source_unchanged_and_no_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_binary(root)
            edited = _point_removed(loaded.reconstruction)
            plan = validate_export_request(loaded.source, edited, backup_enabled=True)

            with (
                patch(
                    "tools.modifier.core.export_backup._copy_backup_file",
                    side_effect=OSError("injected backup copy failure"),
                ),
                self.assertRaises(ExportTransactionError) as caught,
            ):
                execute_export(plan, edited)

            self.assertEqual(caught.exception.stage, "backup")
            self.assertFalse(caught.exception.source_changed)
            self.assertEqual(
                read_model_binary(loaded.source.model_directory, modern=False),
                loaded.reconstruction,
            )
            self.assertFalse((root / "sparse_backup").exists())

    def test_staging_writer_failure_leaves_source_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            edited = _point_removed(loaded.reconstruction)
            plan = validate_export_request(loaded.source, edited, backup_enabled=False)

            with (
                patch(
                    "tools.modifier.core.export_transaction._write_staging",
                    side_effect=OSError("injected writer failure"),
                ),
                self.assertRaises(ExportTransactionError) as caught,
            ):
                execute_export(plan, edited)

            self.assertEqual(caught.exception.stage, "staging")
            self.assertFalse(caught.exception.replaced_files)
            self.assertEqual(
                read_model_text(loaded.source.model_directory, modern=False),
                loaded.reconstruction,
            )
            self.assertEqual(_fingerprint_from_source(loaded.source), plan.expected_source_fingerprint)


class ExportFastPathStructureTests(unittest.TestCase):
    def test_success_path_uses_one_parser_and_one_integrity_validation(self):
        for backup_enabled in (False, True):
            with (
                self.subTest(backup_enabled=backup_enabled),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                loaded = _loaded_text(root)
                edited = _point_removed(loaded.reconstruction)
                plan = validate_export_request(
                    loaded.source,
                    edited,
                    backup_enabled=backup_enabled,
                )

                with (
                    patch.object(
                        export_transaction,
                        "_read_model",
                        wraps=export_transaction._read_model,
                    ) as parser,
                    patch.object(
                        export_transaction,
                        "validate_reconstruction",
                        wraps=export_transaction.validate_reconstruction,
                    ) as integrity,
                ):
                    result = execute_export(plan, edited)

                self.assertIs(result.loaded.reconstruction, edited)
                self.assertEqual(parser.call_count, 1)
                self.assertEqual(integrity.call_count, 1)

    def test_original_files_are_copied_without_hash_or_parser_validation(self):
        for backup_enabled in (False, True):
            with (
                self.subTest(backup_enabled=backup_enabled),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                loaded = _loaded_text(root)
                edited = _point_removed(loaded.reconstruction)
                plan = validate_export_request(
                    loaded.source,
                    edited,
                    backup_enabled=backup_enabled,
                )

                with (
                    patch.object(
                        export_validation,
                        "sha256_file",
                        wraps=export_validation.sha256_file,
                    ) as manifest_hash,
                    patch.object(
                        export_transaction,
                        "_read_model",
                        wraps=export_transaction._read_model,
                    ) as parser,
                    patch.object(
                        export_transaction,
                        "_copy_rollback_file",
                        wraps=export_transaction._copy_rollback_file,
                    ) as rollback_copy,
                    patch.object(
                        export_backup,
                        "_copy_backup_file",
                        wraps=export_backup._copy_backup_file,
                    ) as backup_copy,
                ):
                    execute_export(plan, edited)

                source_hashes = [
                    call
                    for call in manifest_hash.call_args_list
                    if Path(call.args[0]).parent == loaded.source.model_directory
                ]
                staging_hashes = [
                    call
                    for call in manifest_hash.call_args_list
                    if Path(call.args[0]).parent.name == "staging"
                ]
                self.assertEqual(len(source_hashes), 0)
                self.assertEqual(len(staging_hashes), 3)
                self.assertEqual(parser.call_count, 1)
                if backup_enabled:
                    self.assertEqual(backup_copy.call_count, 3)
                    self.assertEqual(rollback_copy.call_count, 0)
                else:
                    self.assertEqual(rollback_copy.call_count, 3)
                    self.assertEqual(backup_copy.call_count, 0)

    def test_runtime_token_mismatch_fails_before_original_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            edited = _point_removed(loaded.reconstruction)
            plan = validate_export_request(
                loaded.source,
                edited,
                backup_enabled=False,
            )
            stale_plan = replace(
                plan,
                runtime_token=replace(
                    plan.runtime_token,
                    edit_revision=1,
                    index_revision=0,
                ),
            )

            with (
                patch.object(
                    export_transaction,
                    "_copy_rollback_file",
                    wraps=export_transaction._copy_rollback_file,
                ) as rollback_copy,
                self.assertRaises(ExportTransactionError) as caught,
            ):
                execute_export(stale_plan, edited)

            self.assertEqual(caught.exception.stage, "validation")
            self.assertEqual(rollback_copy.call_count, 0)

    def test_backup_snapshot_is_the_only_original_copy_and_rollback_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            edited = _point_removed(loaded.reconstruction)
            plan = validate_export_request(
                loaded.source,
                edited,
                backup_enabled=True,
            )
            call_index = 0

            def fail_second_commit(source: Path, destination: Path):
                nonlocal call_index
                current = call_index
                call_index += 1
                if current == 1:
                    raise OSError("injected second commit failure")
                os.replace(source, destination)

            with (
                patch.object(
                    export_transaction,
                    "_replace_staged_file",
                    side_effect=fail_second_commit,
                ),
                patch.object(
                    export_transaction,
                    "_copy_rollback_file",
                    wraps=export_transaction._copy_rollback_file,
                ) as rollback_copy,
                patch.object(
                    export_backup,
                    "_copy_backup_file",
                    wraps=export_backup._copy_backup_file,
                ) as backup_copy,
                patch.object(
                    export_transaction,
                    "_read_model",
                    wraps=export_transaction._read_model,
                ) as parser,
                self.assertRaises(ExportTransactionError) as caught,
            ):
                execute_export(plan, edited)

            self.assertEqual(rollback_copy.call_count, 0)
            self.assertEqual(backup_copy.call_count, 3)
            self.assertEqual(parser.call_count, 1)
            self.assertTrue(caught.exception.rollback_succeeded)
            self.assertIsNotNone(caught.exception.backup_snapshot)
            self.assertEqual(
                read_model_text(loaded.source.model_directory, modern=False),
                loaded.reconstruction,
            )


class ExportPathSafetyTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "symlink"), "symlink를 지원하는 환경에서만 실행합니다.")
    def test_source_file_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            source = loaded.source.model_directory / "cameras.txt"
            outside = root / "outside.txt"
            outside.write_bytes(source.read_bytes())
            source.unlink()
            try:
                os.symlink(outside, source)
            except OSError:
                self.skipTest("현재 계정에서 file symlink 생성 권한이 없습니다.")

            with self.assertRaisesRegex(ValueError, "link|reparse"):
                validate_export_request(
                    loaded.source,
                    loaded.reconstruction,
                    backup_enabled=False,
                )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink를 지원하는 환경에서만 실행합니다.")
    def test_backup_root_directory_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            outside = root / "outside_backup"
            outside.mkdir()
            backup_link = root / "sparse_backup"
            try:
                os.symlink(outside, backup_link, target_is_directory=True)
            except OSError:
                self.skipTest("현재 계정에서 directory symlink 생성 권한이 없습니다.")

            with self.assertRaisesRegex(ValueError, "link|reparse"):
                validate_export_request(
                    loaded.source,
                    loaded.reconstruction,
                    backup_enabled=True,
                )


if __name__ == "__main__":
    unittest.main()
