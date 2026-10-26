from datetime import date, datetime, timezone
from unittest.mock import Mock, call

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.features.todos.models import TodoRecord
from app.features.todos import repository as repository_module
from app.features.todos.repository import TodoRepository
from app.features.todos.schemas import Priority, TodoCreate, TodoUpdate


def test_list_maps_models_and_orders_tasks_newest_first(model_factory, todo_values, todo):
    session = Mock(spec=Session)
    session.scalars.return_value = [model_factory(todo_values)]
    assert TodoRepository(session).list() == [todo]
    statement = session.scalars.call_args.args[0]
    expected = select(TodoRecord).order_by(TodoRecord.created_at.desc(), TodoRecord.id.desc())
    assert statement.compare(expected)
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "ORDER BY todos.created_at DESC, todos.id DESC" in compiled


def test_get_returns_none_for_missing_task():
    session = Mock(spec=Session)
    session.get.return_value = None
    assert TodoRepository(session).get(404) is None
    session.get.assert_called_once_with(TodoRecord, 404)


def test_create_persists_defaults_and_returns_the_refreshed_record(monkeypatch):
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    clock = Mock()
    clock.now.return_value = now
    monkeypatch.setattr(repository_module, "datetime", clock)
    session = Mock(spec=Session)
    session.refresh.side_effect = lambda record: setattr(record, "id", 12)

    created = TodoRepository(session).create(TodoCreate(title="  Ship the release  "))

    record = session.add.call_args.args[0]
    assert isinstance(record, TodoRecord)
    assert created.id == record.id == 12
    assert created.title == record.title == "Ship the release"
    assert created.description == "" and created.priority == Priority.medium
    assert created.completed is False and created.due_date is None
    assert created.created_at == created.updated_at == now
    clock.now.assert_called_once_with(timezone.utc)
    assert session.method_calls == [call.add(record), call.commit(), call.refresh(record)]


def test_update_changes_only_supplied_fields_including_false_and_null(
    monkeypatch, model_factory, todo_values,
):
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    clock = Mock()
    clock.now.return_value = now
    monkeypatch.setattr(repository_module, "datetime", clock)
    record = model_factory({**todo_values, "completed": True, "due_date": date(2026, 11, 1)})
    session = Mock(spec=Session)
    session.get.return_value = record

    updated = TodoRepository(session).update(
        record.id, TodoUpdate(description="", completed=False, due_date=None),
    )

    assert updated.description == record.description == ""
    assert updated.completed is record.completed is False
    assert updated.due_date is record.due_date is None
    assert updated.title == todo_values["title"]
    assert updated.priority == todo_values["priority"]
    assert updated.created_at == todo_values["created_at"]
    assert updated.updated_at == record.updated_at == now
    clock.now.assert_called_once_with(timezone.utc)
    assert session.method_calls == [call.get(TodoRecord, record.id), call.commit(), call.refresh(record)]


def test_update_missing_task_does_not_commit_or_refresh():
    session = Mock(spec=Session)
    session.get.return_value = None

    assert TodoRepository(session).update(404, TodoUpdate(title="New title")) is None
    assert session.method_calls == [call.get(TodoRecord, 404)]


def test_delete_commits_existing_task_but_does_not_write_for_missing_task(model_factory, todo_values):
    record = model_factory(todo_values)
    session = Mock(spec=Session)
    session.get.side_effect = [record, None]
    repository = TodoRepository(session)

    assert repository.delete(record.id) is True
    assert repository.delete(404) is False
    assert session.method_calls == [
        call.get(TodoRecord, record.id), call.delete(record), call.commit(), call.get(TodoRecord, 404),
    ]
