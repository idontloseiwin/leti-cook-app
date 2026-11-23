"""Release versions are strict, ordered SemVer and readable without services."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from dataclasses import FrozenInstanceError
from io import StringIO
from pathlib import Path
import sys
import tempfile
import os
import shutil
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "application"))
sys.path.insert(0, str(ROOT / "tests"))
from application.release_version import parse_version, parse_version_file, read_version
import manage


class ReleaseVersionTests(unittest.TestCase):
    def test_semver_precedence_is_numeric_and_prerelease_aware(self):
        ordered = ["0.1.0-0", "0.1.0-alpha", "0.1.0-alpha.1", "0.1.0-alpha.beta",
                   "0.1.0-beta", "0.1.0-beta.2", "0.1.0-beta.11", "0.1.0-rc.2",
                   "0.1.0-rc.10", "0.1.0", "0.2.0", "0.10.0", "1.0.0", "2.0.0", "10.0.0"]
        versions = list(map(parse_version, ordered))
        self.assertEqual(list(map(str, sorted(reversed(versions)))), ordered)
        self.assertTrue(all(left < right for left, right in zip(versions, versions[1:])))

    def test_build_metadata_is_preserved_but_does_not_change_precedence(self):
        left = parse_version("0.1.0-rc.1+001.build")
        right = parse_version("0.1.0-rc.1+other")
        self.assertEqual(left, right)
        self.assertEqual(hash(left), hash(right))
        self.assertEqual(str(left), "0.1.0-rc.1+001.build")
        self.assertEqual(left.prerelease, ("rc", "1"))
        self.assertEqual(left.build, ("001", "build"))
        self.assertEqual((left.major, left.minor, left.patch), (0, 1, 0))
        with self.assertRaises(FrozenInstanceError):
            left.patch = 4

    def test_malformed_or_ambiguous_versions_are_rejected(self):
        for value in (None, 1, [], "", "v0.1.0", "1.0", "01.0.0", "0.01.0", "0.1.00",
                      "0.1.0-01", "0.1.0-rc.01", "0.1.0-", "0.1.0-rc..1", "0.1.0+", "0.1.0+a..b",
                      "0.1.0_1", "0.1.0+build/value", "0.1.0\n", " 0.1.0", "0.1.0 ",
                      "0.1.0-\u00e9", "0.1.0+" + "x" * 250):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "^invalid_release_version$"):
                parse_version(value)

    def test_version_file_is_exact_ascii_with_one_lf(self):
        self.assertEqual(parse_version_file(b"0.1.0-rc.2+build.001\n"), "0.1.0-rc.2+build.001")
        for data in (b"0.1.0", b"0.1.0\r\n", b"0.1.0\n\n", b"\xef\xbb\xbf0.1.0\n", b"0.1.0 \n",
                     b"0.1.0-\xff\n", b"x" * 257, "0.1.0\n", None):
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, "^invalid_release_version$"):
                parse_version_file(data)

    def test_reader_uses_adjacent_file_and_bounds_trusted_path_input(self):
        self.assertEqual(read_version(), parse_version_file((ROOT / "application/VERSION").read_bytes()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "VERSION"
            path.write_bytes(b"3.2.1-rc.10\n")
            self.assertEqual(read_version(path), "3.2.1-rc.10")
            path.write_bytes(b"0.1.0\n" + b"x" * 512)
            with self.assertRaises(ValueError):
                read_version(path)

    def test_manage_version_needs_no_installation_docker_or_terminal(self):
        out, err = StringIO(), StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", ["manage.py", "--version"]))
            tty = stack.enter_context(patch.object(sys.stdin, "isatty", side_effect=AssertionError("TTY checked")))
            for name in ("safe_path", "check_docker", "command", "compose_prefix"):
                stack.enter_context(patch.object(manage, name, side_effect=AssertionError(name)))
            stack.enter_context(redirect_stdout(out)); stack.enter_context(redirect_stderr(err))
            self.assertEqual(manage.main(), 0)
        self.assertEqual(out.getvalue(), "Leti Cook " + read_version() + "\n")
        self.assertEqual(err.getvalue(), "")
        tty.assert_not_called()

    def test_plain_host_python_creates_no_bytecode_or_installation_state(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            names = ("manage.py", "install.py", "application/release_version.py", "application/VERSION",
                     "application/schema_release.py", "ops/release_manifest.py", "ops/schema_manifest.py",
                     "ops/self_hosted_credentials.py", "ops/bundle_contract.py")
            for name in names:
                target = folder / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            environment = {key: value for key, value in os.environ.items()
                           if key not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPATH"}}
            environment["PATH"] = ""
            result = subprocess.run([sys.executable, "-S", str(folder / "manage.py"), "--version"],
                                    input="", capture_output=True, text=True, env=environment,
                                    cwd=folder, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "Leti Cook " + read_version() + "\n")
            self.assertEqual(result.stderr, "")
            self.assertEqual({p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}, set(names))

    def test_manage_missing_or_corrupt_version_fails_without_docker_advice(self):
        with patch.object(sys, "argv", ["manage.py", "--version"]), \
                patch.object(manage, "read_version", side_effect=ValueError("invalid_release_version")), \
                patch.object(manage, "check_docker") as docker, \
                redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err:
            self.assertEqual(manage.main(), 1)
        docker.assert_not_called()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "Could not read the product version.\n")

    def test_manage_version_cannot_hide_an_action(self):
        with patch.object(sys, "argv", ["manage.py", "--version", "restart"]), \
                patch.object(manage, "command") as command, redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                manage.main()
        self.assertEqual(raised.exception.code, 2)
        command.assert_not_called()

    def test_system_http_version_matches_the_same_file(self):
        from test_honest_status import summary
        for paused in (True, False):
            self.assertEqual(summary(paused=paused)["version"], read_version())


if __name__ == "__main__":
    unittest.main()
