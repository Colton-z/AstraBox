from __future__ import annotations

import unittest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    _parse_skill_descriptor,
)


class ParseSkillDescriptorTest(unittest.TestCase):
    """Contract for the git skill descriptor parser ``<repo>@<ref>#<path>``.

    The ref split is authority-aware: the ``git@`` in an scp-form SSH URL must
    NOT be mistaken for the ref delimiter. Malformed descriptors fail loud
    (APIError) so a bad template entry never reaches a sandbox exec — there is
    no fallback.
    """


    # ---- authority-aware ref split -------------------------------------------
    def test_credentials_in_an_https_source_are_refused_without_echoing_them(self) -> None:
        # The descriptor is stored in the Agent and its cache manifest and echoed
        # in clone errors; a private HTTPS source uses a Vault credential instead.
        for descriptor in (
            "https://x-access-token:TOK@github.com/o/repo@main",
            "https://TOK@github.com/o/repo.git",
        ):
            with self.subTest(descriptor=descriptor):
                with self.assertRaises(APIError) as caught:
                    _parse_skill_descriptor(descriptor)
                self.assertNotIn("TOK", caught.exception.message)
                self.assertIn("github.com", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
