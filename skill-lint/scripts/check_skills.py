#!/usr/bin/env python3
"""
Проверка целостности скиллов Claude Code — backpressure для двухэтапной загрузки.

Скилл грузится в два этапа: сперва SKILL.md, а справочники — по ссылкам из него.
Если ссылка битая, агент не падает и не жалуется: он просто молча пропускает шаг,
и заметить это можно только по тому, что работа сделана хуже обычного. Скрипт
превращает такую тихую поломку в громкую.

Запуск:  python check_skills.py                  # все скиллы
         python check_skills.py my-skill other-skill    # только эти
         python check_skills.py --skills-dir ~/.claude/skills

Где искать скиллы, решается в таком порядке: флаг `--skills-dir` → переменная
окружения `CLAUDE_SKILLS_DIR` → папка самого скрипта, если в ней лежат скиллы →
`~/.claude/skills`.

Что ловит:
  1. Битая маршрутизация — SKILL.md ссылается на references/…, которого нет.
     Это главная поломка при выносе кусков в справочники: инструкция «открой X»
     без X превращается в тихий пропуск шага.
  2. Битые соседские ссылки — внутри references/ на соседа ссылаются коротко,
     markdown-ссылкой [текст](сосед.md). Проверка (1) их не видит: разные формы.
  3. Ссылка на несуществующий скилл — ~/.claude/skills/<имя>/… после переименования.
  4. Сироты — файл в references/ есть, но на него никто не ссылается.
     Кандидат на pruning: либо забыли подключить, либо он уже не нужен.
  5. Раздутый SKILL.md — грузится при каждой активации целиком (см. BUDGET).
  6. Сломанный frontmatter — без name/description скилл не активируется.
  7. Нарушения спецификации Agent Skills во frontmatter: длина `description`
     (максимум 1024), формат и длина `name`, несовпадение `name` с именем папки,
     длина `compatibility` (максимум 500). Claude Code эти лимиты сегодня не
     навязывает, поэтому поломка тихая: локально работает, а при публикации
     скилла или прогоне `skills-ref validate` — отлетает. Поэтому ⚠️, не ⛔.
  8. Битый указатель на раздел — `references/foo.md` → «Раздел», которого в файле
     уже нет. Файл на месте, поэтому проверка (1) молчит, а агент открывает
     справочник и не находит того, за чем пришёл. Рвётся при переименовании
     заголовка — то есть при обычной правке, без всякого переезда файлов.

Флаги:
  --quiet   молчать, когда всё чисто; печатать только проблемы
  --hook    режим хука PostToolUse: прочитать JSON со stdin и промолчать,
            если правка была не в ~/.claude/skills (иначе проверка шумела бы
            на каждом Write/Edit во всех проектах). Подразумевает --quiet.

Коды выхода: 0 — чисто, 1 — есть ошибки (⛔), предупреждения (⚠️) не валят.
"""
import glob
import json
import os
import re
import sys

BUDGET = 15_000          # байт SKILL.md — мягкий потолок (~5-6 тыс. токенов на активацию)
SUBDIRS = ("references", "assets", "scripts", "templates")


def resolve_skills_dir(argv):
    """Где лежат скиллы.

    Считать «папка скрипта = папка скиллов» нельзя: скрипт может лежать и в корне
    скиллов, и внутри своего скилла (skill-lint/scripts/), и вообще где угодно.
    Поэтому порядок: явный флаг → переменная окружения → папка скрипта, если в ней
    действительно лежат скиллы → стандартное место.
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

# ссылки вида references/foo.md, assets/bar.md, scripts/baz.py — в тексте и в бэктиках
LINK_RE = re.compile(r"(?:" + "|".join(SUBDIRS) + r")/[\w./-]+\.\w+")
# кросс-скилловая ссылка: .../skills/<другой скилл>/references/foo.md или .../SKILL.md — не наш файл
CROSS_RE = re.compile(
    r"skills/([\w-]+)/((?:(?:" + "|".join(SUBDIRS) + r")/[\w./-]+\.\w+)|SKILL\.md)")
# шаблонная ссылка на каталог: references/institutions/<slug>.md → вся папка используется
WILDCARD_RE = re.compile(r"((?:" + "|".join(SUBDIRS) + r")/[\w./-]*?)/?<[^>]+>\.\w+")
# markdown-ссылка на соседний файл внутри той же папки: [текст](сосед.md)
SIBLING_RE = re.compile(r"\]\((?!https?:|#)([\w.-]+\.\w+)\)")

# Указатель на раздел справочника: `references/foo.md` → «Раздел» / «…», разделы «A» и «B».
# Между файлом и указателем не пускаем кавычки-ёлочки: без этого окно перепрыгивает
# через посторонний текст и цепляет цитату, не имеющую отношения к разделу.
SECPTR_RE = re.compile(
    r"((?:" + "|".join(SUBDIRS) + r")/[\w./-]+\.md)`?"      # файл
    r"[^\n«»]{0,40}?"                                       # немного текста без кавычек
    r"(?:→|->|раздел[аыов]*)"                               # явное указание на раздел
    r"([^\n]{0,140})"                                       # хвост строки: там сами «разделы»
)
SECTION_RE = re.compile(r"«([^»]{2,80})»")

# Спецификация Agent Skills: agentskills.io/specification.md
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DESC_MAX = 1024
NAME_MAX = 64
COMPAT_MAX = 500


def fm_field(fm, key):
    """Значение ключа frontmatter, включая блочные скаляры (`>-`, `|`), схлопнутое в строку."""
    m = re.search(
        rf"^{key}:[ \t]*(>[-+]?|\|[-+]?)?[ \t]*\n?(.*?)(?=^[A-Za-z_][\w-]*:|\Z)",
        fm, re.M | re.S)
    return " ".join(m.group(2).split()) if m else None


def headings(path):
    """Множество заголовков файла, нормализованных для сравнения."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return set()
    # MULTILINE обязателен: иначе ^ и $ ловят только начало и конец всего текста,
    # заголовки не собираются вовсе и проверка молча простаивает, отчитываясь «чисто».
    return {" ".join(h.split()).casefold()
            for h in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M)}


def collect(text, mentioned, cross, wildcard_dirs, cross_full=None):
    """Разложить ссылки из текста на локальные, кросс-скилловые и шаблонные."""
    for skill, rel in CROSS_RE.findall(text):
        cross.add(rel)
        if cross_full is not None:
            cross_full.add((skill, rel))
    for d in WILDCARD_RE.findall(text):
        wildcard_dirs.add(d.rstrip("/"))
    mentioned |= set(LINK_RE.findall(text))


def collect_pointers(text, source, pointers):
    """Указатели «файл + раздел»: (откуда, какой файл, какой раздел).

    Хвост после указателя разбираем целиком — так ловится и форма
    «разделы «A» и «B»», где разделов в одной строке несколько.
    """
    for target, tail in SECPTR_RE.findall(text):
        for section in SECTION_RE.findall(tail):
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

    if not os.path.isfile(md):
        return [f"нет SKILL.md"], [], 0

    with open(md, encoding="utf-8") as f:
        text = f.read()
    size = len(text.encode("utf-8"))

    # 1. frontmatter — наличие полей и соответствие спецификации
    fm = frontmatter(text)
    if fm is None:
        errors.append("нет frontmatter (--- в начале файла)")
    else:
        for key in ("name:", "description:"):
            if key not in fm:
                errors.append(f"во frontmatter нет `{key}`")

        # Лимиты спецификации — предупреждения, а не ошибки: Claude Code их сегодня
        # не навязывает, скилл работает. Ломается при публикации и на `skills-ref validate`.
        name = fm_field(fm, "name")
        if name:
            if name != skill:
                errors.append(
                    f"`name: {name}` не совпадает с именем папки `{skill}` — "
                    f"по спецификации обязано совпадать; переименуй одно из двух")
            if len(name) > NAME_MAX:
                warnings.append(f"`name` {len(name)} симв. > {NAME_MAX} по спецификации")
            if not NAME_RE.match(name):
                warnings.append(
                    f"`name: {name}` не по спецификации: только строчные латинские буквы, "
                    f"цифры и одиночные дефисы, не с краю")

        desc = fm_field(fm, "description")
        if desc is not None:
            if not desc:
                errors.append("`description` пустой — скилл не будет срабатывать")
            elif len(desc) > DESC_MAX:
                warnings.append(
                    f"`description` {len(desc)} симв. > {DESC_MAX} по спецификации "
                    f"(лишних {len(desc) - DESC_MAX}) — Claude Code стерпит, "
                    f"публикация и `skills-ref validate` — нет")

        compat = fm_field(fm, "compatibility")
        if compat and len(compat) > COMPAT_MAX:
            warnings.append(f"`compatibility` {len(compat)} симв. > {COMPAT_MAX} по спецификации")

    # 2. собрать все ссылки на файлы скилла — из SKILL.md и из самих справочников
    mentioned, cross, wildcard_dirs, cross_full = set(), set(), set(), set()
    pointers = []
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
                    collect(body, mentioned, cross, wildcard_dirs, cross_full)
                    collect_pointers(
                        body, os.path.relpath(full, root).replace("\\", "/"), pointers)
                    # соседские markdown-ссылки резолвятся от папки самого файла
                    for sib in SIBLING_RE.findall(body):
                        target = os.path.join(dirpath, sib)
                        if not os.path.exists(target):
                            here = os.path.relpath(full, root).replace("\\", "/")
                            errors.append(f"битая ссылка на соседа: {sib} (из {here})")
                        else:
                            mentioned.add(os.path.relpath(target, root).replace("\\", "/"))

    # 3. битые ссылки — но только те, что не объясняются ссылкой на чужой скилл
    others = {d for d in os.listdir(SKILLS_DIR)
              if os.path.isdir(os.path.join(SKILLS_DIR, d)) and d != skill}
    for rel in sorted(mentioned - cross):
        if os.path.exists(os.path.join(root, rel)):
            continue
        elsewhere = [o for o in others if os.path.exists(os.path.join(SKILLS_DIR, o, rel))]
        if elsewhere:
            warnings.append(
                f"путь без имени скилла: {rel} — лежит в `{elsewhere[0]}`, "
                f"уточни до `~/.claude/skills/{elsewhere[0]}/{rel}`")
        elif not os.path.isdir(os.path.join(root, rel.split("/", 1)[0])):
            # Папки нет вовсе — это почти всегда пример пути в ЧУЖОМ репозитории
            # (`assets/readme/hero.svg` у beautify-github-readme), а не наша маршрутизация.
            # Ошибкой не считаем: иначе хук валится на каждой правке из-за примеров в тексте.
            warnings.append(f"путь без такой папки в скилле: {rel} — похоже на пример, не маршрут")
        else:
            errors.append(f"ссылка на несуществующий файл: {rel}")

    # 3б. указатели на разделы: файл существует, а заголовка в нём уже нет
    heads_cache = {}
    for source, target, section in pointers:
        full = os.path.join(root, target)
        if not os.path.isfile(full):
            continue                      # отсутствие файла ловит проверка 3, не дублируем
        if full not in heads_cache:
            heads_cache[full] = headings(full)
        heads = heads_cache[full]
        if not heads:
            continue                      # заголовков нет вовсе — сверять не с чем
        want = " ".join(section.split()).casefold()
        if not any(want == h or want in h for h in heads):
            warnings.append(
                f"указатель на раздел, которого нет: {target} → «{section}» (из {source})")

    # 4. сироты
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
        # каталог подключён шаблоном (references/types/<тип>.md) — файлы в нём не сироты
        if any(rel.startswith(d + "/") for d in wildcard_dirs):
            continue
        warnings.append(f"сирота (никто не ссылается): {rel}")

    # 5. кросс-скилловые ссылки: существует ли скилл и файл в нём
    for other, rel in sorted(cross_full):
        if other == skill:
            continue
        if not os.path.isdir(os.path.join(SKILLS_DIR, other)):
            errors.append(f"ссылка на несуществующий скилл: {other} (в {rel})")
        elif not os.path.exists(os.path.join(SKILLS_DIR, other, rel)):
            errors.append(f"в скилле {other} нет файла {rel}")

    # 6. бюджет
    if size > BUDGET:
        warnings.append(f"SKILL.md {size} б > бюджета {BUDGET} б — что-то можно вынести в references/")

    return errors, warnings, size


def touched_our_skills():
    """Режим хука: правка была внутри ~/.claude/skills? Читает JSON PostToolUse со stdin."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return True                       # не разобрали вход — лучше проверить, чем промолчать
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path:
        return False
    try:
        return os.path.commonpath([
            os.path.realpath(path), os.path.realpath(SKILLS_DIR)
        ]) == os.path.realpath(SKILLS_DIR)
    except ValueError:                    # разные диски на Windows
        return False


def positional(argv):
    """Имена скиллов из аргументов — не спутав их со значением `--skills-dir <путь>`."""
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
    hook = "--hook" in sys.argv
    quiet = hook or "--quiet" in sys.argv

    if hook and not touched_our_skills():
        return 0

    targets = args or sorted(
        d for d in os.listdir(SKILLS_DIR)
        if os.path.isdir(os.path.join(SKILLS_DIR, d))
        and not d.startswith(".")
        and os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md"))
    )

    total_err = 0
    lines = []
    for skill in targets:
        errors, warnings, size = check(skill)
        total_err += len(errors)
        if quiet and not errors:
            continue                      # в тихом режиме молчим и о предупреждениях
        mark = "⛔" if errors else ("⚠️ " if warnings else "✅")
        lines.append(f"{mark} {skill}  ({size} б)")
        lines.extend(f"     ⛔ {e}" for e in errors)
        lines.extend(f"     ⚠️  {w}" for w in warnings)

    if quiet:
        if total_err:
            print("Проверка скиллов нашла поломки — их нужно починить:")
            print("\n".join(lines))
        if not total_err:
            return 0
        # в режиме хука код 2 — единственный, который харнесс передаёт модели;
        # с кодом 1 сообщение осело бы в логе и никто бы его не прочитал
        return 2 if hook else 1

    print("\n".join(lines))
    print()
    print(f"скиллов: {len(targets)} · ошибок: {total_err}")
    return 1 if total_err else 0


if __name__ == "__main__":
    sys.exit(main())
