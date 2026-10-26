"""`web.search` sources and citations, end to end through the real executor.

Sources come only from the committed provider tool result of the same call,
re-checked against the registered shape; a source is `cited` only when the
committed answer deliberately links that exact URL (a Markdown link target or
an autolink, outside code). The part stays presentation-only: third-party
titles never reach the model, so sources cannot grow the follow-up prompt or
carry text into it. A search without sources is exactly the pre-sources part.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

import omnivia_core_runtime.service.chat_generation_executor as executor_module
import pytest
from omnivia_core_runtime.service.chat_generation_executor import (
    MAX_PROVIDER_TOOL_RESULT_BYTES,
    PROVIDER_WEB_SEARCH_TOOL_NAME,
    _message_hash,
    answer_link_urls,
)
from omnivia_core_runtime.service.chat_part_registry import (
    WEB_SEARCH_MAX_SOURCES,
    WEB_SEARCH_PART_TYPE,
    PartPayloadInvalid,
    validate_registered_payload,
)
from omnivia_core_runtime.storage import chat
from test_chat_direction_continuation import (
    Crash,
    Scripted,
    assistant_messages,
    run,
    status,
    wire,
)
from test_chat_request_manifest import WORKSPACE_ID

# The fixtures and readers of the provider-tool part suite, so both run over the
# same seeded graph.
from test_chat_tool_call_part import (
    owned,
    parts,
    provider_context_through_assistant,
    seeded,
)

__all__ = ["owned", "seeded"]

QUERY = "node 24 release"
A = "https://nodejs.org/en/blog/release/v24.0.0"
B = "https://nodejs.org/en/download"
C = "https://github.com/nodejs/node/releases"
WIKI = "https://en.wikipedia.org/wiki/Node.js_(software)"


def source(url: str, title: str, domain: str, opened: bool = False) -> dict[str, Any]:
    return {"url": url, "title": title, "domain": domain, "opened": opened}


SOURCES = [
    source(A, "Node.js 24.0.0", "nodejs.org", opened=True),
    source(B, "Download Node.js", "nodejs.org"),
    source(C, "Releases", "github.com"),
    source(WIKI, "Node.js (software)", "en.wikipedia.org"),
]


def search_stream(output: dict[str, Any], text: str) -> Any:
    def script(request: Any) -> Iterator[dict[str, Any]]:
        yield wire(request, 0, "stream-start")
        yield wire(
            request,
            1,
            "tool-call",
            toolCallId="search-1",
            stepId="step-1",
            toolName=PROVIDER_WEB_SEARCH_TOOL_NAME,
            input={"query": QUERY},
        )
        yield wire(request, 2, "tool-result", toolCallId="search-1", stepId="step-1", output=output)
        yield wire(request, 3, "text-delta", delta=text, partId="part-1", stepId="step-2")
        yield wire(request, 4, "finish", finishReason="stop")

    return script


def committed_search(holder: Any) -> dict[str, Any]:
    found = [part for part in parts(holder) if part.part_type == WEB_SEARCH_PART_TYPE]
    assert len(found) == 1
    return dict(found[0].payload)


def cited_urls(holder: Any) -> list[str]:
    return [entry["url"] for entry in committed_search(holder).get("sources", []) if entry["cited"]]


def test_committed_sources_carry_exact_link_citations(seeded: Any) -> None:
    text = f"Released May 6, 2025 ([announcement]({A})). Downloads: <{B}>. Background: [wiki]({WIKI})."
    run(seeded, Scripted(search_stream({"sources": SOURCES}, text)))
    assert status(seeded).state == "succeeded", status(seeded)
    assert committed_search(seeded) == {
        "query": QUERY,
        "state": "complete",
        "sources": [
            {**SOURCES[0], "cited": True},
            {**SOURCES[1], "cited": True},
            {**SOURCES[2], "cited": False},
            {**SOURCES[3], "cited": True},
        ],
    }


@pytest.mark.parametrize(
    "text",
    [
        f"See {A} for details.",  # a bare URL in prose is not a deliberate link
        f"[{A}](https://example.com/)",  # a URL used only as a label
        f"`[x]({A})`",  # inside a code span
        f"```\n[x]({A})\n```",  # inside a code fence
        f"[x]({A}.)",  # a near miss is not the same URL
        f"[x]({A}/)",
        f"[x](http://evil.com/{A})",
        "[x](https://example.com/made-up)",  # never returned by the search
    ],
)
def test_nothing_but_an_exact_deliberate_link_is_a_citation(seeded: Any, text: str) -> None:
    run(seeded, Scripted(search_stream({"sources": SOURCES}, text)))
    assert cited_urls(seeded) == []
    assert "example.com" not in str(committed_search(seeded))


def test_sources_never_reach_the_model(seeded: Any) -> None:
    run(seeded, Scripted(search_stream({"sources": SOURCES}, f"See [it]({A}).")))
    context = provider_context_through_assistant(seeded)
    assert context.messages[-1] == {"role": "assistant", "parts": [{"kind": "text", "text": f"See [it]({A})."}]}
    assert [ref["partType"] for ref in context.manifest_message_refs[-1]["parts"]] == ["text"]
    assert "Node.js 24.0.0" not in str(context.messages)


@pytest.mark.parametrize(
    "bad",
    [
        source("http://nodejs.org/x", "Insecure", "nodejs.org"),
        source("https://127.0.0.1/admin", "Loopback", "127.0.0.1"),
        source("https://[::1]/", "IPv6", "[::1]"),
        source("https://intranet/", "Dotless", "intranet"),
        source("https://localhost./", "Trailing dot", "localhost."),
        source("https://printer.local./x", "Trailing dot local", "printer.local."),
        source("https://printer.local/", "Local", "printer.local"),
        source("https://evil.com/x", "Mismatched", "nodejs.org"),
        source("https://nodejs.org/a b", "Space", "nodejs.org"),
        source("https://user:pw@nodejs.org/", "Login", "nodejs.org"),
        source(A, " padded ", "nodejs.org"),
        source(A, "Read more\u0085", "nodejs.org"),
        source(A, "﻿Title", "nodejs.org"),
        {**source(A, "Extra", "nodejs.org"), "snippet": "third-party text"},
        {**source(A, "Smuggled cited", "nodejs.org"), "cited": True},
        {"url": ["not", "a", "string"], "title": "List", "domain": "nodejs.org", "opened": False},
        {"url": {"nested": 1}, "title": "Dict", "domain": "nodejs.org", "opened": False},
    ],
)
def test_one_unchecked_source_drops_all_sources_and_keeps_the_search(seeded: Any, bad: dict[str, Any]) -> None:
    run(seeded, Scripted(search_stream({"sources": [SOURCES[0], bad]}, f"See [it]({A}).")))
    assert status(seeded).state == "succeeded", status(seeded)
    assert committed_search(seeded) == {"query": QUERY, "state": "complete"}


def test_a_port_and_percent_encoding_are_ordinary_sources(seeded: Any) -> None:
    ok = [source("https://docs.example.com:8443/a%20b?q=1#frag", "Docs", "docs.example.com")]
    run(seeded, Scripted(search_stream({"sources": ok}, "See docs.")))
    assert committed_search(seeded)["sources"] == [{**ok[0], "cited": False}]


def test_a_source_with_a_guarded_word_still_fails_the_generation_in_core(seeded: Any) -> None:
    """Core's tool-result guard is unchanged, which is why Platform drops such sources."""
    guarded = source(
        "https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Authorization",
        "Authorization",
        "developer.mozilla.org",
    )
    run(seeded, Scripted(search_stream({"sources": [guarded]}, "See MDN.")))
    assert status(seeded).state == "failed"


def test_the_largest_admissible_search_fits_the_tool_result_cap(seeded: Any) -> None:
    host = ".".join(["a" * 63, "b" * 63, "c" * 63]) + ".example"
    assert len(host) <= 253
    biggest = [
        source(f"https://{host}/{n:02d}" + "x" * (2_048 - len(f"https://{host}/00")), "t" * 256, host)
        for n in range(WEB_SEARCH_MAX_SOURCES)
    ]
    assert len(executor_module.to_canonical_json({"sources": biggest}).encode()) < MAX_PROVIDER_TOOL_RESULT_BYTES
    run(seeded, Scripted(search_stream({"sources": biggest}, "Done.")))
    assert status(seeded).state == "succeeded", status(seeded)
    assert len(committed_search(seeded)["sources"]) == WEB_SEARCH_MAX_SOURCES


def test_an_exec_search_without_sources_is_exactly_the_pre_sources_part(seeded: Any) -> None:
    run(seeded, Scripted(search_stream({}, f"See [it]({A}).")))
    assert committed_search(seeded) == {"query": QUERY, "state": "complete"}
    calls = chat.read_chat_tool_calls_for_generation_job(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=assistant_messages(seeded)[0].generation_job_id,
    )
    result = chat.read_chat_tool_result(seeded.connection, workspace_id=WORKSPACE_ID, tool_call_id=calls[0].tool_call_id)
    assert result is not None
    text = f"See [it]({A})."
    assert _message_hash(text, calls, (), False, (), {calls[0].tool_call_id: result}) == _message_hash(text, calls)


def test_a_crash_before_the_message_replays_to_the_same_sources_and_hash(seeded: Any) -> None:
    script = search_stream({"sources": SOURCES}, f"See [it]({A}).")

    def dies(*args: Any, **kwargs: Any) -> Any:
        raise Crash

    with pytest.raises(Crash), pytest.MonkeyPatch.context() as patch:
        patch.setattr(executor_module.ChatGenerationExecutor, "_materialize_assistant", dies)
        run(seeded, Scripted(script))
    assert assistant_messages(seeded) == []

    run(seeded, Scripted(script))
    assert status(seeded).state == "succeeded", status(seeded)
    first = assistant_messages(seeded)[0].content_hash
    assert cited_urls(seeded) == [A]
    run(seeded, Scripted(script))
    assert [message.content_hash for message in assistant_messages(seeded)] == [first]


def test_the_validator_bounds_the_sources_list() -> None:
    def payload(sources: Any) -> dict[str, Any]:
        return {"query": QUERY, "state": "complete", "sources": sources}

    cited = [{**entry, "cited": False} for entry in SOURCES]
    validate_registered_payload(WEB_SEARCH_PART_TYPE, payload(cited))
    many = [{**source(f"https://a.example/{n}", f"t{n}", "a.example"), "cited": False} for n in range(21)]
    for bad in ([], many, [cited[0], copy.deepcopy(cited[0])], "not a list"):
        with pytest.raises(PartPayloadInvalid):
            validate_registered_payload(WEB_SEARCH_PART_TYPE, payload(bad))
    long_url = {**cited[0], "url": "https://nodejs.org/" + "x" * 2_100}
    long_title = {**cited[0], "title": "x" * 257}
    long_host = {**cited[0], "url": f"https://{'a' * 250}.example/", "domain": f"{'a' * 250}.example"}
    for bad_source in (long_url, long_title, long_host):
        with pytest.raises(PartPayloadInvalid):
            validate_registered_payload(WEB_SEARCH_PART_TYPE, payload([bad_source]))


def test_answer_links_are_read_in_the_exact_form_written() -> None:
    text = (
        f"[a]({A}) <{B}> [w]({WIKI}) [t]({C} \"title\") "
        f"[e](https://en.wikipedia.org/wiki/Ender's_Game) bare {A}x `[c](https://code.example/)`"
    )
    assert answer_link_urls(text) == {A, B, WIKI, C, "https://en.wikipedia.org/wiki/Ender's_Game"}
