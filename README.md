# Agent-Skills

Skills and tools for [Claude Code](https://claude.com/claude-code) that I use myself. Only the
ones that are not tied to my personal files and that someone else can actually use.

Every folder is self-contained: copy it into `~/.claude/skills/` and the agent picks the skill
up. The scripts inside also run straight from the command line, with no agent involved.

| Skill | What it does |
|---|---|
| [skill-lint](skill-lint/) | Checks the integrity of skills: broken links into `references/`, links to skills that no longer exist, orphan files, broken frontmatter, Agent Skills spec violations, an oversized `SKILL.md`, and code left in the prose instead of `scripts/`. Wires onto a hook |
| [kuper-prices](kuper-prices/) | Collects grocery prices from Kuper across every store at once - no browser, no account. Filters junk out of the results, computes the price per kg/l/unit, returns a sorted table ready to use |

Documentation is in English; `kuper-prices` works over Russian retail, so its own examples are
in Russian.

## Why these are separate scripts

An agent has no business paging through product cards or working out a price per kilogram in its
head - it burns time there and makes mistakes. Let the script fetch and sort the data, and let
the agent do what it is better at: building a decision out of it.

Hence the two rules the scripts in this repository follow:

* **fail loudly.** A silent empty result is more dangerous than an error - it looks like solid
  data. A block, a network failure and "nothing found" have to be distinguishable;
* **never lose what already worked.** A failed run does not overwrite a working configuration.
