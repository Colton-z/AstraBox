"""Diagnostic reports must not hand out the secrets they quote.

``GET /v1/sandboxes/{id}/diagnostics/{scope}`` returns prose rendered by the
OpenSandbox server. On its DOCKER runtime, ``inspect`` renders the container's
whole ``Environment:`` block (and ``summary`` is inspect + events + logs, so it
inherits that), masking only names containing SECRET / TOKEN / PASSWORD / KEY.

That default does not know AstraBox's vocabulary. Two of the values the
orchestrator injects are secrets whose NAMES contain none of those four words,
so they render in full:

* ``_ASTRABOX_TRANSCRIPT_BACKEND_BASE_URL`` — its path carries a transcript
  capability token (read/write on that session's transcript).
* ``ANTHROPIC_CUSTOM_HEADERS`` — the Langfuse correlation pair naming the
  conversation and the AstraBox user.

So this backend redacts the text itself, on an ALLOWLIST, and does not rely on
the server's keyword rule. The fixtures below are shaped like the real reports
(``services/docker/docker_diagnostics.py`` and ``services/k8s/k8s_diagnostics.py``
upstream) so the tests pin behavior against the format actually received.

The half of this file that matters most is what a redacted value can DO. A value
may contain newlines, so it renders as several lines and only the first is
shaped like an assignment; the rest is secret that has to stay masked. Every
candidate for "the value ended here" — a blank line, a ``Word:`` header, a line
spelling an allowlisted assignment — is a string the value itself can contain,
which means honouring any of them lets the value end its own redaction and
publish its second half. So none of them do, and the tests below say so one
forgery at a time. The price is a section of over-redaction, pinned in
``TheCostOfClosingTheSpanTests`` so it reads as a decision rather than a bug.
"""

from __future__ import annotations

import unittest

from astrabox.providers.open_sandbox.sandbox import _redact_env_values

_CAPABILITY_TOKEN = "eyJzaWQiOiJjb252LTEifQ.c2lnbmF0dXJlLWhlcmU"
_TRANSCRIPT_URL = f"https://astrabox.internal/api/v1/sbxcap/{_CAPABILITY_TOKEN}"

#: Shaped like the docker runtime's ``inspect``: labels (lowercase, dotted),
#: then the environment block with the server's own masking already applied to
#: the names it recognises.
_DOCKER_INSPECT = f"""Container ID:   3f1a9c2b
Image:          astrabox/agent:latest
Status:         running

Network:
  bridge: 172.18.0.4

Labels:
  astrabox.managed-by=astrabox
  astrabox.session-id=conv-1

Environment:
  PATH=/usr/local/bin:/usr/bin:/bin
  HOME=/home/convuser
  IS_SANDBOX=1
  ANTHROPIC_BASE_URL=https://litellm.internal/v1
  ANTHROPIC_MODEL=claude-opus-4
  ANTHROPIC_AUTH_TOKEN=***
  _ASTRABOX_TRANSCRIPT_BACKEND_BASE_URL={_TRANSCRIPT_URL}
  ANTHROPIC_CUSTOM_HEADERS=langfuse_session_id: conv-1, langfuse_trace_user_id: user-42
  ASTRABOX_MCP_AUTH_FILE=/run/astrabox/mcp-auth-conv-1.json
"""


class TranscriptCapabilityTokenRedactionTests(unittest.TestCase):
    def test_capability_token_never_survives_a_report(self) -> None:
        out = _redact_env_values(_DOCKER_INSPECT)
        self.assertNotIn(_CAPABILITY_TOKEN, out)
        self.assertNotIn(_TRANSCRIPT_URL, out)
        self.assertIn("_ASTRABOX_TRANSCRIPT_BACKEND_BASE_URL=***", out)

    def test_correlation_headers_never_survive_a_report(self) -> None:
        out = _redact_env_values(_DOCKER_INSPECT)
        self.assertNotIn("langfuse_session_id", out)
        self.assertNotIn("langfuse_trace_user_id", out)
        self.assertNotIn("user-42", out)
        self.assertIn("ANTHROPIC_CUSTOM_HEADERS=***", out)

    def test_unknown_variables_are_redacted_by_default(self) -> None:
        # The allowlist's reason for existing: a variable nobody has classified
        # yet is redacted, so adding one cannot silently create a leak.
        out = _redact_env_values("Environment:\n  SOME_FUTURE_CREDENTIAL=hunter2\n")
        self.assertNotIn("hunter2", out)
        self.assertIn("SOME_FUTURE_CREDENTIAL=***", out)

    def test_leading_underscore_names_are_matched(self) -> None:
        # AstraBox's internal wire variables are underscore-prefixed; a name
        # pattern that missed them would miss the capability token exactly.
        out = _redact_env_values("  _ASTRABOX_SOMETHING=secret-value\n")
        self.assertNotIn("secret-value", out)


class RedactionCannotBeSteppedAroundTests(unittest.TestCase):
    """The two shapes an env line can take that a NAME-only rule lets through."""

    def test_a_lowercase_variable_name_is_still_redacted(self) -> None:
        # An allowlist keyed on upper-snake names says nothing about a variable
        # whose name is not upper-snake — and "said nothing" must mean redacted,
        # not printed. Same value, same danger, different spelling.
        out = _redact_env_values(f"Environment:\n  transcript_url={_TRANSCRIPT_URL}\n")
        self.assertNotIn(_CAPABILITY_TOKEN, out)
        self.assertIn("transcript_url=***", out)

    def test_a_mixed_case_variable_name_is_still_redacted(self) -> None:
        out = _redact_env_values("Environment:\n  Anthropic_Custom_Headers=user-42\n")
        self.assertNotIn("user-42", out)
        self.assertIn("Anthropic_Custom_Headers=***", out)

    def test_a_multiline_value_is_redacted_past_its_first_line(self) -> None:
        # A value containing a newline renders as several lines: only the first
        # one is shaped like an assignment. The rest is still the value, and it
        # is the half that carries the correlation pair here.
        report = (
            "Environment:\n"
            "  ANTHROPIC_CUSTOM_HEADERS=x-agent-header: set-by-the-agent\n"
            "langfuse_session_id: conv-1, langfuse_trace_user_id: user-42\n"
            "  PATH=/usr/local/bin:/usr/bin:/bin\n"
        )
        out = _redact_env_values(report)
        self.assertNotIn("langfuse_trace_user_id", out)
        self.assertNotIn("user-42", out)
        # A line that is itself an assignment is judged on its own name, which
        # keeps the block readable. It is also the stated residue: a value whose
        # continuation happens to spell an allowlisted assignment gets that one
        # line printed, but printing it must not reopen PROSE printing for the
        # rest of the value — see the two tests below.
        self.assertIn("PATH=/usr/local/bin:/usr/bin:/bin", out)

    def test_a_name_outside_any_charset_is_still_redacted(self) -> None:
        # The direction that matters: a name this module cannot DESCRIBE must
        # not be a name it PRINTS. A pattern that spelled out which characters a
        # name may hold would answer "not a variable" for each of these and let
        # the value through whole — the exact failure the allowlist exists to
        # rule out.
        for line in (
            f"  2FA_TRANSCRIPT_URL={_TRANSCRIPT_URL}",
            f"  agent[0].token={_TRANSCRIPT_URL}",
            f"  x:custom:header={_TRANSCRIPT_URL}",
            f"  ~weird~={_TRANSCRIPT_URL}",
        ):
            out = _redact_env_values(f"Environment:\n{line}\n")
            self.assertNotIn(_CAPABILITY_TOKEN, out, line)
            self.assertIn("=***", out, line)

    def test_a_section_header_inside_a_value_does_not_reopen_printing(self) -> None:
        # A header is just `Word:` — a shape any value can contain, and the head
        # half of ANTHROPIC_CUSTOM_HEADERS is set by the agent. Honouring one as
        # a span boundary let a multi-line value end its own redaction and hand
        # out the rest of itself.
        report = (
            "Environment:\n"
            "  ANTHROPIC_CUSTOM_HEADERS=x-agent-header: set-by-the-agent\n"
            "Recent events:\n"
            "langfuse_session_id: conv-1, langfuse_trace_user_id: user-42\n"
        )
        out = _redact_env_values(report)
        self.assertNotIn("langfuse_trace_user_id", out)
        self.assertNotIn("user-42", out)

    def test_a_blank_line_inside_a_value_does_not_reopen_printing(self) -> None:
        # The blank line was the last forgeable boundary left. A value can hold
        # "\n\n" as easily as it holds a section header, so honouring one as the
        # end of a redaction let the value publish its own second half.
        report = (
            "Environment:\n"
            "  ANTHROPIC_CUSTOM_HEADERS=x-agent-header: set-by-the-agent\n"
            "\n"
            "langfuse_session_id: conv-1, langfuse_trace_user_id: user-42\n"
        )
        out = _redact_env_values(report)
        self.assertNotIn("langfuse_trace_user_id", out)
        self.assertNotIn("user-42", out)

    def test_an_allowlisted_line_inside_a_value_does_not_reopen_printing(self) -> None:
        # The other forgeable boundary: a value that spells an allowlisted
        # assignment. That one line is printed — it is a line the allowlist
        # would have printed anyway — but it must not hand the REST of the value
        # back to the reader as prose.
        report = (
            "Environment:\n"
            "  ANTHROPIC_CUSTOM_HEADERS=x-agent-header: set-by-the-agent\n"
            "  PATH=/usr/local/bin\n"
            "langfuse_session_id: conv-1, langfuse_trace_user_id: user-42\n"
        )
        out = _redact_env_values(report)
        self.assertNotIn("langfuse_trace_user_id", out)
        self.assertNotIn("user-42", out)

    def test_nothing_a_value_can_contain_ends_the_redaction(self) -> None:
        # The property stated as one test: whatever the agent-settable half of
        # a value spells, the line after it is not printed. Anything a value can
        # hold is a string this pass cannot tell from the report's own framing,
        # so none of it is allowed to decide.
        for forged in (
            "",  # a blank line
            "Recent events:",  # a section header
            "  PATH=/usr/local/bin",  # an allowlisted assignment
            "  SOME_OTHER=thing",  # an unrecognised one
            "----------",  # a rule
            "}",  # the end of a JSON blob
        ):
            report = (
                "Environment:\n"
                "  ANTHROPIC_CUSTOM_HEADERS=x-agent-header: set-by-the-agent\n"
                f"{forged}\n"
                "langfuse_trace_user_id: user-42\n"
            )
            out = _redact_env_values(report)
            self.assertNotIn("user-42", out, forged)

    def test_an_unknown_label_shaped_key_is_redacted(self) -> None:
        # The label block is readable because the two keys AstraBox writes are
        # named as safe — not because label-shaped keys are exempt.
        out = _redact_env_values("Labels:\n  vendor.injected-label=whatever\n")
        self.assertNotIn("whatever", out)
        self.assertIn("vendor.injected-label=***", out)


class TheCostOfClosingTheSpanTests(unittest.TestCase):
    """What refusing every forgeable boundary costs, pinned so it is not a surprise.

    Nothing a report renders as text can be PROVEN to be the report's own
    framing rather than the inside of a value — the two are the same bytes. So
    once a value has been redacted, prose is masked to the end of that report,
    and the price is paid where an operator will actually meet it: ``summary``
    is inspect + events + logs, and its environment block comes first.
    """

    def test_prose_after_a_redacted_value_is_masked_to_the_end(self) -> None:
        report = (
            "Environment:\n"
            "  SOME_FUTURE_CREDENTIAL=hunter2\n"
            "\n"
            "Recent events:\n"
            "  container started\n"
        )
        out = _redact_env_values(report)
        self.assertNotIn("hunter2", out)
        # The section that follows is masked. It is readable in full as its own
        # scope (`events`), which is the recovery an operator has.
        self.assertNotIn("Recent events:", out)
        self.assertNotIn("container started", out)

    def test_the_blocks_own_lines_stay_legible(self) -> None:
        # The cost falls on prose, not on the assignment block: every following
        # line that IS an assignment is still judged on its own name, so an
        # operator keeps the names and the allowlisted values.
        out = _redact_env_values(_DOCKER_INSPECT)
        self.assertIn("ASTRABOX_MCP_AUTH_FILE=***", out)
        self.assertIn("ANTHROPIC_CUSTOM_HEADERS=***", out)

    def test_a_report_with_nothing_to_redact_is_untouched(self) -> None:
        # The masking starts at the first unrecognised assignment and not
        # before, so a report that has none reads exactly as the server wrote it.
        report = "Recent events:\n  container started\n  probe ok\n"
        self.assertEqual(_redact_env_values(report), report)


class ReportStaysReadableTests(unittest.TestCase):
    def test_allowlisted_operational_values_are_kept(self) -> None:
        out = _redact_env_values(_DOCKER_INSPECT)
        self.assertIn("PATH=/usr/local/bin:/usr/bin:/bin", out)
        self.assertIn("HOME=/home/convuser", out)
        self.assertIn("IS_SANDBOX=1", out)
        # The endpoint and model are what an operator is usually here to read.
        self.assertIn("ANTHROPIC_BASE_URL=https://litellm.internal/v1", out)
        self.assertIn("ANTHROPIC_MODEL=claude-opus-4", out)

    def test_variable_names_are_always_kept(self) -> None:
        # "Set, value hidden" and "not set at all" are different diagnoses.
        out = _redact_env_values(_DOCKER_INSPECT)
        self.assertIn("ANTHROPIC_CUSTOM_HEADERS=", out)
        self.assertIn("ASTRABOX_MCP_AUTH_FILE=", out)

    def test_lowercase_dotted_labels_are_untouched(self) -> None:
        # The k8s and docker renderers both emit a Labels block as `  k=v`.
        # Those keys are not env-var-shaped and must stay readable.
        out = _redact_env_values(_DOCKER_INSPECT)
        self.assertIn("astrabox.managed-by=astrabox", out)
        self.assertIn("astrabox.session-id=conv-1", out)

    def test_non_environment_prose_is_untouched(self) -> None:
        out = _redact_env_values(_DOCKER_INSPECT)
        for line in (
            "Container ID:   3f1a9c2b",
            "Image:          astrabox/agent:latest",
            "  bridge: 172.18.0.4",
        ):
            self.assertIn(line, out)

    def test_kubernetes_inspect_has_nothing_to_redact(self) -> None:
        # The k8s renderer emits no environment block at all, so the pass must
        # be a no-op on it rather than mangling resource/condition lines.
        k8s_report = (
            "Pod Name:       sbx-abc\n"
            "Namespace:      astrabox\n"
            "Phase:          Running\n"
            "\n"
            "Conditions:\n"
            "  Ready: True (reason=N/A)\n"
            "\n"
            "Resources:\n"
            "  agent:\n"
            "    Requests: {'cpu': '1', 'memory': '2Gi'}\n"
        )
        self.assertEqual(_redact_env_values(k8s_report), k8s_report)

    def test_empty_and_assignment_free_text_pass_through(self) -> None:
        self.assertEqual(_redact_env_values(""), "")
        self.assertEqual(_redact_env_values("(no events)"), "(no events)")


if __name__ == "__main__":
    unittest.main()
