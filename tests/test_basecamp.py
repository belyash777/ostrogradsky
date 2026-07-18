"""Tests for the Basecamp CLI wrapper."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from bcworker.basecamp import (
    BasecampClient,
    BasecampError,
    Todo,
    _extract_todos,
    _todo_from_dict,
)

from conftest import read_cli_records


def _set_output(data: object, *, ok: bool = True, exit_code: int = 0) -> None:
    envelope = {"ok": ok, "data": data, "summary": "test"}
    os.environ["FAKE_STDOUT"] = json.dumps(envelope)
    os.environ["FAKE_EXIT"] = str(exit_code)


# --- pure helpers ---------------------------------------------------------


def test_task_text_title_only() -> None:
    assert Todo(id=1, title="Fix bug").task_text == "Fix bug"


def test_task_text_with_description() -> None:
    todo = Todo(id=1, title="Fix bug", description="Details here")
    assert todo.task_text == "Fix bug\n\nDetails here"


def test_extract_todos_from_list() -> None:
    assert _extract_todos([{"id": 1}, "junk", {"id": 2}]) == [{"id": 1}, {"id": 2}]


def test_extract_todos_from_object() -> None:
    assert _extract_todos({"todos": [{"id": 5}]}) == [{"id": 5}]


def test_extract_todos_unknown_shape() -> None:
    assert _extract_todos(42) == []


def test_todo_from_dict_prefers_title_then_content() -> None:
    assert _todo_from_dict({"id": 3, "content": "C"}).title == "C"
    assert _todo_from_dict({"id": 3, "title": "T", "content": "C"}).title == "T"


def test_todo_from_dict_requires_int_id() -> None:
    assert _todo_from_dict({"title": "no id"}) is None
    assert _todo_from_dict({"id": "3"}) is None
    # bool is a subclass of int and must be rejected.
    assert _todo_from_dict({"id": True, "title": "x"}) is None


# --- CLI integration via the fake binary ----------------------------------


async def test_assigned_todos_parses_output(
    make_client: Callable[..., BasecampClient],
) -> None:
    _set_output({"todos": [{"id": 10, "title": "One"}, {"id": 11, "title": "Two"}]})
    client = make_client()
    todos = await client.assigned_todos()
    assert [t.id for t in todos] == [10, 11]


async def test_auth_status_reports_authenticated(
    make_client: Callable[..., BasecampClient],
) -> None:
    _set_output({"authenticated": True, "user_id": "42"})
    client = make_client()
    assert await client.is_authenticated() is True


async def test_is_authenticated_false_on_error(
    make_client: Callable[..., BasecampClient],
) -> None:
    os.environ["FAKE_EXIT"] = "1"
    os.environ["FAKE_STDERR"] = "not logged in"
    client = make_client()
    assert await client.is_authenticated() is False


async def test_nonzero_exit_raises(make_client: Callable[..., BasecampClient]) -> None:
    os.environ["FAKE_EXIT"] = "3"
    os.environ["FAKE_STDERR"] = "kaboom"
    client = make_client()
    with pytest.raises(BasecampError, match="kaboom"):
        await client.assigned_todos()


async def test_ok_false_envelope_raises(
    make_client: Callable[..., BasecampClient],
) -> None:
    _set_output(None, ok=False)
    client = make_client()
    with pytest.raises(BasecampError, match="failure"):
        await client.assigned_todos()


async def test_invalid_json_raises(make_client: Callable[..., BasecampClient]) -> None:
    os.environ["FAKE_STDOUT"] = "not json"
    client = make_client()
    with pytest.raises(BasecampError, match="Invalid JSON"):
        await client.assigned_todos()


async def test_empty_output_raises(make_client: Callable[..., BasecampClient]) -> None:
    os.environ["FAKE_STDOUT"] = ""
    client = make_client()
    with pytest.raises(BasecampError, match="Empty output"):
        await client.assigned_todos()


async def test_timeout_raises(make_client: Callable[..., BasecampClient]) -> None:
    os.environ["FAKE_SLEEP"] = "5"
    client = make_client(timeout_seconds=1)
    with pytest.raises(BasecampError, match="timed out"):
        await client.assigned_todos()


async def test_missing_binary_raises(tmp_path_factory: pytest.TempPathFactory) -> None:
    client = BasecampClient(
        bin_path="/nonexistent/basecamp-xyz",
        config_dir=tmp_path_factory.mktemp("cfg"),
    )
    with pytest.raises(BasecampError, match="not found"):
        await client.auth_status()


async def test_create_comment_succeeds(
    make_client: Callable[..., BasecampClient],
) -> None:
    _set_output({"id": 1})
    client = make_client()
    # Should not raise on a well-formed ok envelope.
    await client.create_comment(10, "Hello")


# --- command construction (argv + env) ------------------------------------


async def test_assigned_todos_command_and_env(
    make_client: Callable[..., BasecampClient], cli_records: Path
) -> None:
    _set_output({"todos": []})
    client = make_client(account_id="777")
    await client.assigned_todos()

    (rec,) = read_cli_records(cli_records)
    assert rec["argv"] == ["reports", "assigned", "--json"]
    assert rec["env"]["BASECAMP_NO_KEYRING"] == "1"
    assert rec["env"]["BASECAMP_NONINTERACTIVE"] == "1"
    assert rec["env"]["BASECAMP_ACCOUNT_ID"] == "777"
    assert rec["env"]["XDG_CONFIG_HOME"] is not None


async def test_create_comment_command(
    make_client: Callable[..., BasecampClient], cli_records: Path
) -> None:
    _set_output({"id": 1})
    client = make_client()
    await client.create_comment(42, "Привіт")

    (rec,) = read_cli_records(cli_records)
    assert rec["argv"] == ["comments", "create", "42", "Привіт", "--json"]


async def test_account_id_omitted_when_unset(
    make_client: Callable[..., BasecampClient], cli_records: Path
) -> None:
    _set_output({"authenticated": True})
    client = make_client()  # no account_id
    await client.auth_status()

    (rec,) = read_cli_records(cli_records)
    assert rec["env"]["BASECAMP_ACCOUNT_ID"] is None


# --- error envelope surfacing --------------------------------------------


async def test_error_envelope_on_stdout_is_surfaced(
    make_client: Callable[..., BasecampClient],
) -> None:
    # The CLI writes its error envelope to stdout and exits non-zero.
    os.environ["FAKE_STDOUT"] = json.dumps(
        {"ok": False, "error": "account is required", "hint": "set BASECAMP_ACCOUNT_ID"}
    )
    os.environ["FAKE_EXIT"] = "1"
    client = make_client()
    with pytest.raises(BasecampError, match="account is required.*set BASECAMP_ACCOUNT_ID"):
        await client.assigned_todos()


async def test_non_dict_envelope_raises(
    make_client: Callable[..., BasecampClient],
) -> None:
    os.environ["FAKE_STDOUT"] = "[1, 2, 3]"
    client = make_client()
    with pytest.raises(BasecampError, match="Unexpected JSON shape"):
        await client.assigned_todos()


# --- account resolution ---------------------------------------------------


async def test_is_authenticated_false_envelope(
    make_client: Callable[..., BasecampClient],
) -> None:
    _set_output({"authenticated": False})
    client = make_client()
    assert await client.is_authenticated() is False


async def test_ensure_account_uses_configured_id(
    make_client: Callable[..., BasecampClient], cli_records: Path
) -> None:
    client = make_client(account_id="555")
    assert await client.ensure_account() == "555"
    # No `accounts list` call needed when the id is already known.
    assert read_cli_records(cli_records) == []


async def test_ensure_account_autodetects_single(
    make_client: Callable[..., BasecampClient],
) -> None:
    _set_output([{"id": 42, "name": "Acme"}])
    client = make_client()
    assert await client.ensure_account() == "42"
    assert client.account_id == "42"


async def test_ensure_account_zero_raises(
    make_client: Callable[..., BasecampClient],
) -> None:
    _set_output([])
    client = make_client()
    with pytest.raises(BasecampError, match="No Basecamp accounts"):
        await client.ensure_account()


async def test_ensure_account_multiple_raises(
    make_client: Callable[..., BasecampClient],
) -> None:
    _set_output([{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])
    client = make_client()
    with pytest.raises(BasecampError, match="Multiple Basecamp accounts"):
        await client.ensure_account()
