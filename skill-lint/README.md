# skill-lint

An integrity checker for [Claude Code](https://claude.com/claude-code) skills: broken links,
orphan files, spec violations and bloated `SKILL.md`. No dependencies - Python standard
library only.

## Why

A skill loads in two stages: `SKILL.md` first, then its references through the links inside
it. When a link is broken, **nothing crashes and nothing complains.** The agent silently
skips the step, and the only way to notice is that the work came out worse than usual, with
no explanation.

Refactoring is where this happens: you move a chunk into `references/`, rename a file,
rename a skill - and half of the instructions quietly stop applying. The script turns that
silent breakage into a loud one.

## What it catches

| | Check | Level |
|---|---|---|
| 1 | **Broken routing** - `SKILL.md` links to a `references/…` file that is not there | ⛔ error |
| 2 | **Broken sibling link** - inside `references/` the short form is used: `[text](neighbour.md)` | ⛔ error |
| 3 | **Link to a skill that does not exist** - `~/.claude/skills/<name>/…` after a rename | ⛔ error |
| 4 | **Link to a missing file in another skill** - the skill is there, the file is not | ⛔ error |
| 5 | **Broken frontmatter** - without `name`/`description` the skill never activates | ⛔ error |
| 6 | **`name` does not match the folder** - the spec requires them to match | ⛔ error |
| 7 | **Duplicate `name` across skills** - one shadows the other, unpredictably | ⛔ error |
| 8 | **No body** - frontmatter present, instructions missing | ⛔ error |
| 9 | **Broken outbound path** - a `~/…` path in backticks that no longer exists | ⛔ error |
| 10 | **Broken section pointer** - `references/foo.md` → `"Section"` after a heading rename | ⚠️ warning |
| 11 | **Orphan** - a file in `references/` that nothing links to | ⚠️ warning |
| 12 | **Oversized `SKILL.md`** - loaded whole on every activation | ⚠️ warning |
| 13 | **Oversized reference** - opened whole once it is reached | ⚠️ warning |
| 14 | **Spec violations** - `description` over 1024, bad `name` format, long `compatibility` | ⚠️ warning |
| 15 | **Too short a `description`** - no trigger conditions in it at all | ⚠️ warning |
| 16 | **Unknown frontmatter key** - `descriptoin:` is not a syntax error, the field just vanishes | ⚠️ warning |
| 17 | **Code as prose** - a long executable block that should be a file in `scripts/` | ⚠️ warning |

Checks 1 and 2 are separate on purpose: links are written in different forms, and the regex
that sees `references/foo.md` does not see `[text](neighbour.md)` in a neighbouring file.

Paths pointing into a folder the skill does not have at all (`assets/readme/hero.svg` in a
skill with no `assets/`) count as examples rather than routes - a warning, not an error.
Otherwise the hook would fail on every skill that shows example paths from another repo.

### Two checks worth explaining

**The section pointer** (10) is the one an ordinary link checker cannot do. The file exists,
so the link is fine; it is the *heading* inside it that was renamed. The agent opens the
reference and does not find what it came for. Only the explicit form is matched: a file, then
`→`, `->` or the word *section*, then the name in quotes - `"x"`, `“x”` or `«x»`. A loose
paraphrase is deliberately not matched: guessing at those produces false positives on
ordinary quotations, and a linter that lies stops being read.

**Code as prose** (17) is a design check, not a syntax one. A step that is always performed
the same way belongs in `scripts/`: code left in the instructions is retyped by the model
every run - probabilistically, and for tokens - cannot be executed, and cannot be fixed once
and for all. Bad/good example pairs are exempt (they are teaching material, and in a file
they would be dead), recognised by a marker line such as `# WRONG` / `# GOOD`.

Both the example markers and the *section* keyword are English. For skills written in
another language, add your words to `EXAMPLE_RE` and `SECPTR_RE` at the top of the script -
that is the only language-specific spot in the file.

## Installing and running

```bash
python check_skills.py                       # every skill
python check_skills.py my-skill other-skill  # these only
python check_skills.py --skills-dir ~/.claude/skills
python check_skills.py --quiet               # stay silent when clean
```

The skills folder is resolved in this order: the `--skills-dir` flag → the `CLAUDE_SKILLS_DIR`
variable → the script's own folder, if skills live in it → `~/.claude/skills`.

Sample output:

```
⛔ broken  (485 B)
     ⛔ `name: wrong-name` does not match the folder name `broken` - the spec requires them to match; rename one of the two
     ⛔ broken sibling link: nosuch.md (from references/present.md)
     ⛔ link to a missing file: references/missing.md
     ⛔ link to a skill that does not exist: nosuchskill (in references/x.md)
     ⚠️  pointer to a missing section: references/present.md → "Gone Heading" (from SKILL.md)
     ⚠️  orphan (nothing links to it): references/orphan.md
✅ good  (261 B)

skills: 4 · errors: 4
```

Exit codes: `0` - clean, `1` - errors. Warnings never fail the run.

## Wiring it to a hook

The check is most useful when it runs by itself, right after a skill was edited, while the
context is still fresh. It comes in two parts, and both are needed.

`PostToolUse` only **records** which skills were touched this turn:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python ~/.claude/skills/skill-lint/scripts/check_skills.py --mark",
            "timeout": 20,
            "statusMessage": "Marking edited skills"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python ~/.claude/skills/skill-lint/scripts/check_skills.py --stop",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

`Stop` then runs the full check and **blocks the stop** until the errors are fixed.

Four details that are easy to lose a day to:

* **Checking at `Stop`, not at `PostToolUse`.** Mid-turn the skill is still half-written: a
  fresh `SKILL.md` would be flagged for `references/` files its author is about to create
  with the very next command. Marking is cheap and happens immediately; judging waits for
  the end of the turn.
* **`--mark` reads JSON from stdin and stays silent for edits outside the skills folder.**
  Without that the hook would fire on every `Write`/`Edit` in every project.
* **Match `Bash` too, not just `Write|Edit`.** Skills are edited through heredoc, `sed`, `mv`
  and `rm` just as often as through an editor, and those only ever appear in `command`.
* **In hook mode the exit code is `2`, not `1`.** Only a 2 is passed back to the model; with
  a 1 the message settles in a log and nobody reads it.

In the `Stop` run, warnings are printed only for the skills edited in that turn - the author
has already seen the rest and is not going to fix them now. Errors are always printed in full.

## Tuning

At the top of the script:

| Constant | Default | Meaning |
|---|---|---|
| `BUDGET` | 15 000 | soft ceiling for `SKILL.md` in bytes (~5-6k tokens per activation) |
| `REF_BUDGET` | 25 000 | same for one file in `references/`, which is opened whole |
| `DESC_MIN` | 60 | below this a `description` cannot hold trigger conditions |
| `SCRIPT_LINES` | 15 | code lines in the prose above which the block has to become a file |
| `SUBDIRS` | references, assets, scripts, templates | which subfolders count as part of a skill |
| `KNOWN_KEYS` | see the file | frontmatter keys that mean something; the rest are treated as typos |
