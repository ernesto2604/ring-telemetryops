"""RING-008: the Bedrock client uses boto3's standard credential chain unless keys are set."""
import pytest

from src.agent.concierge import SupportConcierge


@pytest.fixture
def boto3_calls(monkeypatch):
    calls = []
    monkeypatch.setattr("boto3.client", lambda **kwargs: calls.append(kwargs) or object())
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION",
                 "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(name, raising=False)
    return calls


def test_without_explicit_keys_boto3_resolves_its_own_chain(boto3_calls):
    SupportConcierge()._get_client()

    assert boto3_calls == [{"service_name": "bedrock-runtime", "region_name": "us-east-1"}]


@pytest.mark.parametrize("key_id, secret", [("", ""), ("  ", "  "), ("AKIA_TEST_ONLY", ""), ("", "secret-test-only")])
def test_empty_or_partial_keys_are_never_passed(boto3_calls, monkeypatch, key_id, secret):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", key_id)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", secret)

    SupportConcierge()._get_client()

    assert "aws_access_key_id" not in boto3_calls[0]
    assert "aws_secret_access_key" not in boto3_calls[0]


def test_explicit_keys_and_session_token_are_passed(boto3_calls, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA_TEST_ONLY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-test-only")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-test-only")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")

    SupportConcierge()._get_client()

    assert boto3_calls == [{
        "service_name": "bedrock-runtime", "region_name": "eu-west-1",
        "aws_access_key_id": "AKIA_TEST_ONLY", "aws_secret_access_key": "secret-test-only",
        "aws_session_token": "session-test-only",
    }]


def test_client_is_created_once(boto3_calls):
    agent = SupportConcierge()
    assert agent._get_client() is agent._get_client()
    assert len(boto3_calls) == 1


def test_unmocked_bedrock_call_is_blocked_by_the_suite_guard():
    agent = SupportConcierge()
    with pytest.raises(AssertionError, match="must not create real AWS clients"):
        agent._call_llm("prompt", None)
