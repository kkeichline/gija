"""Lake stand-in: synthetic source tables plus Lake Formation-style filtered snapshots.

In AWS the snapshot step would be an Athena CTAS or Glue job running under a
Lake Formation-governed role, writing to a per-run S3 prefix. A network-isolated
SageMaker Processing job cannot call Lake Formation itself, so the filtering has
to happen before the job starts, the same as it does here.
"""

import sys
from pathlib import Path

import duckdb


def generate(lake_dir: Path, members: int = 20000) -> None:
    lake_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads = 1")  # keeps setseed() deterministic
    con.execute("SELECT setseed(0.42)")
    con.execute(f"""
        CREATE TABLE members AS
        SELECT i AS member_id,
               'Member ' || i AS member_name,
               printf('900-%02d-%04d', i % 100, i % 10000) AS ssn,  -- 900-series: never a real SSN
               ['18-29', '30-44', '45-64', '65+'][1 + floor(random() * 4)::INT] AS age_band,
               ['US-East', 'US-West', 'US-Central', 'EU'][1 + floor(random() * 4)::INT] AS region,
               ['bronze', 'silver', 'gold'][1 + floor(random() * 3)::INT] AS plan,
               floor(random() * random() * 5)::INT AS chronic_conditions,
               random() AS noise
        FROM range({members}) t(i)""")
    con.execute(f"""
        COPY (
            SELECT member_id, member_name, age_band, region, plan, chronic_conditions,
                   round(1200
                         * CASE age_band WHEN '18-29' THEN 0.7 WHEN '30-44' THEN 1.0
                                         WHEN '45-64' THEN 1.6 ELSE 2.4 END
                         * CASE plan WHEN 'bronze' THEN 0.8 WHEN 'silver' THEN 1.0 ELSE 1.3 END
                         * (1 + 0.8 * chronic_conditions)
                         * exp(noise * 1.5), 2) AS claim_cost
            FROM members
        ) TO '{lake_dir}/claims_synthetic.parquet' (FORMAT parquet)""")
    con.execute(f"""
        COPY (SELECT member_id, member_name, ssn FROM members)
        TO '{lake_dir}/members_pii.parquet' (FORMAT parquet)""")


def snapshot(catalog: dict, dataset: str, team: str, lake_dir: Path, dest: Path) -> int:
    """Write the team's column- and row-filtered view of `dataset` to `dest`; return row count."""
    grant = catalog["datasets"][dataset]["grants"].get(team)
    if grant is None:
        raise PermissionError(f"team {team!r} has no grant on {dataset!r}")
    cols = ", ".join(f'"{c}"' for c in grant["columns"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"""
        COPY (SELECT {cols} FROM read_parquet('{lake_dir / dataset}.parquet')
              WHERE {grant['row_filter']})
        TO '{dest}' (FORMAT parquet)""")
    return con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]


if __name__ == "__main__":
    generate(Path(sys.argv[1]))
