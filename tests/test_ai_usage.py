"""Model pricing and spend reporting.

Two things matter here. Prices must be replaceable without a code change,
because they change more often than this ships. And a call whose model has no
published price must be reported as unknown, never rolled into a total as
zero -- a cheap-looking number nobody measured is worse than an honest gap.
"""
from __future__ import annotations

import json

import pytest

from makma.ai import pricing


@pytest.fixture(autouse=True)
def fresh_table():
    pricing.reset()
    yield
    pricing.reset()


def set_override(monkeypatch, value: str) -> None:
    monkeypatch.setattr(pricing.settings, "model_pricing", value)
    pricing.reset()


# --- the price table -------------------------------------------------------


def test_a_dated_model_id_inherits_its_family_price():
    cost, known = pricing.estimate("claude-sonnet-4-5-20250929", 1_000_000, 0)
    assert known
    assert cost == pytest.approx(3.0)


def test_the_longest_matching_prefix_wins():
    """'gpt-4o-mini' must not be priced as 'gpt-4o'."""
    mini, _ = pricing.estimate("gpt-4o-mini", 1_000_000, 0)
    full, _ = pricing.estimate("gpt-4o", 1_000_000, 0)
    assert mini < full


def test_input_and_output_are_priced_separately():
    price = pricing.price_for("claude-sonnet-4-5")
    assert price.tokens_out > price.tokens_in
    cost, _ = pricing.estimate("claude-sonnet-4-5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(price.tokens_in + price.tokens_out)


def test_an_unknown_model_is_unknown_not_free():
    cost, known = pricing.estimate("some-local-llama", 1_000_000, 1_000_000)
    assert known is False
    assert cost == 0.0, "an unpriced call must not fabricate a figure"


def test_cache_reads_are_never_free_without_a_published_rate(monkeypatch):
    """A model priced without a cache rate falls back to full input price."""
    set_override(monkeypatch, json.dumps({"nocache": {"in": 10.0, "out": 30.0}}))
    cost, _ = pricing.estimate("nocache", 1_000_000, 0, cached_in=1_000_000)
    assert cost == pytest.approx(10.0)


def test_a_published_cache_rate_is_used(monkeypatch):
    set_override(monkeypatch, json.dumps(
        {"cached": {"in": 10.0, "out": 30.0, "cache_read": 1.0}}))
    cost, _ = pricing.estimate("cached", 1_000_000, 0, cached_in=1_000_000)
    assert cost == pytest.approx(1.0)


# --- overrides -------------------------------------------------------------


def test_inline_json_overrides_a_built_in_price(monkeypatch):
    set_override(monkeypatch, json.dumps(
        {"claude-sonnet-4-5": {"in": 99.0, "out": 99.0}}))
    cost, known = pricing.estimate("claude-sonnet-4-5", 1_000_000, 0)
    assert known and cost == pytest.approx(99.0)


def test_a_json_file_overrides_a_built_in_price(monkeypatch, tmp_path):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"my-model": {"in": 1.0, "out": 2.0}}))
    set_override(monkeypatch, str(path))
    cost, known = pricing.estimate("my-model", 1_000_000, 1_000_000)
    assert known and cost == pytest.approx(3.0)


def test_an_override_adds_models_without_removing_the_defaults(monkeypatch):
    set_override(monkeypatch, json.dumps({"my-model": {"in": 1.0, "out": 2.0}}))
    assert pricing.price_for("my-model") is not None
    assert pricing.price_for("claude-sonnet-4-5") is not None


@pytest.mark.parametrize("bad", [
    "{ not json",
    "/nonexistent/path/prices.json",
    json.dumps({"broken": {"out": 1.0}}),        # missing "in"
    json.dumps({"broken": "cheap"}),             # not an object
    json.dumps({"broken": {"in": "free", "out": 1.0}}),
])
def test_an_unusable_override_falls_back_rather_than_crashing(monkeypatch, bad):
    set_override(monkeypatch, bad)
    cost, known = pricing.estimate("claude-sonnet-4-5", 1_000_000, 0)
    assert known and cost == pytest.approx(3.0)
    assert pricing.price_for("broken") is None


# --- the usage report ------------------------------------------------------


def record(db, project_id: str, **kw) -> None:
    from makma.models import ModelUsage

    defaults = dict(project_id=project_id, capability="reason",
                    provider="anthropic", model="claude-sonnet-4-5",
                    tokens_in=1000, tokens_out=500, cost_usd=0.01,
                    cost_known=True, ok=True)
    db.add(ModelUsage(**{**defaults, **kw}))
    db.commit()


@pytest.fixture
def project_with_usage(client):
    from makma.db import SessionLocal

    project = client.post("/api/projects", json={
        "name": "Spend", "width": 320, "height": 180, "fps": 25}).json()
    db = SessionLocal()
    try:
        record(db, project["id"], capability="reason", cost_usd=0.02)
        record(db, project["id"], capability="reason", cost_usd=0.03)
        record(db, project["id"], capability="vision", cost_usd=0.01,
               model="claude-haiku-4-5")
        record(db, project["id"], capability="reason", cost_usd=0.0,
               cost_known=False, model="mystery-model")
        record(db, project["id"], capability="reason", cost_usd=0.005, ok=False)
    finally:
        db.close()
    return project["id"]


def test_the_report_totals_calls_and_tokens(client, project_with_usage):
    report = client.get(f"/api/projects/{project_with_usage}/ai/usage").json()
    assert report["calls"] == 5
    assert report["failed_calls"] == 1
    assert report["tokens_in"] == 5000
    assert report["tokens_out"] == 2500


def test_an_unpriced_call_makes_the_total_a_lower_bound(client, project_with_usage):
    report = client.get(f"/api/projects/{project_with_usage}/ai/usage").json()
    assert report["unpriced_calls"] == 1
    assert report["cost_complete"] is False, (
        "a total that hides an unpriced call reads as the whole bill"
    )
    assert report["cost_usd"] == pytest.approx(0.065)


def test_the_report_breaks_spend_down_by_capability(client, project_with_usage):
    report = client.get(f"/api/projects/{project_with_usage}/ai/usage").json()
    by_capability = {line["key"]: line for line in report["by_capability"]}
    assert by_capability["reason"]["calls"] == 4
    assert by_capability["vision"]["calls"] == 1
    assert by_capability["reason"]["unpriced_calls"] == 1


def test_the_report_breaks_spend_down_by_model(client, project_with_usage):
    report = client.get(f"/api/projects/{project_with_usage}/ai/usage").json()
    models = {line["key"] for line in report["by_model"]}
    assert models == {"claude-sonnet-4-5", "claude-haiku-4-5", "mystery-model"}


def test_the_most_expensive_line_comes_first(client, project_with_usage):
    report = client.get(f"/api/projects/{project_with_usage}/ai/usage").json()
    costs = [line["cost_usd"] for line in report["by_capability"]]
    assert costs == sorted(costs, reverse=True)


def test_a_project_sees_only_its_own_spend(client, project_with_usage):
    other = client.post("/api/projects", json={
        "name": "Someone else", "width": 320, "height": 180, "fps": 25}).json()
    report = client.get(f"/api/projects/{other['id']}/ai/usage").json()
    assert report["calls"] == 0
    assert report["cost_usd"] == 0.0
    assert report["cost_complete"] is True


def test_a_project_with_no_calls_reports_zero_not_an_error(client):
    project = client.post("/api/projects", json={
        "name": "Quiet", "width": 320, "height": 180, "fps": 25}).json()
    resp = client.get(f"/api/projects/{project['id']}/ai/usage")
    assert resp.status_code == 200
    assert resp.json()["calls"] == 0


# --- schema reconciliation -------------------------------------------------


def test_a_column_added_to_a_model_reaches_an_existing_database(tmp_path):
    """Pulling a change must not leave a dev database throwing on every query."""
    import sqlalchemy as sa

    path = tmp_path / "old.db"
    engine = sa.create_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE widgets (id TEXT PRIMARY KEY, name TEXT)"))

    from makma import db as db_module

    metadata = sa.MetaData()
    sa.Table(
        "widgets", metadata,
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(60)),
        sa.Column("added_later", sa.Boolean, default=True),
    )

    class FakeBase:
        pass

    FakeBase.metadata = metadata

    monkey = pytest.MonkeyPatch()
    monkey.setattr(db_module, "engine", engine)
    monkey.setattr(db_module, "Base", FakeBase)
    try:
        db_module._add_missing_columns()
    finally:
        monkey.undo()

    with engine.connect() as connection:
        columns = {row[1] for row in
                   connection.execute(sa.text("PRAGMA table_info(widgets)"))}
    assert "added_later" in columns


def test_a_column_that_cannot_be_added_safely_is_left_alone(tmp_path, caplog):
    """SQLite cannot add NOT NULL without a default, and guessing invents data."""
    import logging

    import sqlalchemy as sa

    path = tmp_path / "strict.db"
    engine = sa.create_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE rows_ (id TEXT PRIMARY KEY)"))

    from makma import db as db_module

    metadata = sa.MetaData()
    sa.Table(
        "rows_", metadata,
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("required", sa.String(40), nullable=False),
    )

    class FakeBase:
        pass

    FakeBase.metadata = metadata

    monkey = pytest.MonkeyPatch()
    monkey.setattr(db_module, "engine", engine)
    monkey.setattr(db_module, "Base", FakeBase)
    try:
        with caplog.at_level(logging.WARNING, logger="makma.db"):
            db_module._add_missing_columns()
    finally:
        monkey.undo()

    with engine.connect() as connection:
        columns = {row[1] for row in
                   connection.execute(sa.text("PRAGMA table_info(rows_)"))}
    assert "required" not in columns
    assert "real migration" in caplog.text
