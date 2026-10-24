
"""Offline fresh-installation contracts. All Docker commands are recorded fakes."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install
from ops.self_hosted_credentials import create_credentials, runtime_settings

PROJECT = "leti-cook-0123456789ab"
PORT = 8010
PASSWORD = "a" * 64


# Settings of the retired HTTP database gateway; a fresh installation writes none of them.
RETIRED_GATEWAY_ENV = frozenset({"SUPABASE_URL", "SUPABASE_KEY", "JWT_SECRET", "SELF_HOSTED_DB_PORT"})


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read_env(path):
    """The literal KEY=value lines the credential generator writes."""
    raw = path.read_bytes()
    values = {}
    for line in raw.decode("utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, separator, value = line.partition("=")
            assert separator and key not in values
            values[key] = value
    return raw, values


def composed(directory=PurePosixPath('/opt/leti-cook'), project=PROJECT):
    quiet = {"logging": {"driver": "none"}}
    return {
        "services": {
            "db": {**quiet, "networks": {"database": None}},
            "backend": {**quiet, "user": "10001:10001",
                        "volumes": [
                            {"type": "volume", "source": "application-state", "target": "/var/lib/leti-cook"},
                            {"type": "bind", "source": str(directory / 'config'), "target": "/etc/leti-cook", "read_only": True},
                            {"type": "bind", "source": str(directory) + '.update/marker', "target": "/run/leti-cook-update/marker", "read_only": True, "bind": {"create_host_path": False}},
                            {"type": "bind", "source": str(directory) + '.update/cipher', "target": "/run/leti-cook-update/cipher", "bind": {"create_host_path": False}},
                        ],
                        "networks": {"database": None, "application-access": None},
                        "depends_on": {"db": {"condition": "service_healthy"}},
                        "ports": [{"host_ip": "127.0.0.1", "published": str(PORT), "target": 8002}]},
        },
        "networks": {"database": {"internal": True}, "application-access": {"driver": "bridge"}},
        "volumes": {"application-state": {"name": project + '_application-state'}},
    }


class FreshSettingsTests(unittest.TestCase):
    def fresh_install(self, *, fail_build=False, version=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name) / "new-installation"
        files = {"application/VERSION": version if version is not None else (ROOT / "application/VERSION").read_bytes(),
                 "database/Dockerfile": b"FROM postgres:17-bookworm\n",
                 "schema/manifest.json": json.dumps({"files": {"schema.sql": "a"*64}}).encode()}
        manifest = {"files": {name: digest(data) for name, data in files.items()}}
        self.fresh_calls, self.fresh_directory = [], directory
        def run(args, **kwargs):
            self.fresh_calls.append(list(args))
            if args[-3:] == ["config", "--format", "json"]:
                return json.dumps(composed(directory, args[args.index('--project-name') + 1]))
            if args[:1] == ["timedatectl"]:
                return "yes\n"
            if "build" in args:
                self.assertEqual((directory/"database/Dockerfile").read_bytes(), files["database/Dockerfile"])
                if fail_build:
                    raise install.InstallError("command_failed_1")
            return ""
        owner_state = dict(configured=False, authenticated=False, username=None, access_mode="pending")
        with patch.object(install.sys, "platform", "linux"), \
                patch.object(install, "verify_bundle", return_value=(manifest, files)), \
                patch.object(install, "check_docker", return_value={}), \
                patch.object(install, "new_directory", return_value=directory), \
                patch.object(install, "port_available", return_value=True), \
                patch.object(install, "installation_settings", return_value=({}, {'LIC_DATABASE_ID': '00000000-0000-4000-8000-000000000001'})), \
                patch.object(install, "check_update_copy_storage"), \
                patch.object(install, "initialize_update_copy_storage"), \
                patch.object(install.urllib.request, "build_opener") as opener, \
                redirect_stdout(StringIO()):
            opener.return_value.open.return_value = StringIO(json.dumps(owner_state))
            return install.install(Path("/verified-release"), directory, PORT, run=run)

    def test_fresh_install_builds_both_images_before_starting_database(self):
        result = self.fresh_install()
        self.assertRegex(result["project"], r"^leti-cook-[0-9a-f]{12}$")
        builds = [args for args in self.fresh_calls if "build" in args]
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0][-3:], ["build", "db", "backend"])
        self.assertEqual(builds[0][builds[0].index("--project-name")+1], result["project"])
        starts = [i for i, args in enumerate(self.fresh_calls) if "up" in args]
        self.assertTrue(starts)
        self.assertLess(self.fresh_calls.index(builds[0]), starts[0])
        if os.name != "nt":
            self.assertEqual((self.fresh_directory/"database/Dockerfile").stat().st_mode & 0o777, 0o644)
            self.assertEqual(self.fresh_directory.stat().st_mode & 0o777, 0o700)

    def test_receipts_use_captured_bundle_version_instead_of_running_installer_version(self):
        result = self.fresh_install(version=b"7.8.9-rc.2+fixture\n")
        self.assertEqual(result["version"], "7.8.9-rc.2+fixture")
        self.assertEqual(result["source"], "source")
        for name in ("installation.json", "installed.json"):
            receipt = json.loads((self.fresh_directory / name).read_bytes())
            self.assertEqual(receipt["version"], result["version"])
            self.assertEqual(receipt["source"], "source")
        self.assertEqual((self.fresh_directory / "application/VERSION").read_bytes(), b"7.8.9-rc.2+fixture\n")

    def test_fresh_database_image_build_failure_does_not_start_services(self):
        with self.assertRaisesRegex(install.InstallError, "command_failed_1"):
            self.fresh_install(fail_build=True)
        self.assertFalse(any("up" in args or "run" in args or "exec" in args for args in self.fresh_calls))

    def test_fresh_install_reuses_nonowner_password_and_has_independent_admission(self):
        with patch.object(install, "safe_path", return_value=PurePosixPath("/opt/lic")):
            installation, backend = install.installation_settings("/opt/lic", PORT, "a"*64, PROJECT)
        parsed = urlsplit(backend["LIC_DATABASE_DSN"])
        self.assertEqual((parsed.username, parsed.hostname, parsed.port, parsed.path),
                         ("authenticator", "db", 5432, "/leti-cook"))
        self.assertEqual(parsed.password, installation["AUTHENTICATOR_PASSWORD"])
        self.assertEqual(str(UUID(backend["LIC_DATABASE_ID"])), backend["LIC_DATABASE_ID"])
        self.assertNotEqual(backend["LIC_HOT_RUNTIME_SECRET"], installation["AUTHENTICATOR_PASSWORD"])
        self.assertFalse(RETIRED_GATEWAY_ENV.intersection(installation))
        self.assertFalse(RETIRED_GATEWAY_ENV.intersection(backend))
        self.assertEqual(backend["LIC_AUTH_DATABASE"], "/var/lib/leti-cook/access/owner.sqlite3")
        self.assertNotIn("YOUTUBE_QUOTA_JOURNAL_NAMESPACE", backend)

    def test_credential_generator_does_not_expose_or_overwrite_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "new"
            result = create_credentials(destination)
            raw, values = read_env(destination / ".env")
            self.assertFalse(RETIRED_GATEWAY_ENV.intersection(values))
            self.assertEqual(values["LIC_DATABASE_TRANSPORT"], "postgres")
            self.assertNotIn(values["LIC_HOT_RUNTIME_SECRET"], json.dumps(result))
            self.assertNotIn(values["LIC_DATABASE_DSN"], json.dumps(result))
            with self.assertRaises(FileExistsError):
                create_credentials(destination)
            self.assertEqual((destination / ".env").read_bytes(), raw)

    def test_composition_requires_private_database_and_only_loopback_backend(self):
        install.validate_composed(composed(), PORT, directory=PurePosixPath('/opt/leti-cook'), project=PROJECT)
        modifications = [
            lambda c: c["services"]["db"].update(ports=[{"published": "5432"}]),
            lambda c: c["networks"]["database"].update(internal=False),
            lambda c: c["services"]["backend"].update(networks={"application-access": None}),
            lambda c: c["services"]["backend"]["ports"][0].update(host_ip="0.0.0.0"),
            lambda c: c["services"].update(gateway={}),
            lambda c: c["services"]["backend"].update(network_mode="host"),
            lambda c: c["services"]["backend"]["depends_on"]["db"].update(condition="service_started"),
            lambda c: c['services']['backend']['volumes'][2].update(read_only=False),
            lambda c: c['services']['backend']['volumes'][3].update(source='/opt/leti-cook.update-keys'),
            lambda c: c['services']['backend']['volumes'][3].update(bind={'create_host_path': True}),
            lambda c: c['services']['backend']['volumes'].append({'type': 'bind', 'source': '/', 'target': '/host'}),
            lambda c: c['volumes']['application-state'].update(name='foreign_state'),
        ]
        for mutate in modifications:
            config = deepcopy(composed())
            mutate(config)
            with self.assertRaises(install.InstallError):
                install.validate_composed(config, PORT, directory=PurePosixPath('/opt/leti-cook'), project=PROJECT)

    def test_packaged_topology_has_no_http_gateway_and_includes_direct_runtime(self):
        database = (ROOT / "database/compose.yaml").read_text()
        backend = (ROOT / "application.compose.yaml").read_text()
        self.assertNotIn("\n  rest:", database)
        self.assertNotIn("\n  gateway:", database + backend)
        self.assertNotIn("\n    ports:", database)
        self.assertIn("build:\n      context: .\n      dockerfile: Dockerfile", database)
        self.assertNotIn("\n    image:", database)
        self.assertIn("networks: [database, application-access]", backend)
        manifest = json.loads((ROOT / "packaging/files.json").read_text())
        self.assertNotIn("database/gateway.conf", manifest)
        self.assertEqual(manifest["database/Dockerfile"], "database/Dockerfile")
        self.assertNotIn("application/db/function_contracts.py", manifest)
        self.assertFalse(any("legacy_pg_transport" in name or "legacy_function_contracts" in name or "schema24_oracle" in name for name in manifest))
        for name in ("postgres.py", "operation_contracts.py", "sql_values.py"):
            self.assertIn("application/db/base/" + name, manifest)


if __name__ == "__main__":
    unittest.main()
