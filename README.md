# skills

Claude Code skills, packaged as a plugin.

| Skill | What it does |
|---|---|
| [`/cheat-sheet`](skills/cheat-sheet/SKILL.md) | Turns text/markdown notes into a printable pen cheat sheet (DOCX or PDF): Roboto Condensed 4 pt, interline 1.0, justified, max 11 lines per strip, 3 cm left margin, dashed cut boxes. |

## Install

Install all skills as a plugin, from inside Claude Code:

```
/plugin marketplace add Hserfx/skills
/plugin install hserfx-skills@hserfx
```

Skills are then namespaced, e.g. `/hserfx-skills:cheat-sheet`. Pull updates with `/plugin marketplace update hserfx`.

Or link every skill into your personal skills (unprefixed names):

```sh
git clone https://github.com/Hserfx/skills.git ~/skills-repo
mkdir -p ~/.claude/skills
for d in ~/skills-repo/skills/*/; do ln -s "$d" ~/.claude/skills/"$(basename "$d")"; done
```

The cheat-sheet script needs only `python3` (standard library).

## Fonts

`skills/cheat-sheet/fonts/` holds static instances of [Roboto Condensed](https://fonts.google.com/specimen/Roboto+Condensed), generated from the Google Fonts variable font and licensed under the SIL Open Font License 1.1 (`fonts/OFL.txt`).
