# Agent-Skills

An index of the skills and tools I build for [Claude Code](https://claude.com/claude-code).
Each one lives in its own repository; this page is only the map.

Listed here are the ones that are not tied to my personal files and that someone else can
actually use. Every repository is self-contained: copy the folder into `~/.claude/skills/` and
the agent picks the skill up. The scripts also run straight from the command line, with no
agent involved.

## Skills

| Repository | What it does |
|---|---|
| [skill-quality-suite](https://github.com/letsloose501/skill-quality-suite) | A quality suite for Agent Skills: eight checks, one command each. Broken links and orphans, spec conformance, whether the description says *when* to fire, portability across ten agent harnesses, secrets and injection in a skill you installed, eval sets, publication readiness, and the repairs that have one correct answer. 70 coded rules; `explain <CODE>` for any of them. Python standard library only |

## Tools

| Repository | What it does |
|---|---|
| [kuper-prices](https://github.com/letsloose501/kuper-prices) | Collects grocery prices from Kuper across every store at once - no browser, no account. Filters junk out of the results, computes the price per kg/l/unit, returns a sorted table ready to use |

Documentation is in English. `kuper-prices` works over Russian retail, so its own examples are
in Russian.

## Why a script and not a prompt

An agent has no business paging through product cards or working out a price per kilogram in
its head - it burns time there and makes mistakes. Let the script fetch and sort the data, and
let the agent do what it is better at: building a decision out of it.

Hence the two rules every script here follows:

* **fail loudly.** A silent empty result is more dangerous than an error - it looks like solid
  data. A block, a network failure and "nothing found" have to be distinguishable;
* **never lose what already worked.** A failed run does not overwrite a working configuration.

## Elsewhere

My pet projects are listed on [my profile](https://github.com/letsloose501).
