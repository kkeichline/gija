"""Offline checks for the governed layer: Cedar decisions, snapshots, and the release rule.

Run:  make test
"""

import os
import sys
import tempfile
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1] / "platform"
os.environ.setdefault("PLATFORM_DIR", str(PLATFORM))
os.environ.setdefault("RUNS_DIR", tempfile.mkdtemp())
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, str(PLATFORM))

import duckdb  # noqa: E402
import pytest  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from gateway import app, lake, release  # noqa: E402

CONTRACT = app.CONTRACTS["claims_cost_drivers"]


def run_ctx(runs_used=0):
    return {"problem": "claims_cost_drivers", "problem_teams": ["research"], "runs_used": runs_used, "max_runs": 5}


def run_entity(owner):
    return {"uid": {"type": "Run", "id": "run-1"}, "attrs": {"owner": {"__entity": {"type": "Agent", "id": owner}}},
            "parents": []}


def test_run_on_internal_dataset_is_allowed():
    app.authorize("research-bot", "s", "run_udf", "Dataset", "claims_synthetic", run_ctx())


def test_pii_is_forbidden_even_though_a_grant_exists():
    with pytest.raises(ToolError, match="never-raw-pii"):
        app.authorize("research-bot", "s", "run_udf", "Dataset", "members_pii", run_ctx())


def test_run_budget_is_enforced():
    with pytest.raises(ToolError, match="no policy permits"):
        app.authorize("research-bot", "s", "run_udf", "Dataset", "claims_synthetic", run_ctx(runs_used=5))


def test_oversized_udf_is_denied():
    app.authorize("research-bot", "s", "submit_udf", "Problem", "claims_cost_drivers", {"code_bytes": 500})
    with pytest.raises(ToolError, match="DENIED"):
        app.authorize("research-bot", "s", "submit_udf", "Problem", "claims_cost_drivers", {"code_bytes": 30000})


def test_unknown_agent_is_denied():
    with pytest.raises(ToolError, match="DENIED"):
        app.authorize("stranger", "s", "run_udf", "Dataset", "claims_synthetic", run_ctx())


def test_ids_cannot_inject_into_cedar_requests():
    with pytest.raises(ToolError, match="malformed"):
        app.authorize("research-bot", "s", "run_udf", "Dataset", 'claims" || true', run_ctx())


def test_only_the_owner_can_read_a_run():
    app.authorize("research-bot", "s", "get_run", "Run", "run-1", extra_entities=[run_entity("research-bot")])
    with pytest.raises(ToolError, match="DENIED"):
        app.authorize("research-bot", "s", "get_run", "Run", "run-1", extra_entities=[run_entity("someone-else")])


def test_snapshot_applies_column_and_row_filters(tmp_path):
    lake.generate(tmp_path / "lake", members=2000)
    dest = tmp_path / "snap.parquet"
    rows = lake.snapshot(app.CATALOG, "claims_synthetic", "research", tmp_path / "lake", dest)
    cols = [r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM '{dest}'").fetchall()]
    assert cols == app.CATALOG["datasets"]["claims_synthetic"]["grants"]["research"]["columns"]
    assert duckdb.sql(f"SELECT count(*) FROM '{dest}' WHERE region = 'EU'").fetchone()[0] == 0
    assert 0 < rows < 2000


HEADER = "age_band,plan,chronic_conditions,n_members,avg_claim_cost\n"


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory):
    d = tmp_path_factory.mktemp("lake")
    lake.generate(d / "lake", members=20000)
    dest = d / "snap.parquet"
    lake.snapshot(app.CATALOG, "claims_synthetic", "research", d / "lake", dest)
    return dest


def truth(snapshot):
    """The honest answer, computed the way a correct UDF would."""
    return duckdb.sql(f"""SELECT age_band, plan, chronic_conditions, count(*) n, round(avg(claim_cost), 4) a
                          FROM '{snapshot}' GROUP BY ALL ORDER BY a DESC""").fetchall()


def write(tmp_path, rows):
    (tmp_path / "result.csv").write_text(HEADER + "".join(",".join(map(str, r)) + "\n" for r in rows))


def test_honest_artifact_is_released_and_small_groups_suppressed(tmp_path, snapshot):
    rows = truth(snapshot)
    write(tmp_path, rows)
    rel = release.apply(CONTRACT, tmp_path, snapshot)
    small = sum(1 for r in rows if r[3] < 25)
    assert rel.released and rel.suppressed == small > 0 and rel.rows == len(rows) - small


def test_fabricated_counts_are_rejected(tmp_path, snapshot):
    # A UDF that writes the same count on every row to pass small-group suppression.
    write(tmp_path, [(*r[:3], 100, r[4]) for r in truth(snapshot)])
    rel = release.apply(CONTRACT, tmp_path, snapshot)
    assert not rel.released and "true size" in rel.notes[0]


def test_row_level_values_passed_off_as_averages_are_rejected(tmp_path, snapshot):
    one_member = duckdb.sql(f"SELECT claim_cost FROM '{snapshot}' LIMIT 1").fetchone()[0]
    rows = truth(snapshot)
    write(tmp_path, [(*rows[0][:4], one_member), *rows[1:]])  # true count, but one person's cost
    rel = release.apply(CONTRACT, tmp_path, snapshot)
    assert not rel.released and "recomputation" in rel.notes[0]


def test_invented_groups_are_rejected(tmp_path, snapshot):
    write(tmp_path, [("99+", "platinum", 9, 500, 1.0), *truth(snapshot)])
    rel = release.apply(CONTRACT, tmp_path, snapshot)
    assert not rel.released and "do not exist" in rel.notes[0]


def test_checks_flag_suspicious_code_and_uniform_output():
    from gateway import checks
    code = "import pandas as pd\ndef run(df):\n    x = __import__('os')\n    return df\n"
    csv_text = HEADER + "".join(f"a,b,{i},100,{i}.0\n" for i in range(6))
    flags = checks.run(CONTRACT, code, csv_text)
    assert "calls __import__()" in flags and "every row has the same n_members" in flags


def test_release_rejections_do_not_echo_output(tmp_path):
    (tmp_path / "result.csv").write_text("Member 42 900-42-0042,x,y\n")
    rel = release.apply(CONTRACT, tmp_path)
    assert not rel.released
    assert "900-42" not in " ".join(rel.notes)


def test_briefing_states_the_rules_the_release_rule_enforces():
    rules = " ".join(app.how_output_is_checked(CONTRACT))
    assert "recomputes" in rules and "n_members" in rules and "suppressed" in rules
    assert "reviewed for hard-coded values" in rules


def test_udf_template_matches_the_contract():
    template = app.udf_template(CONTRACT)
    assert "groupby" in template and "age_band" in template and "n_members=" in template
    assert "avg_claim_cost=(\"claim_cost\", \"mean\")" in template


def test_sampled_snapshot_is_smaller(tmp_path, snapshot):
    lake.generate(tmp_path / "lake", members=3000)
    dest = tmp_path / "sample.parquet"
    rows = lake.snapshot(app.CATALOG, "claims_synthetic", "research", tmp_path / "lake", dest, 500)
    assert rows == 500
