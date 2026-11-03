"""The Claude adapter's message translation, with a stub client (no API calls)."""

from types import SimpleNamespace

from langchain_core.messages import HumanMessage, ToolMessage

from rewind.agent import tool_schemas
from rewind.claude_model import ClaudeChatModel, to_anthropic_messages


class Block(SimpleNamespace):
    def to_dict(self):
        return dict(self.__dict__)


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def test_tool_call_round_trip_keeps_raw_blocks():
    thinking = Block(type="thinking", thinking="", signature="sig")
    call = Block(type="tool_use", id="toolu_1", name="send_email",
                 input={"to": "a@b.test", "subject": "s", "body": "b"})  # fmt: skip
    client = StubClient([SimpleNamespace(stop_reason="tool_use", content=[thinking, call])])
    model = ClaudeChatModel(client=client).bind_tools(tool_schemas())

    ai = model.invoke([HumanMessage("hi")])

    assert ai.tool_calls[0]["name"] == "send_email"
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert {t["name"] for t in req["tools"]} == {"stripe_refund", "send_email"}

    # Next turn: the assistant's blocks go back unchanged, results in one user message.
    _, msgs = to_anthropic_messages(
        [HumanMessage("hi"), ai, ToolMessage('{"status": "CONFIRMED"}', tool_call_id="toolu_1")]
    )
    assert msgs[1] == {"role": "assistant", "content": [thinking.to_dict(), call.to_dict()]}
    assert msgs[2]["content"][0]["tool_use_id"] == "toolu_1"


def test_refusal_becomes_plain_reply():
    client = StubClient([SimpleNamespace(stop_reason="refusal", content=[])])
    ai = ClaudeChatModel(client=client).invoke([HumanMessage("hi")])
    assert not ai.tool_calls
