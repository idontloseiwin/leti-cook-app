"""Offline candidate acceptance: real receipt shapes, captured bytes and refusal before stop."""
from __future__ import annotations

from dataclasses import replace
import configparser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import install
from ops.bundle_contract import FORMAT, REQUIRED_BUNDLE_FILES
from ops.release_verification import VerifiedRelease
from ops.schema_manifest import validate_schema_bundle
from ops.update_candidate import CandidateError, preflight_candidate, prepare_candidate, channel_add_owner_present


def encoded(value):
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def repack(files):
    files["bundle.json"] = encoded({"format": FORMAT, "version": 1, "fresh_install_only": True,
        "stage": "self-hosted-release-candidate", "release_accepted": False,
        "youtube_configuration_included": False,
        "files": {name: digest(data) for name, data in files.items() if name != "bundle.json"}})
    return files


def release(files, version="0.1.1", minimum="0.1.0"):
    files = dict(files)
    files["application/VERSION"] = (version + "\n").encode()
    repack(files)
    return VerifiedRelease({"format": "leti-cook-release", "format_version": 1,
        "version": version, "schema_version": validate_schema_bundle(files).schema_version,
        "minimum_upgrade_version": minimum,
        "package": {"name": f"leti-cook-{version}.tar.gz", "sha256": "0" * 64, "size": 1},
        "images": {name: {"repository": f"ghcr.io/example-owner/example-app-{name}",
            "digest": "sha256:" + "1" * 64, "platform": "linux/amd64"} for name in ("backend", "db")}}, files)


def step(files, version, sql):
    files = dict(files)
    manifest = json.loads(files["schema/manifest.json"])
    name = f"steps/{version:04d}-synthetic-test.sql"
    files["schema/" + name] = sql
    manifest["steps"].append({"version": version, "file": name, "sha256": digest(sql)})
    manifest["files"][name] = digest(sql)
    manifest["schema_version"] = version
    files["schema/schema.sql"] += b"\n-- synthetic fresh schema " + str(version).encode() + b"\n"
    manifest["files"]["schema.sql"] = digest(files["schema/schema.sql"])
    files["schema/manifest.json"] = encoded(manifest)
    runtime = json.loads(files["application/schema-release.json"])
    runtime["schema_version"] = version
    files["application/schema-release.json"] = encoded(runtime)
    return repack(files)


class CandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        names = REQUIRED_BUNDLE_FILES | {"application/update-compatibility.json", "schema/configuration-seeds.sql"}
        # Include all published schema steps in the actual candidate fixture.
        schema_manifest = json.loads((ROOT / "schema/manifest.json").read_bytes())
        names |= {"schema/" + name for name in schema_manifest["files"]}
        cls.source_schema_version = schema_manifest["schema_version"]
        cls.source = {name: (ROOT / name).read_bytes() for name in names}
        cls.source["application/VERSION"] = b"0.1.0\n"
        cls.source["application/release-source.json"] = encoded({"format": "leti-cook-release-source",
            "format_version": 1, "owner": "example-owner", "repository": "example-app"})
        repack(cls.source)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.base.chmod(0o700)
        self.installed = self.base / "installed"
        self.project = "leti-cook-0123456789ab"
        self.write_installation(self.source)

    def write_file(self, name, data):
        target = self.installed / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o600 if name in {"bundle.json", "installation.env", "backend.env", "installation.json", "installed.json", "runtime.compose.json"} else 0o644)

    def write_installation(self, files):
        self.installed.mkdir(mode=0o700, exist_ok=True)
        for name, data in files.items():
            self.write_file(name, data)
        schema = validate_schema_bundle(files)
        # Use the installer itself to produce all fields, secrets and defaults;
        # only substitute a temporary host path on Windows after generation.
        with patch.object(install, "safe_path", return_value=PurePosixPath("/opt/leti-cook-test")):
            host, backend = install.installation_settings(Path("/opt/leti-cook-test"), 8123, schema.schema_sha256, self.project)
        for key in ("LIC_UPDATE_MARKER_HOST_DIRECTORY", "LIC_UPDATE_CIPHER_HOST_DIRECTORY", "SELF_HOSTED_SCHEMA_PATH"):
            host[key] = host[key].replace("/opt/leti-cook-test", self.installed.as_posix())
        self.write_file("installation.env", "".join(key + "=" + value + "\n" for key, value in host.items()).encode())
        self.write_file("backend.env", "".join(key + "=" + value + "\n" for key, value in backend.items()).encode())
        self.identity = backend["LIC_DATABASE_ID"]
        version = files["application/VERSION"].decode().strip()
        self.write_file("installation.json", encoded({"format": FORMAT, "project": self.project, "port": 8123,
            "status": "preparing", "database_id": self.identity, "version": version, "source": "source",
            "bundle_sha256": digest(files["bundle.json"])}))
        self.write_file("installed.json", encoded({"status": "owner_setup_ready", "project": self.project, "port": 8123,
            "database_id": self.identity, "version": version, "source": "source", "collection_started": False}))

    def preflight(self, value=None, **kwargs):
        return preflight_candidate(self.installed, value or release(self.source), **kwargs)

    def test_admission_owner_capability_requires_verified_manifest_and_exact_file(self):
        name='application/db/user_work/reconnect_admission.py'
        self.assertFalse(channel_add_owner_present(self.installed))
        files=dict(self.source);files[name]=b'# synthetic verified owner module\n';repack(files)
        self.write_file(name,files[name]);self.write_file('bundle.json',files['bundle.json'])
        self.assertTrue(channel_add_owner_present(self.installed))
        self.write_file(name,b'# modified owner module\n')
        with self.assertRaisesRegex(CandidateError,'shipped_file_modified'):
            channel_add_owner_present(self.installed)
        (self.installed/name).unlink()
        with self.assertRaisesRegex(CandidateError,'unknown_(file|directory)'):
            channel_add_owner_present(self.installed)

    def test_new_version_cannot_remove_an_existing_persistent_admission_owner(self):
        files=dict(self.source)
        files['application/db/user_work/reconnect_admission.py']=b'# synthetic persistent owner\n'
        repack(files);self.write_installation(files)
        with self.assertRaisesRegex(CandidateError,'update_channel_add_owner_removed'):
            self.preflight(release(self.source))

    def test_real_installer_receipts_and_snapshot_identity(self):
        plan = self.preflight()
        self.assertEqual((plan.project, plan.database_id, plan.port, plan.current_version, plan.target_version),
                         (self.project, self.identity, 8123, "0.1.0", "0.1.1"))
        self.assertEqual(plan.schema, plan.installed_schema)
        self.assertEqual(plan.files["backend.env"], (self.installed / "backend.env").read_bytes())
        self.assertEqual(plan.files["installation.env"], (self.installed / "installation.env").read_bytes())
        self.assertEqual(plan.installed_state["database_id"], self.identity)
        self.assertNotIn("LIC_HOT_RUNTIME_SECRET", repr(plan))
        with self.assertRaises(TypeError):
            plan.files["other"] = b"x"
        with self.assertRaises(TypeError):
            plan.installed_state["database_id"] = "other"

    def test_user_configuration_and_flags_preserved_byte_for_byte(self):
        config = self.source["config/config.ini"].replace(b"automatic_collection = true", b"automatic_collection = false")
        config = (b"# Custom owner comment\n" + config).replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        schedules = b"# Custom schedule comment\n" + self.source["config/collection-schedules.ini"].replace(b"identity_seconds = 2419200", b"identity_seconds = 21600")
        backend = (self.installed / "backend.env").read_bytes().replace(b"CHANNEL_BASELINE_ANALYTICS_ENABLED=1", b"CHANNEL_BASELINE_ANALYTICS_ENABLED=0")
        for name, data in (("config/config.ini", config), ("config/collection-schedules.ini", schedules), ("backend.env", backend)):
            self.write_file(name, data)
        plan = self.preflight()
        self.assertEqual(plan.files["config/config.ini"], config)
        self.assertEqual(plan.files["config/collection-schedules.ini"], schedules)
        self.assertEqual(plan.files["backend.env"], backend)

    def test_new_optional_template_keys_preserve_user_config_through_preparation(self):
        name = "config/config.ini"
        original = self.source[name]
        user_config = b"# Owner setting, keep exactly\r\n" + original.replace(
            b"automatic_collection = true", b"automatic_collection = false")
        self.write_file(name, user_config)
        for section in ("collection", "limits"):
            with self.subTest(section=section):
                files = dict(self.source)
                marker = ("[" + section + "]").encode()
                # Unknown text is data, not a path to open or an expression to run.
                files[name] = original.replace(marker, marker + b"\nfuture_optional = ${HOME}/not-opened")
                plan = self.preflight(release(files))
                self.assertEqual(plan.files[name], user_config)
                self.assertEqual((self.installed / name).read_bytes(), user_config)
                prepared = prepare_candidate(plan, self.base / ("candidate-" + section))
                self.assertEqual((prepared.path / name).read_bytes(), user_config)
                self.assertNotIn(b"future_optional", (prepared.path / name).read_bytes())
                self.assertEqual(json.loads((prepared.path / "installed.json").read_bytes())["version"], "0.1.1")

    def test_template_extensions_do_not_bypass_sections_format_or_known_values(self):
        name = "config/config.ini"
        raw = self.source[name]
        changes = (
            (raw + b"\n[extra]\nkey=1\n", "update_unknown_config_key"),
            (raw.replace(b"[format]", b"[format]\nfuture_optional=1"), "update_unknown_config_key"),
            (raw.replace(b"version = 3", b"version = 4"), "update_config_invalid"),
            (raw.replace(b"automatic_collection = true", b"automatic_collection = maybe"), "update_config_invalid"),
            (raw.replace(b"[collection]", b"[collection]\nfuture=1\nFUTURE=2"), "update_config_invalid"),
            (raw + b"\nnot an option\n", "update_config_invalid"),
            (b"[DEFAULT]\nfuture=1\n" + raw, "update_config_invalid"),
        )
        before = {p.relative_to(self.installed): p.read_bytes() for p in self.installed.rglob("*") if p.is_file()}
        for changed, code in changes:
            with self.subTest(code=code, index=changes.index((changed, code))):
                files = dict(self.source)
                files[name] = changed
                with self.assertRaisesRegex(CandidateError, "^" + code + "$"):
                    self.preflight(release(files))
        self.assertEqual({p.relative_to(self.installed): p.read_bytes() for p in self.installed.rglob("*") if p.is_file()}, before)

    def test_new_tree_receipts_private_files_and_future_preflight(self):
        plan = self.preflight()
        prepared = prepare_candidate(plan, self.base / "candidate")
        self.assertEqual(prepared.path, self.base / "candidate")
        self.assertEqual((prepared.path / "backend.env").read_bytes(), plan.files["backend.env"])
        for name in ("installation.json", "installed.json"):
            value = json.loads((prepared.path / name).read_bytes())
            self.assertEqual((value["database_id"], value["version"], value["source"]), (self.identity, "0.1.1", "release"))
        self.assertEqual(json.loads((prepared.path / "installation.json").read_bytes())["bundle_sha256"], digest(plan.files["bundle.json"]))
        if os.name != "nt":
            self.assertEqual((prepared.path / "backend.env").stat().st_mode & 0o777, 0o600)
            self.assertEqual((prepared.path / "config/config.ini").stat().st_mode & 0o777, 0o644)
        # Simulate host exchange while keeping canonical paths in the env.
        original = self.base / "old"
        self.installed.rename(original)
        prepared.path.rename(self.installed)
        next_plan = self.preflight(release(self.source, "0.1.2"))
        self.assertEqual(next_plan.current_version, "0.1.1")

    @unittest.skipIf(os.name == "nt", "POSIX filesystem modes and umask are required")
    def test_restrictive_umask_keeps_public_tree_readable_and_secrets_private(self):
        plan = self.preflight()
        previous_umask = os.umask(0o077)
        try:
            prepared = prepare_candidate(plan, self.base / "candidate")
            self.assertEqual(os.umask(0o077), 0o077, "preparation must not change the process umask")
        finally:
            os.umask(previous_umask)
        self.assertEqual(prepared.path.stat().st_mode & 0o777, 0o700)
        private = {"bundle.json", "installation.env", "backend.env", "installation.json", "installed.json", "runtime.compose.json"}
        for item in prepared.path.rglob("*"):
            with self.subTest(path=item.relative_to(prepared.path).as_posix()):
                self.assertFalse(item.is_symlink())
                self.assertEqual(item.stat().st_uid, 0)
                expected = 0o755 if item.is_dir() else 0o600 if item.relative_to(prepared.path).as_posix() in private else 0o644
                self.assertEqual(item.stat().st_mode & 0o777, expected)
        for name, data in plan.files.items():
            self.assertEqual((prepared.path / name).read_bytes(), data)

    def test_same_older_build_only_and_below_floor_versions_rejected(self):
        for version, minimum, code in (("0.1.0", "0.1.0", "update_version_not_newer"),
                ("0.0.9", "0.0.1", "update_version_not_newer"),
                ("0.1.0+new-build", "0.1.0", "update_version_not_newer"),
                ("0.2.0", "0.1.1", "update_version_below_minimum")):
            with self.subTest(version=version), self.assertRaisesRegex(CandidateError, code):
                self.preflight(release(self.source, version, minimum))

    def test_prerelease_requires_explicit_true(self):
        value = release(self.source, "0.2.0-rc.1")
        with self.assertRaisesRegex(CandidateError, "update_prerelease_not_allowed"):
            self.preflight(value)
        self.assertEqual(self.preflight(value, allow_prerelease=True).target_version, "0.2.0-rc.1")
        with self.assertRaisesRegex(CandidateError, "update_verified_release_required"):
            self.preflight(value, allow_prerelease=1)

    def test_modified_code_and_unknown_files_or_empty_directories_fail(self):
        for name in ("application/server.py", "schema/schema.sql"):
            original = (self.installed / name).read_bytes()
            self.write_file(name, original + b"\n# edited\n")
            with self.subTest(name=name), self.assertRaisesRegex(CandidateError, "update_shipped_file_modified"):
                self.preflight()
            self.write_file(name, original)
        self.write_file("temporary-backup.txt", b"unknown")
        with self.assertRaisesRegex(CandidateError, "update_unknown_file"):
            self.preflight()
        (self.installed / "temporary-backup.txt").unlink()
        (self.installed / "unknown-empty").mkdir()
        with self.assertRaisesRegex(CandidateError, "update_unknown_directory"):
            self.preflight()

    def test_new_defaults_preserve_existing_owner_video_and_page_limits(self):
        name = "config/config.ini"
        template = self.source[name]
        self.assertIn(b"stats_limit = 20000", template)
        self.assertIn(b"inventory_page_budget = 20000", template)
        for pages, videos in ((0, 6000), (6000, 20000), (17321, 17321)):
            with self.subTest(inventory_page_budget=pages, stats_limit=videos):
                owner = template.replace(b"stats_limit = 20000", f"stats_limit = {videos}".encode())
                owner = owner.replace(b"inventory_page_budget = 20000", f"inventory_page_budget = {pages}".encode())
                self.write_file(name, owner)
                plan = self.preflight()
                self.assertEqual(plan.files[name], owner)
                self.assertEqual((self.installed / name).read_bytes(), owner)

    def test_unknown_and_invalid_configuration_refused_not_discarded(self):
        name = "config/config.ini"
        raw = (self.installed / name).read_bytes()
        for change in (raw + b"\n[extra]\nkey=1\n", raw.replace(b"[collection]", b"[collection]\nunknown=1"),
                raw.replace(b"stats_limit = 20000", b"stats_limit = 0"), raw.replace(b"version = 3", b"version = 2")):
            self.write_file(name, change)
            with self.subTest(change=change[-40:]), self.assertRaises(CandidateError):
                self.preflight()
        self.write_file(name, raw)
        name = "config/collection-schedules.ini"
        self.write_file(name, (self.installed / name).read_bytes() + b"\n[unknown]\nvalue=1\n")
        with self.assertRaisesRegex(CandidateError, "update_config_invalid"):
            self.preflight()

    def test_unsupported_state_or_config_formats_never_execute_candidate(self):
        for field, value in (("application_state_version", 2), ("config_format_version", 4),
                ("schedules_format_version", 5), ("schedules_format_version", True), ("format_version", True)):
            files = dict(self.source)
            compatibility = json.loads(files["application/update-compatibility.json"])
            compatibility[field] = value
            files["application/update-compatibility.json"] = encoded(compatibility)
            with self.subTest(field=field), self.assertRaisesRegex(CandidateError, "update_compatibility_unsupported"):
                self.preflight(release(files))
        files = dict(self.source)
        files["application/self_hosted_settings.py"] = b"raise AssertionError('candidate must not execute')\n"
        files["application/collection_schedule.py"] = b"raise AssertionError('candidate must not execute')\n"
        self.assertEqual(self.preflight(release(files)).target_version, "0.1.1")

    def _old_schedule_upgrade(self, version):
        installed = dict(self.source)
        compatibility = json.loads(installed['application/update-compatibility.json'])
        compatibility['schedules_format_version'] = version
        installed['application/update-compatibility.json'] = encoded(compatibility)
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(installed['config/collection-schedules.ini'].decode())
        parser['format']['version'] = str(version)
        for section in tuple(parser.sections()):
            if section.endswith('.inventory') or (version == 2 and (
                    section.endswith('.unavailable') or section in ('schedule.short.counters', 'schedule.stream.counters'))):
                parser.remove_section(section)
        parser['schedule.long.counters']['age_0_1d_seconds'] = '1800'
        if version == 3:
            parser['schedule.short.unavailable']['daily_seconds'] = '43200'
            parser['schedule.stream.counters']['age_0_1d_seconds'] = '1800'
        output = io.StringIO()
        parser.write(output)
        owner_schedule = b'# Owner schedule, preserve comments and every byte\n' + output.getvalue().encode()
        installed['config/collection-schedules.ini'] = owner_schedule
        repack(installed)
        self.write_installation(installed)
        candidate = dict(self.source)
        compatibility['schedules_format_version'] = 4
        candidate['application/update-compatibility.json'] = encoded(compatibility)
        candidate['application/collection_schedule.py'] = b"raise AssertionError('candidate must not execute')\n"
        plan = self.preflight(release(candidate))
        self.assertEqual(plan.files['config/collection-schedules.ini'], owner_schedule)
        self.assertEqual(plan.target_version, '0.1.1')

    def test_schedule_v4_candidate_preserves_installed_v2_settings_without_execution(self):
        self._old_schedule_upgrade(2)

    def test_schedule_v4_candidate_preserves_installed_v3_settings_without_execution(self):
        self._old_schedule_upgrade(3)

    def test_environment_identity_unknown_keys_and_interpolation_refused(self):
        for name, suffix in (("backend.env", b"UNKNOWN=1\n"), ("installation.env", b"UNKNOWN=1\n"),
                ("backend.env", b"LIC_DATABASE_ID=duplicate\n"), ("backend.env", b"TRAP=$(not-executed)\n")):
            raw = (self.installed / name).read_bytes()
            self.write_file(name, raw + suffix)
            with self.subTest(name=name, suffix=suffix), self.assertRaises(CandidateError):
                self.preflight()
            self.write_file(name, raw)
        raw = (self.installed / "installation.env").read_bytes()
        self.write_file("installation.env", raw.replace(b"LIC_HTTP_PORT=8123", b"LIC_HTTP_PORT=8124"))
        with self.assertRaisesRegex(CandidateError, "update_environment_identity_mismatch"):
            self.preflight()

    def test_receipt_hash_and_cross_identity_cannot_drift(self):
        raw = (self.installed / "installation.json").read_bytes()
        for key, value in (("bundle_sha256", "f" * 64), ("port", 9000), ("extra", True)):
            receipt = json.loads(raw)
            receipt[key] = value
            self.write_file("installation.json", encoded(receipt))
            with self.subTest(key=key), self.assertRaises(CandidateError):
                self.preflight()
        self.write_file("installation.json", raw)

    def test_capture_freezes_verified_file_dictionary(self):
        verified = release(self.source)
        plan = self.preflight(verified)
        verified.files["application/server.py"] = b"tampered after capture"
        verified.manifest["version"] = "9.9.9"
        self.assertNotEqual(plan.files["application/server.py"], verified.files["application/server.py"])
        self.assertEqual(plan.target_version, "0.1.1")

    def test_preparation_refuses_changed_installation_and_existing_target(self):
        plan = self.preflight()
        original = (self.installed / "config/config.ini").read_bytes()
        self.write_file("config/config.ini", original + b"\n# changed after preflight\n")
        with self.assertRaisesRegex(CandidateError, "update_installation_changed"):
            prepare_candidate(plan, self.base / "candidate")
        self.assertFalse((self.base / "candidate").exists())
        self.write_file("config/config.ini", original)
        target = self.base / "candidate"
        target.mkdir()
        with self.assertRaisesRegex(CandidateError, "update_new_candidate_required"):
            prepare_candidate(plan, target)

    def test_same_bytes_with_replaced_root_inode_refused(self):
        plan = self.preflight()
        self.installed.rename(self.base / "old")
        self.write_installation(self.source)
        with self.assertRaisesRegex(CandidateError, "update_installation_changed"):
            prepare_candidate(plan, self.base / "candidate")

    def test_candidate_inside_live_installation_refused(self):
        with self.assertRaisesRegex(CandidateError, "update_new_candidate_required"):
            prepare_candidate(self.preflight(), self.installed / "candidate")

    def test_manually_mutated_plan_refused_before_target_creation(self):
        plan = self.preflight()
        files = dict(plan.files)
        files["application/server.py"] = b"tampered"
        with self.assertRaisesRegex(CandidateError, "update_shipped_file_modified"):
            prepare_candidate(replace(plan, files=files), self.base / "candidate")
        self.assertFalse((self.base / "candidate").exists())

    def test_baseline_and_old_step_prefix_immutable_even_when_new_hashes_valid(self):
        changed = dict(self.source)
        changed["schema/baseline/schema.sql"] += b"\n-- changed published baseline\n"
        changed["schema/schema.sql"] = changed["schema/baseline/schema.sql"]
        manifest = json.loads(changed["schema/manifest.json"])
        manifest["files"]["schema.sql"] = manifest["files"]["baseline/schema.sql"] = digest(changed["schema/schema.sql"])
        changed["schema/manifest.json"] = encoded(manifest)
        with self.assertRaisesRegex(CandidateError, "update_schema_history_changed"):
            self.preflight(release(changed))
        next_version = self.source_schema_version + 1
        with_step = step(self.source, next_version, b"CREATE TABLE synthetic_next (id integer);\n")
        self.write_installation(with_step)
        later = step(with_step, next_version + 1, b"CREATE TABLE synthetic_later (id integer);\n")
        self.assertEqual(self.preflight(release(later)).schema.schema_version, next_version + 1)
        name = f"steps/{next_version:04d}-synthetic-test.sql"
        later["schema/" + name] = b"CREATE TABLE changed_history (id integer);\n"
        manifest = json.loads(later["schema/manifest.json"])
        manifest["files"][name] = digest(later["schema/" + name])
        next(entry for entry in manifest["steps"] if entry["file"] == name)["sha256"] = manifest["files"][name]
        later["schema/manifest.json"] = encoded(manifest)
        with self.assertRaisesRegex(CandidateError, "update_schema_history_changed"):
            self.preflight(release(later))

    def test_no_version_schema_edit_and_schema_downgrade_refused(self):
        changed = dict(self.source)
        changed["schema/configuration-seeds.sql"] += b"\n-- changed without a step\n"
        manifest = json.loads(changed["schema/manifest.json"])
        manifest["files"]["configuration-seeds.sql"] = digest(changed["schema/configuration-seeds.sql"])
        changed["schema/manifest.json"] = encoded(manifest)
        with self.assertRaisesRegex(CandidateError, "update_schema_changed_without_step"):
            self.preflight(release(changed))
        with_step = step(self.source, self.source_schema_version + 1, b"CREATE TABLE synthetic_next (id integer);\n")
        self.write_installation(with_step)
        with self.assertRaisesRegex(CandidateError, "update_schema_history_changed"):
            self.preflight(release(self.source))

    def test_new_step_updates_only_schema_hash_line_in_private_environment(self):
        files = step(self.source, self.source_schema_version + 1, b"CREATE TABLE synthetic_next (id integer);\n")
        previous = (self.installed / "installation.env").read_bytes()
        previous = b"# Preserve private environment comments\n" + previous.replace(b"\n", b"\r\n")
        self.write_file("installation.env", previous)
        plan = self.preflight(release(files))
        self.assertEqual(plan.files["installation.env"], previous.replace(plan.installed_schema.schema_sha256.encode(), plan.schema.schema_sha256.encode()))

    def test_runtime_override_accepts_only_immutable_images_and_owned_volume(self):
        for volume in (self.project + "_database-data", self.project + "_restore_" + "a" * 32):
            value = {"services": {name: {"image": "sha256:" + "b" * 64} for name in ("db", "backend")},
                     "volumes": {"database-data": {"name": volume}}}
            raw = encoded(value)
            self.write_file("runtime.compose.json", raw)
            self.assertEqual(self.preflight().files["runtime.compose.json"], raw)
        for mutate in (lambda v: v["services"]["db"].update(image="postgres:17"),
                lambda v: v["services"]["backend"].update(privileged=True),
                lambda v: v["volumes"]["database-data"].update(name="other-project_database-data")):
            value = json.loads(raw)
            mutate(value)
            self.write_file("runtime.compose.json", encoded(value))
            with self.assertRaisesRegex(CandidateError, "update_runtime_override_invalid"):
                self.preflight()

    def test_hardlinked_file_rejected(self):
        target = self.installed / "application/server.py"
        alias = self.base / "alias.py"
        os.link(target, alias)
        with self.assertRaisesRegex(CandidateError, "update_file_invalid"):
            self.preflight()

    @unittest.skipIf(os.name == "nt", "Windows symlink privilege is not a Linux security proof")
    def test_linux_symlink_and_group_writable_file_rejected(self):
        target = self.installed / "application/server.py"
        original = target.read_bytes()
        target.unlink()
        target.symlink_to(self.base / "outside")
        with self.assertRaises(CandidateError):
            self.preflight()
        target.unlink()
        self.write_file("application/server.py", original)
        target.chmod(0o666)
        with self.assertRaisesRegex(CandidateError, "update_file_permissions"):
            self.preflight()


if __name__ == "__main__":
    unittest.main()
