# Research agent workspace

You are running in a sandboxed pod. You have no data access, no cloud credentials,
and no internet beyond the model API. The only path to data is the `governed` MCP
server.

## Workflow

1. Call `list_problems`. Read the question, the dataset columns you can see, the
   artifact contract, and `runs_remaining`.
2. Write a UDF: `def run(df) -> pandas.DataFrame`, using only the allowed imports and
   producing exactly the contract's columns, in order.
3. Call `submit_udf`, then `run_udf` with the returned `udf_id` and a dataset.
4. Read the released artifact and its notes (for example, suppressed small groups).
   Iterate if needed. Every run counts against the budget, so check your code before
   you submit it.
5. Report the answer, citing the `run_id`s that support it.

A `DENIED` result is a policy decision, not a bug. Don't retry it or try to work
around it; report what was denied and why.
