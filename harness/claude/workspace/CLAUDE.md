# Research agent workspace

You are running in a sandboxed pod. You have no data access, no cloud credentials,
and no internet beyond the model API. The only path to data is the `governed` MCP
server, and everything you do there is logged.

## Workflow

1. `list_problems`. Read three things carefully: the **artifact contract**, the
   **how_your_output_is_checked** rules, and the **udf_template**. They tell you
   exactly what a valid answer looks like.
2. Write `def run(df) -> pandas.DataFrame` using only the allowed imports. Produce the
   contract's columns, in order, computed from `df`. `df` has only the columns the
   contract lists.
3. `submit_udf`, then **`validate_udf`**. Validation runs your UDF on a small sample,
   reports contract problems, and **does not use your run budget**. Fix whatever it
   reports and validate again. It is always cheaper than a real run.
4. `run_udf` once validation is clean. Each real run spends one of `runs_remaining`.
5. Read the result and its notes. Suppressed small groups are normal and expected.
6. Answer the question, citing the `run_id`.

## What gets your work rejected

- Writing counts or averages instead of computing them. The platform recomputes group
  sizes and any metric the contract lists under `verify` from the snapshot itself, and
  rejects the whole artifact when your numbers disagree.
- Inventing groups that don't exist in the data.
- Columns that don't exactly match the contract, or the wrong order.
- Reaching for files, the network, the environment, or imports outside the allowlist.
  There is no network in the job sandbox; such attempts only waste a run.

Released results are also reviewed for hard-coded values, for gaming these rules, and
for whether they answer the question that was asked. Compute honestly from the data:
it is both the shortest path and the only one that passes.

A `DENIED` result is a policy decision, not a bug. Don't retry it or try to work
around it; report what was denied and why.
