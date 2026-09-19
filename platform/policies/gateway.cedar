// Governed MCP gateway policies: the local stand-in for AgentCore Gateway + Cedar.
//
// Cedar is default-deny: a tool call that no policy permits is refused, and any
// matching forbid beats every permit. Every policy carries an @id so the audit log
// names the rule that decided each call.

@id("discover-problems")
permit (principal is Agent, action == Action::"list_problems", resource);

@id("read-own-runs")
permit (principal is Agent, action == Action::"get_run", resource is Run)
when { resource.owner == principal };

@id("submit-udf-enrolled-team")
permit (principal is Agent, action == Action::"submit_udf", resource is Problem)
when { resource.teams.contains(principal.team) && context.code_bytes <= 20000 };

@id("run-udf-within-contract")
permit (principal is Agent, action == Action::"run_udf", resource is Dataset)
when {
  context.problem_teams.contains(principal.team) &&
  resource.problems.contains(context.problem) &&
  principal.clearances.contains(resource.sensitivity) &&
  context.runs_used < context.max_runs
};

// Hard guardrail: even if a grant or problem is misconfigured to include a PII
// dataset, no agent run can read it.
@id("never-raw-pii")
forbid (principal, action == Action::"run_udf", resource is Dataset)
when { resource.sensitivity == "pii" };
