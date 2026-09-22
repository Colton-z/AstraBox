from __future__ import annotations

import unittest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    _parse_skill_descriptor,
    skill_repo_egress_hosts,
)


class ParseSkillDescriptorTest(unittest.TestCase):
    """Contract for the git skill descriptor parser ``<repo>@<ref>#<path>``.

    The ref split is authority-aware: a credential ``@`` in an https URL or the
    ``git@`` in an scp-form SSH URL must NOT be mistaken for the ref delimiter.
    Malformed descriptors fail loud (APIError) so a bad template entry never
    reaches a sandbox exec — there is no fallback.
    """

    def test_skill_git_origins_preserve_descriptor_authority(self) -> None:
        for descriptor, expected in (
            ("https://GitHub.com/o/repo@main#skills/foo", ["github.com"]),
            ("https://user:token@GitHub.com/o/repo@main", ["github.com"]),
            ("git@GitHub.com:o/repo@v1.0", ["github.com"]),
            ("ssh://git@GitHub.com/o/repo@v1.0", ["github.com"]),
            ("/opt/repos/skills#skills/foo", []),
            ("file:///opt/repos/skills#skills/foo", []),
            ("file://localhost/opt/repos/skills#skills/foo", []),
        ):
            with self.subTest(descriptor=descriptor):
                self.assertEqual(skill_repo_egress_hosts([descriptor, descriptor]), expected)

    # ---- happy paths ---------------------------------------------------------
    def test_plain_repo_derives_name_from_repo(self) -> None:
        repo, ref, path, name = _parse_skill_descriptor("https://github.com/o/repo.git")
        self.assertEqual(repo, "https://github.com/o/repo.git")
        self.assertEqual(ref, "")
        self.assertEqual(path, "")
        self.assertEqual(name, "repo")

    def test_branch_ref(self) -> None:
        repo, ref, path, name = _parse_skill_descriptor("https://github.com/o/repo@main")
        self.assertEqual(repo, "https://github.com/o/repo")
        self.assertEqual(ref, "main")
        self.assertEqual(path, "")
        self.assertEqual(name, "repo")

    def test_path_derives_name_from_path(self) -> None:
        repo, ref, path, name = _parse_skill_descriptor(
            "https://github.com/o/repo#skills/foo"
        )
        self.assertEqual(repo, "https://github.com/o/repo")
        self.assertEqual(ref, "")
        self.assertEqual(path, "skills/foo")
        self.assertEqual(name, "foo")

    def test_branch_and_path(self) -> None:
        repo, ref, path, name = _parse_skill_descriptor(
            "https://github.com/o/repo@release/v2#skills/bar"
        )
        self.assertEqual(repo, "https://github.com/o/repo")
        self.assertEqual(ref, "release/v2")
        self.assertEqual(path, "skills/bar")
        self.assertEqual(name, "bar")

    def test_trailing_slash_in_path_is_stripped(self) -> None:
        _, _, path, name = _parse_skill_descriptor(
            "https://github.com/o/repo#skills/foo/"
        )
        self.assertEqual(path, "skills/foo")
        self.assertEqual(name, "foo")

    def test_commit_sha_ref_is_returned_verbatim(self) -> None:
        # The parser does not classify SHA vs branch (the in-box git loop does);
        # it just returns the ref unchanged.
        _, ref, _, _ = _parse_skill_descriptor("https://github.com/o/repo@a1b2c3d4")
        self.assertEqual(ref, "a1b2c3d4")

    # ---- authority-aware ref split -------------------------------------------
    def test_embedded_credentials_with_ref(self) -> None:
        # the credential '@' before the host must NOT be taken as the ref delimiter
        repo, ref, _, name = _parse_skill_descriptor(
            "https://x-access-token:TOK@github.com/o/repo@main"
        )
        self.assertEqual(repo, "https://x-access-token:TOK@github.com/o/repo")
        self.assertEqual(ref, "main")
        self.assertEqual(name, "repo")

    def test_embedded_credentials_without_ref(self) -> None:
        repo, ref, _, name = _parse_skill_descriptor(
            "https://x:tok@github.com/o/repo.git"
        )
        self.assertEqual(repo, "https://x:tok@github.com/o/repo.git")
        self.assertEqual(ref, "")
        self.assertEqual(name, "repo")

    def test_scp_ssh_without_ref(self) -> None:
        # git@host:org/repo — the scp userinfo '@' must not be a ref delimiter
        repo, ref, _, name = _parse_skill_descriptor("git@github.com:o/repo.git")
        self.assertEqual(repo, "git@github.com:o/repo.git")
        self.assertEqual(ref, "")
        self.assertEqual(name, "repo")

    def test_scp_ssh_with_ref(self) -> None:
        repo, ref, _, name = _parse_skill_descriptor("git@github.com:o/repo@v1.0")
        self.assertEqual(repo, "git@github.com:o/repo")
        self.assertEqual(ref, "v1.0")
        self.assertEqual(name, "repo")

    # ---- fail-loud errors ----------------------------------------------------
    def test_empty_raises(self) -> None:
        with self.assertRaises(APIError):
            _parse_skill_descriptor("")

    def test_whitespace_only_raises(self) -> None:
        with self.assertRaises(APIError):
            _parse_skill_descriptor("   ")

    def test_missing_repo_raises(self) -> None:
        # just a path, no repo
        with self.assertRaises(APIError):
            _parse_skill_descriptor("#skills/foo")

    def test_malformed_ref_double_at_raises(self) -> None:
        with self.assertRaises(APIError):
            _parse_skill_descriptor("https://github.com/o/repo@a@b")

    def test_invalid_derived_name_raises(self) -> None:
        # a path whose basename starts with a dot is not a valid skill name
        with self.assertRaises(APIError):
            _parse_skill_descriptor("https://github.com/o/repo#.hidden")


if __name__ == "__main__":
    unittest.main()
