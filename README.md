# skills

Claude Code skills, packaged as a plugin.

| Skill | What it does |
|---|---|
| [`/cheat-sheet`](skills/cheat-sheet/SKILL.md) | Turns text/markdown notes into a printable pen cheat sheet (DOCX or PDF): Roboto Condensed 4 pt, interline 1.0, justified, max 11 lines per strip, 3 cm left margin, dashed cut boxes. |

## Install

Link a skill into your personal skills:

```sh
git clone https://github.com/Hserfx/skills.git
ln -s "$PWD/skills/skills/cheat-sheet" ~/.claude/skills/cheat-sheet
```

The cheat-sheet script needs only `python3` (standard library).

## Fonts

`skills/cheat-sheet/fonts/` holds static instances of [Roboto Condensed](https://fonts.google.com/specimen/Roboto+Condensed), generated from the Google Fonts variable font and licensed under the SIL Open Font License 1.1 (`fonts/OFL.txt`).
