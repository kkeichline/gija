---
name: repo-docs
description: Writing rules for any prose committed to this repository - README.md, files under docs/, CONTRIBUTING, SECURITY, release notes, commit messages, and pull request descriptions. Use before writing or editing any of those files. Treats all repository prose as technical documentation for a first-time reader.
---

# Repository documentation style

Every reader is reading for the first time. They have no context, no history with
this project, and no interest in being persuaded. Documentation states what is
true, what to run, and what happens. Nothing else.

## Rules

1. **Plain words.** Use the simplest word that is correct. Expand every acronym on
   first use. Define a term the first time it appears, in one sentence.
2. **One idea per sentence.** Maximum 25 words. Split anything longer.
3. **Short sections.** Maximum 150 words per section. Use a table or list when
   content has more than three parallel items.
4. **Same word for the same thing, every time.** Never vary wording for style. If
   the component is called "the gateway", it is "the gateway" in every sentence.
5. **Explicit and specific.** Give exact names, paths, versions, ports, and
   commands. Write `platform/gateway/app.py`, not "the gateway code".
6. **Technically correct.** State only what is verified. Mark anything untested as
   untested. Do not describe planned work as if it exists.
7. **Objective tone.** State facts. Do not argue, persuade, sell, or justify. Do
   not tell the reader what is interesting, important, powerful, or simple.
8. **No drama.** No stories, no narrative tension, no "surprisingly", no
   exclamation marks, no emphasis for effect.
9. **Active voice, present tense.** "The gateway checks each call." Not "each call
   is checked" or "the gateway will check".
10. **One command per code block.** Show the command alone. Describe its result in
    the sentence before it, not in a comment inside it.

## Banned words and constructions

- Hype: powerful, seamless, robust, elegant, blazing, rich, cutting-edge, magic.
- Minimisers: just, simply, easy, obviously, of course, trivially, merely.
- Filler: note that, it is worth noting, in order to, basically, actually.
- Persuasion: you should, we believe, the best way, the right approach, clearly.
- Vague amounts: several, various, many, a few, some. Give the number.

## Checklist before committing prose

- [ ] A first-time reader can follow it without prior context.
- [ ] Every sentence is under 25 words and states one idea.
- [ ] Every acronym is expanded once. Every term is defined once.
- [ ] Every claim is verified, or marked as untested.
- [ ] No banned words. No arguments. No narrative.
- [ ] Commands are exact and copyable. Paths and versions are exact.
- [ ] The same component has the same name throughout.

## Example

Before:

> Because the agent runs in a hardened sandbox and simply cannot reach the network,
> even a malicious UDF is powerless - which is really the whole argument for putting
> governance below the harness rather than trusting the model.

After:

> The job pod has no network access. Domain name lookups and outbound connections
> fail. A user-defined function (UDF) cannot reach the internet, the gateway, or the
> storage service.

## Scope

Applies to: `README.md`, `docs/`, `CONTRIBUTING.md`, `SECURITY.md`, release notes,
commit messages, and pull request descriptions.

Does not apply to: chat replies, code identifiers, or test names.
