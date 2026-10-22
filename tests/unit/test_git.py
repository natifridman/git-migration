import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_migration.git import GitError, GitTransport


class GitTests(unittest.TestCase):
    def test_credentials_are_ephemeral_and_errors_sanitized(self):
        helper_paths = []

        def invoke(args, **kwargs):
            self.assertNotIn("secret-value", repr(args))
            self.assertEqual(kwargs["env"]["GIT_MIGRATION_TOKEN"], "secret-value")
            self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
            helper = Path(kwargs["env"]["GIT_ASKPASS"])
            helper_paths.append(helper)
            self.assertEqual(helper.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("secret-value", helper.read_text())
            return subprocess.CompletedProcess(args, 1, "", "remote echoed secret-value")

        with patch("git_migration.git.subprocess.run", side_effect=invoke):
            with self.assertRaises(GitError) as raised:
                GitTransport(token="secret-value").refs("https://git.example/repo.git")
        self.assertNotIn("secret-value", str(raised.exception))
        self.assertTrue(all(not path.exists() for path in helper_paths))

    def test_askpass_supplies_credentials_when_global_interactivity_is_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "gitconfig"
            config.write_text("[credential]\n\tinteractive = false\n")
            with patch.dict("os.environ", {"GIT_CONFIG_GLOBAL": str(config)}):
                output = GitTransport(token="secret-value").run(
                    ["credential", "fill"],
                    cwd=root,
                    input_text="protocol=https\nhost=git.example\n\n",
                )
        credentials = dict(line.split("=", 1) for line in output.splitlines())
        self.assertEqual(credentials["username"], "oauth2")
        self.assertEqual(credentials["password"], "secret-value")

    def test_timeout_is_sanitized(self):
        with patch(
            "git_migration.git.subprocess.run", side_effect=subprocess.TimeoutExpired("secret", 1)
        ):
            with self.assertRaisesRegex(GitError, "^Git operation timed out$"):
                GitTransport().refs("https://git.example/repo.git")

    def test_embedded_credentials_rejected(self):
        with self.assertRaises(GitError):
            GitTransport().refs("https://user:password@git.example/repo.git")

    def test_exact_branch_annotated_tag_and_empty_repository(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, empty = root / "repo", root / "empty"

            def git(*args, cwd=None):
                return subprocess.run(
                    ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
                ).stdout.strip()

            git("init", "-b", "main", str(repo))
            git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.test",
                "commit",
                "--allow-empty",
                "-m",
                "initial",
                cwd=repo,
            )
            git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.test",
                "tag",
                "-a",
                "release",
                "-m",
                "release",
                cwd=repo,
            )
            git("init", "--bare", str(empty))
            transport = GitTransport()
            snapshot = transport.refs(str(repo))
            self.assertEqual(snapshot["default_branch"], "main")
            self.assertIn("refs/tags/release^{}", snapshot["refs"])
            self.assertNotEqual(
                snapshot["refs"]["refs/tags/release"], snapshot["refs"]["refs/tags/release^{}"]
            )
            self.assertEqual(transport.refs(str(empty))["refs"], {})
            transport.fetch(str(repo), root / "copy.git")
            self.assertEqual(transport.lfs_oids(root / "copy.git"), [])

    def test_historical_lfs_pointer_is_detected_alongside_binary_blobs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(
                ["git", "init", "-b", "main", str(root / "repo")], check=True, capture_output=True
            )
            repo = root / "repo"
            pointer = (
                "version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64 + "\nsize 42\n"
            )
            (repo / "large.dat").write_text(pointer)
            (repo / "binary.dat").write_bytes(b"\xff\x00\xfe")
            for args in (
                ["add", "."],
                [
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.test",
                    "commit",
                    "-m",
                    "pointer",
                ],
                ["rm", "large.dat"],
                [
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.test",
                    "commit",
                    "-m",
                    "remove",
                ],
            ):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
            transport = GitTransport()
            transport.fetch(str(repo), root / "copy.git")
            self.assertEqual(transport.lfs_oids(root / "copy.git"), ["a" * 64])

    def test_lfs_is_required_when_pointer_is_present(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = GitTransport()
            with patch.object(
                transport, "run", side_effect=GitError("Git operation failed (exit code 1)")
            ) as run:
                with self.assertRaises(GitError):
                    transport.verify_lfs("https://git.example/repo.git", Path(folder))
            self.assertEqual(run.call_args.args[0], ["lfs", "version"])


if __name__ == "__main__":
    unittest.main()
