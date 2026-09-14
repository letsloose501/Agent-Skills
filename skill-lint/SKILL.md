---
name: skill-lint
description: >-
  Integrity check for Claude Code skills: broken links into references/, pointers to
  sections that no longer exist, links to skills that were renamed away, orphan files,
  broken frontmatter, Agent Skills spec violations (description length, name format,
  name not matching the folder), an oversized SKILL.md, and long code blocks left in
  the prose instead of scripts/. Trigger when the user asks to check or lint skills,
  complains that a skill "does not fire" or "only half works", renames a skill, a file
  inside one, or a heading inside a reference, moves part of a SKILL.md out into a
  reference, installs somebody else's skill and wants to be sure it is intact, or asks
  why the agent keeps skipping steps of an instruction. Also before publishing or
  committing a skill.
---

# Checking skills

```
python ~/.claude/skills/skill-lint/scripts/check_skills.py
python ~/.claude/skills/skill-lint/scripts/check_skills.py my-skill other   # these only
```

No dependencies. The script finds the skills folder on its own; override it with
`--skills-dir <path>` when needed.

## How to read the result

⛔ **Errors have to be fixed.** A broken link into `references/…` does not crash anything:
the agent silently skips the step, and from the outside it just looks like weaker work.
That is exactly why such breakage survives for months. The same goes for a `name` that
does not match the folder: the spec requires them to match, and some loaders trip on it.

⚠️ **Warnings are something to think about, not to fix mechanically:**

- *orphan* - either the file was never wired up (then link to it) or it is no longer
  needed (then delete it). An orphan is not wrong by itself;
- *oversized SKILL.md* - it is loaded whole on every activation. Move what is not always
  needed into `references/`, and keep the routing in `SKILL.md`: which reference to open
  when;
- *path with no such folder in the skill* - nearly always an example path from somebody
  else's repository, not a route of your own;
- *pointer to a missing section* - `references/foo.md` → "Section" after the heading was
  renamed. The file is still there, so an ordinary link check stays quiet while the agent
  opens the reference and does not find what it came for. It is fixed in one direction or
  the other: restore the heading, or correct the pointer - and **the section name is
  copied from the file verbatim, never from memory**;
- *`description` over 1024* - Claude Code does not enforce that limit today and the skill
  works. But the Agent Skills spec sets it, so on publication and on `skills-ref validate`
  the description is rejected. Fix before publishing, not urgently;
- *code as prose* - a long executable block sitting in the text. A step that is always
  performed the same way belongs in `scripts/`: code in prose is retyped by the model
  every time, cannot be run and cannot be fixed once. Leave the call and how to read the
  output in the text.

## Limits of the section check

Only the explicit form is caught: a file, then `→`, `->` or the word *section*, then the
name in quotes - `"like this"`, `“like this”` or `«like this»`. A loose paraphrase ("see
the part about the loop in there") is deliberately not caught: guessing at those produces
false positives on ordinary quotations, and a linter that lies stops being read.

The example markers for the *code as prose* check (`# WRONG`, `# GOOD`, `# BEFORE`, …) are
English. If your skills are written in another language, add your words to `EXAMPLE_RE` and
to the section keyword in `SECPTR_RE` - that is the only language-specific spot in the file.

## After fixing

Run it again and confirm there are zero errors. If you edited somebody else's skill, tell
the user exactly what you changed: they may have had their reasons for an odd structure.

Details, hook installation and budget tuning are in [README.md](README.md).
