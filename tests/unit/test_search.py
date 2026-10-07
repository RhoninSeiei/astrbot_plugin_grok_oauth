import copy
import json
import time

import httpx
import pytest

from grok_oauth.errors import EmptyOutput, InvalidRequest, ProtocolError, RateLimited
from grok_oauth.http import AuthorizedHttp
from grok_oauth.models import RequestPolicy, TokenSnapshot
from grok_oauth.responses import ResponsesClient, normalize_response
from grok_oauth.search import SearchClient, web_search_tool


def raw_search():
    return {
        "id": "search-1",
        "model": "grok-4.6",
        "status": "completed",
        "output": [
            {
                "id": "ws-1",
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search", "query": "latest"},
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Current facts.",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://example.com/news",
                                "title": "News",
                                "start_index": 0,
                                "end_index": 5,
                            }
                        ],
                    }
                ],
            },
        ],
        "usage": {"input_tokens": 20, "output_tokens": 10},
    }


class OAuth:
    async def get_token(self, **kwargs):
        return TokenSnapshot("default", "synthetic-oauth", "synthetic-refresh", None, "s", "c")


async def test_explicit_search_payload_and_grounded_result():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer synthetic-oauth"
        return httpx.Response(200, json=raw_search())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SearchClient(ResponsesClient(AuthorizedHttp(http, OAuth())))
        result = await client.search(
            " Latest facts ",
            model="grok-4.6",
            allowed_domains=["Example.COM"],
            policy=RequestPolicy(deadline=time.monotonic() + 10),
        )
    assert len(sent) == 1
    assert sent[0]["tools"] == [
        {"type": "web_search", "filters": {"allowed_domains": ["example.com"]}}
    ]
    assert sent[0]["tool_choice"] == "required" and sent[0]["store"] is False
    assert "previous_response_id" not in sent[0]
    assert result.text == "Current facts." and result.search_calls == 1
    assert result.citations == [{"url": "https://example.com/news", "title": "News"}]
    assert result.usage["input_tokens"] == 20


@pytest.mark.parametrize(
    "domains",
    [
        "example.com",
        ["https://example.com"],
        ["*.example.com"],
        ["localhost"],
        ["127.0.0.1"],
        ["example.com:443"],
        ["example.com/path"],
        ["a.com"] * 6,
        [None],
    ],
)
def test_domain_filters_reject_invalid_values(domains):
    with pytest.raises(InvalidRequest):
        web_search_tool(domains)


@pytest.mark.parametrize("query", [None, "", "   ", "x" * 4001, 42])
async def test_invalid_query_never_requests(query):
    class NoRequests:
        async def create(self, *args, **kwargs):
            pytest.fail("Invalid query reached inference")

    with pytest.raises(InvalidRequest):
        await SearchClient(NoRequests()).search(query, model="grok-4.6", policy=RequestPolicy())


@pytest.mark.parametrize(
    "kind", ["no_search", "failed_search", "foreign_native", "no_text", "local_function"]
)
async def test_search_requires_completed_search_and_text(kind):
    raw = raw_search()
    if kind == "no_search":
        raw["output"].pop(0)
    elif kind == "failed_search":
        raw["output"][0]["status"] = "failed"
    elif kind == "foreign_native":
        raw["output"][0]["type"] = "code_interpreter_call"
    elif kind == "no_text":
        raw["output"].pop()
    else:
        raw["output"].append(
            {"type": "function_call", "call_id": "f", "name": "unrequested", "arguments": "{}"}
        )

    class Responses:
        async def create(self, *args, **kwargs):
            return normalize_response(raw)

    with pytest.raises((EmptyOutput, ProtocolError)):
        await SearchClient(Responses()).search("facts", model="grok-4.6", policy=RequestPolicy())


async def test_search_429_is_terminal_and_retains_status():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(429, json={"error": "private-body"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(RateLimited) as caught:
            await SearchClient(ResponsesClient(AuthorizedHttp(http, OAuth()))).search(
                "facts", model="grok-4.6", policy=RequestPolicy()
            )
    assert len(sent) == 1 and caught.value.status_code == 429
    assert "private-body" not in str(caught.value)


def test_citations_keep_safe_sources_without_modifying_upstream():
    raw = raw_search()
    annotations = raw["output"][1]["content"][0]["annotations"]
    annotations.extend(
        [
            {"type": "url_citation", "url": "javascript:alert(1)", "title": "unsafe"},
            {
                "type": "url_citation",
                "url": "https://user:password@example.com/",
                "title": "unsafe",
            },
            {"type": "url_citation", "url": "http://127.0.0.1/", "title": "unsafe"},
            {"type": "url_citation", "url": "https://example.com/news", "title": "duplicate"},
        ]
    )
    before = copy.deepcopy(raw)
    result = normalize_response(raw)
    assert result.citations == [{"url": "https://example.com/news", "title": "News"}]
    assert raw == before


def test_native_action_sources_are_preserved_when_annotations_missing():
    raw = raw_search()
    raw["output"][1]["content"][0]["annotations"] = []
    raw["output"][0]["action"]["sources"] = [{"url": "https://example.com/news", "title": "News"}]
    assert normalize_response(raw).citations == [
        {"url": "https://example.com/news", "title": "News"}
    ]


async def test_large_search_result_is_explicitly_truncated_and_usage_sanitized():
    raw = raw_search()
    raw["output"][1]["content"][0]["text"] = "a" * 20000
    raw["usage"]["unrecognized_private_field"] = "should-not-escape"

    class Responses:
        async def create(self, *args, **kwargs):
            return normalize_response(raw)

    result = await SearchClient(Responses()).search(
        "facts", model="grok-4.6", policy=RequestPolicy()
    )
    assert len(result.text) == 16000 and result.truncated
    assert "unrecognized_private_field" not in result.usage
