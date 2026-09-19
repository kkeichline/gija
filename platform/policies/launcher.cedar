// Front-door policies: who may ask for research, see it, and approve it.
//
// The principal is the *person* signed in to the front door (LibreChat, Open WebUI,
// ZeroClaw, OpenClaw), vouched for by that front door's token. Default-deny, and every
// policy has an @id so the audit log names the rule that decided.

@id("discover-problems")
permit (principal is User, action == Action::"list_problems", resource);

@id("ask-enrolled-team")
permit (principal is User, action == Action::"start_research", resource is Problem)
when { resource.teams.contains(principal.team) };

@id("see-own-research")
permit (principal is User, action == Action::"research_status", resource is Research)
when { resource.requested_by == principal };

@id("approvers-see-team-research")
permit (principal is User, action == Action::"research_status", resource is Research)
when { principal.roles.contains("approver") && resource.teams.contains(principal.team) };

@id("approver-decides")
permit (principal is User, action == Action::"decide", resource is Research)
when { principal.roles.contains("approver") && resource.teams.contains(principal.team) };

// Four eyes: nobody approves their own request, even an approver.
@id("no-self-approval")
forbid (principal, action == Action::"decide", resource is Research)
when { resource.requested_by == principal };
