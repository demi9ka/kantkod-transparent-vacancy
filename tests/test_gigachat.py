import time
import uuid
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.errors import ModelResponseError, ServiceError
from app.services.gigachat import GigaChatClient


def response(data, status=200):
    result = Mock(status_code=status, ok=status < 400)
    result.json.return_value = data
    return result


def test_oauth_cached_and_401_refreshes():
    config = Settings(_env_file=None, gigachat_auth_key=SecretStr("test-key"))
    client = GigaChatClient(config)
    client.session = Mock()
    client.session.request.side_effect = [
        response({"access_token": "first", "expires_at": (time.time() + 1800) * 1000}),
        response({"data": []}),
        response({}, 401),
        response({"access_token": "second", "expires_at": time.time() + 1800}),
        response({"data": [{"id": "GigaChat"}]}),
    ]
    assert client.models() == {"data": []}
    assert client.models()["data"][0]["id"] == "GigaChat"
    calls = client.session.request.call_args_list
    oauth_calls = [call for call in calls if call.args[0] == "POST"]
    assert len(oauth_calls) == 2
    for call in oauth_calls:
        assert uuid.UUID(call.kwargs["headers"]["RqUID"]).version == 4
        assert call.kwargs["headers"]["Authorization"] == "Basic test-key"
        assert call.kwargs["data"] == {"scope": "GIGACHAT_API_PERS"}
        assert call.kwargs["verify"] is True
    assert calls[-1].kwargs["headers"]["Authorization"] == "Bearer second"


def test_completion_sends_prompt_and_vacancy():
    client = GigaChatClient(Settings(_env_file=None))
    client._request = Mock(return_value={"choices": [{"finish_reason": "stop", "message": {"content": '{"issues":[]}'}}]})
    assert client.complete("Промпт", {"id": "1"}) == '{"issues":[]}'
    request = client._request.call_args
    assert request.args == ("POST", "chat/completions")
    assert request.kwargs["json"]["messages"][0]["content"].startswith("Промпт")
    assert request.kwargs["json"]["messages"][1]["content"] == '{"id": "1"}'
    output = request.kwargs["json"]["response_format"]
    assert output["type"] == "json_schema"
    assert output["strict"] is True
    assert "problem" in output["schema"]["$defs"]["Issue"]["required"]


def test_invalid_completion_json_is_distinct_from_transport_error():
    client = GigaChatClient(Settings(_env_file=None))
    client._token = "test-token"
    client._expires_at = time.time() + 1800
    client.session = Mock()
    invalid = Mock(ok=True)
    invalid.json.side_effect = ValueError("not JSON")
    client.session.request.return_value = invalid
    with pytest.raises(ModelResponseError):
        client.complete("prompt", {"id": "1"})
    # Ошибки HTTP и авторизации не считаются ошибками содержания замечаний.
    client.session.request.return_value = response({}, status=403)
    with pytest.raises(ServiceError) as error:
        client.complete("prompt", {"id": "1"})
    assert not isinstance(error.value, ModelResponseError)
    assert "403" in str(error.value)
