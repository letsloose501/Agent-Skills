#!/usr/bin/env python3
"""
Integrity checker for Claude Code skills - backpressure for two-stage loading.

A skill loads in two stages: SKILL.md first, then its references through the links
inside it. When a link is broken nothing crashes and nothing complains: the agent
silently skips the step, and the only symptom is that the work came out worse than
usual. This script turns that silent breakage into a loud one.

Usage:  python check_skills.py                        # every skill
        python check_skills.py my-skill other-skill   # only these
        python check_skills.py --skills-dir ~/.claude/skills

The skills directory is resolved in this order: the `--skills-dir` flag → the
`CLAUDE_SKILLS_DIR` environment variable → the script's own directory, if skills
actually live in it → `~/.claude/skills`.

What it catches:
  1. Broken routing - SKILL.md points at a references/… file that is not there.
     This is the classic breakage when parts of a skill are moved out into
     references: the instruction "open X" without an X becomes a silent skip.
  2. Broken sibling links - inside references/ a neighbour is linked in the short
     markdown form, [text](neighbour.md). Check (1) cannot see those: different form.
  3. Link to a skill that does not exist - ~/.claude/skills/<name>/… after a rename.
  4. Orphans - a file in references/ that nothing links to. A pruning candidate:
     either it was never wired up, or it is no longer needed.
  5. Oversized SKILL.md - it is loaded in full on every activation (see BUDGET).
  6. Broken frontmatter - without name/description the skill never activates.
  7. Agent Skills spec violations in the frontmatter: `description` length (1024 max),
     `name` format and length, `name` not matching the folder name, `compatibility`
     length (500 max). Claude Code does not enforce these limits today, which is what
     makes the breakage quiet: it works locally and falls over when the skill is
     published or run through `skills-ref validate`. Hence a warning, not an error.
  8. Broken section pointer - `references/foo.md` → "Section" where that heading no
     longer exists in the file. The file itself is present, so check (1) stays quiet
     while the agent opens the reference and does not find what it came for. This
     breaks on a heading rename, i.e. during ordinary editing, with no files moved.
  9. Broken outbound path - a skill points at `~/Documents/…/Note.md` and the note was
     renamed. For skills that read an external knowledge base before they start
     working, this is the main source of quiet degradation: the theory is gone and the
     work goes ahead anyway. Non-ASCII names are compared normalized - a letter with a
     diacritic can be stored precomposed (NFC) or as a base letter plus a combining
     mark (NFD), and then a live file reads as a missing one.
 10. Unknown frontmatter key - a typo such as `descriptoin:` kills the skill in
     silence: no field means no description, which means the agent never calls it.
 11. Duplicate `name:` across skills - one shadows the other, and which one wins is
     not something you can predict in advance.
 12. Oversized reference - references/ are read in full once they are reached. A 30 KB
     file cancels out the whole point of two-stage loading (see REF_BUDGET).
 13. Skill with no body - frontmatter present, instructions missing: it activates and
     says nothing.
 14. Too short a `description` - two words contain no trigger conditions, and trigger
     conditions are the only thing the agent uses to decide whether to open the skill
     at all (see DESC_MIN).
 15. Code as prose - a long block in an executable language sitting in the skill text
     (see SCRIPT_LINES). A step that is always performed the same way belongs in a
     script under scripts/, not in a paragraph of instructions: code in prose is
     retyped by the model every time (probabilistically, and for tokens), cannot be
     run, and cannot be fixed once and for all. What is left for the instructions is
     when to call the script and how to read its output. Bad/good example pairs do not
     count (see EXAMPLE_RE): that code is shown, not executed, and in a file it would
     be dead.

Flags:
  --quiet   stay silent when everything is clean; print problems only
  --mark    PostToolUse hook mode: check nothing, only record which skills were
            touched during this turn. Edits outside the skills directory are
            ignored. Understands both Write/Edit (file_path) and Bash (command):
            without parsing the command the check is blind to edits made through
            heredoc, sed, mv and rm.
  --stop    Stop hook mode: if no skill was touched this turn, stay silent;
            otherwise run the full check and block the stop until the errors are
            fixed. Checking at the end of the turn is deliberate: at PostToolUse
            time the skill is still half-written, and a fresh SKILL.md would be
            flagged for references/ files its author is about to create with the
            very next command.
  --hook    deprecated synonym for --mark.

Exit codes: 0 - clean, 1 - errors (⛔), 2 - the same in hook mode (the only code
the harness passes back to the model). Warnings (⚠️) never fail the run.
"""
import glob
import json
import os
import re
import sys
import unicodedata

BUDGET = 15_000          # SKILL.md bytes - soft ceiling (~5-6k tokens per activation)
REF_BUDGET = 25_000      # reference bytes - it is opened whole, nobody splits it for you
DESC_MIN = 60            # shorter than this and a description holds no trigger conditions
SCRIPT_LINES = 15        # lines of code in prose - above this the block must become a file
SUBDIRS = ("references", "assets", "scripts", "templates")

# Languages in which a block in the text is a program, not an illustration. markdown,
# yaml, json and text are deliberately out: there a block shows the shape of a result,
# not a step to perform.
CODE_LANGS = {"python", "py", "bash", "sh", "shell", "powershell", "ps1", "pwsh"}

# Frontmatter keys that mean something: the Agent Skills spec plus what Claude Code
# understands. Anything else is almost certainly a typo - and a typo in a key is not a
# syntax error: the field simply disappears, taking its meaning with it.
KNOWN_KEYS = {
    "name", "description", "license", "compatibility", "allowed-tools",
    "metadata", "version",
    "disable-model-invocation", "model", "argument-hint", "user-invocable",
}


def resolve_skills_dir(argv):
    """Where the skills live.

    "The script's folder is the skills folder" is not a safe assumption: the script
    may sit in the skills root, inside somebody's scripts/, or anywhere else at all.
    Hence the order: explicit flag → environment variable → the script's folder, but
    only if skills really are in it → the standard location.
    """
    for i, a in enumerate(argv):
        if a == "--skills-dir" and i + 1 < len(argv):
            return os.path.abspath(os.path.expanduser(argv[i + 1]))
        if a.startswith("--skills-dir="):
            return os.path.abspath(os.path.expanduser(a.split("=", 1)[1]))
    env = os.environ.get("CLAUDE_SKILLS_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    here = os.path.dirname(os.path.abspath(__file__))
    if glob.glob(os.path.join(here, "*", "SKILL.md")):
        return here
    return os.path.expanduser(os.path.join("~", ".claude", "skills"))


SKILLS_DIR = resolve_skills_dir(sys.argv[1:])
# The "skills were touched this turn" marker: written by --mark, read and cleared by
# --stop. Without it the Stop hook would have to run after every turn in every project.
MARKER = os.path.join(SKILLS_DIR, ".check-pending")

# A link to a file inside the skill: `references/foo.md`, `scripts/bar.py`.
# The negative lookbehind cuts off the case where the same folder/file pair turns out
# to be the tail of SOMEBODY ELSE'S path: `~/tools/agent-memory/scripts/add_drawer.py`
# is a tool outside the skills tree, and checking for it inside the skill folder makes
# no sense. Without this, every link to an external script raises a false "file missing".
LINK_RE = re.compile(r"(?<![\w./\\-])(?:" + "|".join(SUBDIRS) + r")/[\w./-]+\.\w+")
# cross-skill link: .../skills/<other skill>/references/foo.md or .../SKILL.md - not our file
CROSS_RE = re.compile(
    r"skills/([\w-]+)/((?:(?:" + "|".join(SUBDIRS) + r")/[\w./-]+\.\w+)|SKILL\.md)")
# templated link to a directory: references/institutions/<slug>.md → the whole folder is in use
WILDCARD_RE = re.compile(r"((?:" + "|".join(SUBDIRS) + r")/[\w./-]*?)/?<[^>]+>\.\w+")
# markdown link to a neighbour in the same folder: [text](neighbour.md)
SIBLING_RE = re.compile(r"\]\((?!https?:|#)([\w.-]+\.\w+)\)")

# A path leading outside the skill - into a notes vault or into another skill. Only
# what sits entirely inside backticks is taken: note names contain spaces and dashes,
# and such a path cannot be cut out of running prose without grabbing extra words.
VAULT_RE = re.compile(r"`([~$][^`\n]{3,200})`")

# A pointer to a section of a reference: `references/foo.md` → "Section", or
# sections "A" and "B". Quotes are not allowed between the file and the pointer:
# without that the window jumps over unrelated text and latches onto a quotation that
# has nothing to do with any section.
QUOTES = "\"\u201c\u201d\u00ab\u00bb"
SECPTR_RE = re.compile(
    r"((?:" + "|".join(SUBDIRS) + r")/[\w./-]+\.md)`?"      # the file
    r"[^\n" + QUOTES + r"]{0,40}?"                          # a little text, no quotes
    r"(?:\u2192|->|sections?)"                              # an explicit section marker
    r"([^\n]{0,140})"                                       # rest of the line: the sections live there
)
# The section name itself: straight quotes, curly quotes or guillemets.
SECTION_RE = re.compile(
    r"\"([^\"\n]{2,80})\""
    r"|\u201c([^\u201d\n]{2,80})\u201d"
    r"|\u00ab([^\u00bb\n]{2,80})\u00bb")

# A code block in the text: ```python … ```. The fence length is remembered so that a
# nested block inside an example does not end the outer one too early.
CODE_RE = re.compile(r"^(?P<fence>`{3,})[ \t]*(\w+)[^\n]*\n(.*?)^(?P=fence)", re.M | re.S)

# Code that DEMONSTRATES rather than EXECUTES. A bad/good pair is teaching material:
# moving it into scripts/ means throwing it away, not optimising it. Length here is a
# sign of a thorough example, not of debt.
#
# The marker has to LABEL the block - stand on its own header line, usually inside a
# comment (`# WRONG`). Searching for it anywhere inside the code is not allowed: words
# like "before" and "after" show up in ordinary comments, and "❌" shows up in a string
# that a real script prints. That laxity once hid a 49-line draft script - the very
# thing this check was written for.
#
# The markers are English-only, as is the section keyword in SECPTR_RE above. If your
# skills are written in another language, add your own words to both alternations -
# that is the one place in this file that is language-specific.
EXAMPLE_RE = re.compile(
    r"^[ \t]*(?:#+|//)?[ \t]*"
    r"(?:WRONG|RIGHT|BAD|GOOD|BEFORE|AFTER|DON'T|DO|\u274c|\u2705)"
    r"[ \t]*[-\u2014:,]?[^\n]{0,60}$", re.M)

# Agent Skills specification: agentskills.io/specification.md
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DESC_MAX = 1024
NAME_MAX = 64
COMPAT_MAX = 500


def path_exists(p):
    """Does this file or folder exist - allowing for how the name is encoded.

    Within one and the same string a letter with a diacritic can be a single character
    (NFC) or a base letter plus a combining mark (NFD). Different bytes,
    indistinguishable by eye, and `os.path.exists` answers "no" for a file that is
    right there. So a miss is re-checked by comparing the directory listing in
    normalized form.
    """
    p = os.path.expanduser(p.replace("$HOME", "~").replace("${HOME}", "~"))
    p = p.rstrip("/\\") or p
    if os.path.exists(p):
        return True
    parent, base = os.path.split(p)
    if not base or not os.path.isdir(parent):
        return False
    want = unicodedata.normalize("NFC", base).casefold()
    try:
        return any(unicodedata.normalize("NFC", n).casefold() == want
                   for n in os.listdir(parent))
    except OSError:
        return False


def vault_paths(text):
    """Outbound paths taken from backticks - templates and placeholders excluded.

    `<name>.md` and `*` describe the shape of a path, not a path: there is nothing
    there to check.
    """
    out = set()
    for raw in VAULT_RE.findall(text):
        p = raw.strip().rstrip(".,;:)\u00bb")
        if not p.startswith(("~/", "~\\", "$HOME", "${HOME}")):
            continue
        if any(c in p for c in "<>*?|"):
            continue
        if " " in p and not re.search(r"\.\w{1,5}$|/$", p):
            continue                      # "~/somewhere in there" from prose, not a path
        out.add(p)
    return out


def fm_field(fm, key):
    """A frontmatter value, block scalars (`>-`, `|`) included, collapsed to one line."""
    m = re.search(
        rf"^{key}:[ \t]*(>[-+]?|\|[-+]?)?[ \t]*\n?(.*?)(?=^[A-Za-z_][\w-]*:|\Z)",
        fm, re.M | re.S)
    return " ".join(m.group(2).split()) if m else None


def headings(path):
    """The set of headings in a file, normalized for comparison."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return set()
    # MULTILINE is mandatory: without it ^ and $ only match the very start and end of
    # the whole text, no headings are collected at all, and the check quietly idles
    # while reporting "clean".
    return {" ".join(h.split()).casefold()
            for h in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M)}


def collect(text, mentioned, cross, wildcard_dirs, cross_full=None):
    """Split the links found in a text into local, cross-skill and templated ones."""
    for skill, rel in CROSS_RE.findall(text):
        cross.add(rel)
        if cross_full is not None:
            cross_full.add((skill, rel))
    for d in WILDCARD_RE.findall(text):
        wildcard_dirs.add(d.rstrip("/"))
    mentioned |= set(LINK_RE.findall(text))


def collect_pointers(text, source, pointers):
    """"File + section" pointers: (where from, which file, which section).

    The tail after the pointer is parsed whole - that is how the form
    'sections "A" and "B"', with several sections on one line, is caught too.
    """
    for target, tail in SECPTR_RE.findall(text):
        for groups in SECTION_RE.findall(tail):
            section = next((g for g in groups if g), None)
            if section:
                pointers.append((source, target, section))


def frontmatter(text):
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else None


def check(skill):
    root = os.path.join(SKILLS_DIR, skill)
    md = os.path.join(root, "SKILL.md")
    errors, warnings = [], []
    name, slash_only = None, False

    if not os.path.isfile(md):
        return ["no SKILL.md"], [], 0, None

    with open(md, encoding="utf-8") as f:
        text = f.read()
    size = len(text.encode("utf-8"))

    # 1. frontmatter - required fields and compliance with the spec
    fm = frontmatter(text)
    if fm is None:
        errors.append("no frontmatter (--- at the top of the file)")
    else:
        for key in ("name:", "description:"):
            if key not in fm:
                errors.append(f"frontmatter has no `{key}`")

        # Spec limits are warnings, not errors: Claude Code does not enforce them today
        # and the skill works. It breaks on publication and on `skills-ref validate`.
        name = fm_field(fm, "name")
        if name:
            if name != skill:
                errors.append(
                    f"`name: {name}` does not match the folder name `{skill}` - "
                    f"the spec requires them to match; rename one of the two")
            if len(name) > NAME_MAX:
                warnings.append(f"`name` is {len(name)} chars > {NAME_MAX} in the spec")
            if not NAME_RE.match(name):
                warnings.append(
                    f"`name: {name}` breaks the spec: lowercase latin letters, digits "
                    f"and single hyphens only, never at the edges")

        desc = fm_field(fm, "description")
        if desc is not None:
            if not desc:
                errors.append("`description` is empty - the skill will never trigger")
            elif len(desc) > DESC_MAX:
                warnings.append(
                    f"`description` is {len(desc)} chars > {DESC_MAX} in the spec "
                    f"({len(desc) - DESC_MAX} over) - Claude Code tolerates it, "
                    f"publishing and `skills-ref validate` do not")

        compat = fm_field(fm, "compatibility")
        if compat and len(compat) > COMPAT_MAX:
            warnings.append(
                f"`compatibility` is {len(compat)} chars > {COMPAT_MAX} in the spec")

        # A skill with model invocation disabled is only ever called by slash command.
        # It needs no trigger wording: a human decides, not the description.
        slash_only = (fm_field(fm, "disable-model-invocation") or "").lower() == "true"
        if desc and not slash_only and len(desc) < DESC_MIN:
            warnings.append(
                f"`description` is {len(desc)} chars < {DESC_MIN} - it holds no trigger "
                f"conditions, and those are what the agent decides by")

        # A typo in a key does not break the YAML: the field just vanishes, meaning included.
        for key in re.findall(r"^([A-Za-z_][\w-]*):", fm, re.M):
            if key not in KNOWN_KEYS:
                warnings.append(
                    f"unknown frontmatter key `{key}:` - a typo? "
                    f"the harness ignores it silently")

    # 2. collect every link to a file of this skill - from SKILL.md and from the references
    mentioned, cross, wildcard_dirs, cross_full = set(), set(), set(), set()
    pointers = []
    texts = [text]                        # for the outbound path check - see item 9
    sources = [("SKILL.md", text)]        # same, by file name - see item 15
    collect(text, mentioned, cross, wildcard_dirs, cross_full)
    collect_pointers(text, "SKILL.md", pointers)
    for sub in SUBDIRS:
        d = os.path.join(root, sub)
        if not os.path.isdir(d):
            continue
        for dirpath, _, files in os.walk(d):
            for fn in files:
                if fn.endswith((".md", ".txt")):
                    full = os.path.join(dirpath, fn)
                    with open(full, encoding="utf-8", errors="replace") as f:
                        body = f.read()
                    texts.append(body)
                    rel_here = os.path.relpath(full, root).replace("\\", "/")
                    sources.append((rel_here, body))
                    collect(body, mentioned, cross, wildcard_dirs, cross_full)
                    collect_pointers(body, rel_here, pointers)
                    # sibling markdown links resolve against the file's own folder
                    for sib in SIBLING_RE.findall(body):
                        target = os.path.join(dirpath, sib)
                        if not os.path.exists(target):
                            here = os.path.relpath(full, root).replace("\\", "/")
                            errors.append(f"broken sibling link: {sib} (from {here})")
                        else:
                            mentioned.add(os.path.relpath(target, root).replace("\\", "/"))

    # 2b. a link to the skill's own file written as a full path
    #     (`~/.claude/skills/video/scripts/…`) is our own route, not somebody else's.
    #     Without this step a script that the skill can only invoke the one way that
    #     works (a full path, or it cannot be run from an arbitrary folder) counted as
    #     an orphan - and the whole "orphan" class stopped meaning anything.
    mentioned |= {rel for other, rel in cross_full if other == skill}

    # 3. broken links - but only the ones not explained by a link into another skill
    others = {d for d in os.listdir(SKILLS_DIR)
              if os.path.isdir(os.path.join(SKILLS_DIR, d)) and d != skill}
    for rel in sorted(mentioned - cross):
        if os.path.exists(os.path.join(root, rel)):
            continue
        elsewhere = [o for o in others if os.path.exists(os.path.join(SKILLS_DIR, o, rel))]
        if elsewhere:
            warnings.append(
                f"path with no skill name: {rel} - it lives in `{elsewhere[0]}`, "
                f"spell it out as `~/.claude/skills/{elsewhere[0]}/{rel}`")
        elif not os.path.isdir(os.path.join(root, rel.split("/", 1)[0])):
            # The folder does not exist at all - this is nearly always an example path
            # from SOMEBODY ELSE'S repository (`assets/readme/hero.svg` in a README
            # skill), not our routing. Not an error: otherwise the hook would fail on
            # every edit because of example paths in the prose.
            warnings.append(
                f"path with no such folder in the skill: {rel} - looks like an example, "
                f"not a route")
        else:
            errors.append(f"link to a missing file: {rel}")

    # 3b. section pointers: the file exists, the heading no longer does
    heads_cache = {}
    for source, target, section in pointers:
        full = os.path.join(root, target)
        if not os.path.isfile(full):
            continue                      # a missing file is check 3's job, no duplicates
        if full not in heads_cache:
            heads_cache[full] = headings(full)
        heads = heads_cache[full]
        if not heads:
            continue                      # no headings at all - nothing to compare against
        want = " ".join(section.split()).casefold()
        if not any(want == h or want in h for h in heads):
            warnings.append(
                f'pointer to a missing section: {target} \u2192 "{section}" (from {source})')

    # 4. orphans
    on_disk = set()
    for sub in SUBDIRS:
        d = os.path.join(root, sub)
        if not os.path.isdir(d):
            continue
        for dirpath, _, files in os.walk(d):
            for fn in files:
                if fn.startswith(".") or "__pycache__" in dirpath:
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/")
                on_disk.add(rel)
    for rel in sorted(on_disk - mentioned):
        # the folder is wired up through a template (references/types/<type>.md) -
        # the files inside it are not orphans
        if any(rel.startswith(d + "/") for d in wildcard_dirs):
            continue
        warnings.append(f"orphan (nothing links to it): {rel}")

    # 5. cross-skill links: does the skill exist, and does the file exist inside it
    for other, rel in sorted(cross_full):
        if other == skill:
            # our own file spelled as a full path: it slipped past check 3 (which
            # subtracts cross-skill links), so its existence is verified here
            if not os.path.exists(os.path.join(root, rel)):
                errors.append(f"link to a missing file: {rel}")
            continue
        if not os.path.isdir(os.path.join(SKILLS_DIR, other)):
            errors.append(f"link to a skill that does not exist: {other} (in {rel})")
        elif not os.path.exists(os.path.join(SKILLS_DIR, other, rel)):
            errors.append(f"skill {other} has no file {rel}")

    # 6. budget - for SKILL.md and for the references alike: the second level is paid
    #    for in context too, just later. A 30 KB reference is opened whole, and there
    #    is nobody to split it for you.
    if size > BUDGET:
        warnings.append(
            f"SKILL.md is {size} B > the {BUDGET} B budget - "
            f"something can move into references/")
    for rel in sorted(on_disk):
        if not rel.endswith(".md"):
            continue
        try:
            rsize = os.path.getsize(os.path.join(root, rel))
        except OSError:
            continue
        if rsize > REF_BUDGET:
            warnings.append(
                f"{rel} is {rsize} B > the {REF_BUDGET} B budget - "
                f"it is opened whole, split it")

    # 7. the body: frontmatter present, instructions missing - it activates and says nothing
    main_body = text[text.find("\n---", 3) + 4:] if fm is not None else text
    if not main_body.strip():
        errors.append("no body: frontmatter is there, instructions are not")
    elif len(main_body.strip()) < 30 and not slash_only:
        warnings.append(f"body is {len(main_body.strip())} chars - the skill is nearly empty")

    # 8. outbound paths: the note was renamed and the skill still calls it by the old name
    for p in sorted({p for t in texts for p in vault_paths(t)}):
        if not path_exists(p):
            errors.append(f"no such path: {p}")

    # 9. code as prose instead of a file. Only meaningful lines are counted: blank and
    #    comment lines do not make a program, and the threshold drifts if they are.
    for src, body in sources:
        for _, lang, code in CODE_RE.findall(body):
            if lang.lower() not in CODE_LANGS or EXAMPLE_RE.search(code):
                continue
            payload = [ln for ln in code.splitlines()
                       if ln.strip() and not ln.strip().startswith("#")]
            if len(payload) > SCRIPT_LINES:
                warnings.append(
                    f"code as prose: {src} - a ```{lang} block of {len(payload)} lines "
                    f"> {SCRIPT_LINES}; move it into scripts/ and leave the call and "
                    f"how to read its output in the text")

    return errors, warnings, size, name


def skills_in(blob):
    """Skill names mentioned as paths inside a text.

    The path to the skills folder is written in many ways: `~/.claude/skills/bars`,
    `C:\\Users\\me\\.claude\\skills\\bars`, and under git-bash also
    `/c/Users/me/.claude/skills/bars`. The full path is no anchor here - we latch onto
    its two-part tail (`.claude/skills`) and take whatever name follows it.
    """
    norm = blob.replace("\\", "/")
    parts = [p for p in os.path.normpath(SKILLS_DIR).replace("\\", "/").split("/") if p]
    tail = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    found, seen_root = set(), False
    for m in re.finditer(re.escape(tail) + r"(?:/([\w.-]+))?", norm, re.I):
        seen_root = True
        who = m.group(1)
        if who and not who.startswith(".") and not who.endswith(".py"):
            found.add(who)
    if seen_root and not found:
        found.add("*")                    # the folder was touched, which skill is unclear
    return found


def touched_skills():
    """Which skills this tool call touched.

    Write/Edit put the path in `file_path`, Bash puts the whole command in `command`.
    Reading only `file_path` means being blind to edits through heredoc, sed, mv and
    rm - and skills are edited that way just as often as through an editor.
    """
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return set()
    ti = payload.get("tool_input") or {}
    blob = " ".join(
        str(ti.get(k, "")) for k in ("file_path", "command", "path", "notebook_path"))
    return skills_in(blob) if blob.strip() else set()


def read_marker():
    try:
        with open(MARKER, encoding="utf-8") as f:
            return {ln.strip() for ln in f if ln.strip()}
    except OSError:
        return set()


def write_marker(names):
    try:
        with open(MARKER, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(names)))
    except OSError:
        pass                              # the marker is a convenience, not a precondition


def clear_marker():
    try:
        os.remove(MARKER)
    except OSError:
        pass


def positional(argv):
    """Skill names from the arguments - without mistaking `--skills-dir <path>` for one."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a == "--skills-dir":
            skip = True
        elif not a.startswith("-"):
            out.append(a)
    return out


def main():
    args = positional(sys.argv[1:])
    mark = "--mark" in sys.argv or "--hook" in sys.argv
    stop = "--stop" in sys.argv
    quiet = stop or "--quiet" in sys.argv

    if mark:
        names = touched_skills()
        if names:
            write_marker(read_marker() | names)
        return 0                          # PostToolUse only marks, it does not judge

    touched = None
    if stop:
        try:
            payload = json.load(sys.stdin)
        except Exception:
            payload = {}
        if payload.get("stop_hook_active"):
            return 0                      # the stop was already blocked - do not loop
        touched = read_marker()
        if not touched:
            return 0                      # no skill touched this turn, nothing to check
        clear_marker()

    targets = args or sorted(
        d for d in os.listdir(SKILLS_DIR)
        if os.path.isdir(os.path.join(SKILLS_DIR, d))
        and not d.startswith(".")
        and os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md"))
    )

    total_err = 0
    lines = []
    by_name = {}
    for skill in targets:
        errors, warnings, size, name = check(skill)
        if name:
            by_name.setdefault(name, []).append(skill)
        total_err += len(errors)
        # Warnings: on a manual run - for every skill; in the Stop hook - only for the
        # ones edited this turn (their author has already seen the others and is not
        # going to fix them now); on a bare --quiet - never, there only breakage counts.
        show_warn = bool(warnings) and stop and ("*" in touched or skill in touched)
        if quiet and not errors and not show_warn:
            continue
        lines.append(f"{'⛔' if errors else ('⚠️ ' if warnings else '✅')} {skill}  ({size} B)")
        lines.extend(f"     ⛔ {e}" for e in errors)
        if show_warn or not quiet:
            lines.extend(f"     ⚠️  {w}" for w in warnings)

    # Duplicate `name` across skills: the harness picks one and says nothing about the
    # other, and which one it picks is not predictable. The check is global, hence here.
    for name, dirs in sorted(by_name.items()):
        if len(dirs) > 1:
            total_err += 1
            lines.append(
                f"⛔ duplicate `name: {name}` - folders {', '.join(dirs)}; "
                f"one shadows the other")

    if quiet:
        if lines:
            # in a hook the reader is the model, and the harness hands it stderr, not stdout
            print("The skill check found breakage that needs fixing:" if total_err
                  else "Skills were edited, the check has remarks:", file=sys.stderr)
            print("\n".join(lines), file=sys.stderr)
        if not total_err:
            return 0
        # in hook mode exit code 2 is the only one the harness passes to the model;
        # with code 1 the message would settle in a log and nobody would read it
        return 2 if stop else 1

    print("\n".join(lines))
    print()
    print(f"skills: {len(targets)} · errors: {total_err}")
    return 1 if total_err else 0


if __name__ == "__main__":
    sys.exit(main())
