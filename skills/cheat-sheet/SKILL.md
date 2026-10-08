---
name: cheat-sheet
description: Turn text/markdown notes into a printable pen cheat sheet (DOCX, or PDF on request) — tiny strips to cut out with scissors and roll into a pen.
argument-hint: "<notes file | pasted text> [--format pdf]"
---

# Pen cheat sheet

`scripts/cheatsheet.py` (pure `python3`, no dependencies) does all layout: it breaks lines with the bundled Roboto Condensed metrics, packs them into strips of at most 11 lines, and writes the file. Line counts it reports are exactly what prints. Your job is the input, the **shrink** loop, and the report.

Fixed format (decided with the user; change only on explicit request): Roboto Condensed 4 pt, interline exactly 1.0 (4 pt), justified, A4 portrait, left margin 3 cm, right 2.5 cm, top/bottom 1.5 cm, 11 lines per strip, dashed cut box around each strip, `✂ n/N` label in the left margin.

## Steps

1. **Input.** A file path → use it as is. Pasted text → write it verbatim to `./cheatsheet-notes.md` and use that. Content stays verbatim: no summarising, no rewording. Markdown is flattened by the script (headings become bold inline `Heading:`, list items `•`, paragraphs separated by `|`, bold/italic kept).
2. **Format and output.** DOCX by default; PDF when the user asks for it (`--format pdf`, "w PDF", etc.). Default output path is `<input-stem>-cheatsheet.<format>` next to the input (the script's default); pass `-o` only when the user names a path.
3. **Check.** Run `python3 <skill-dir>/scripts/cheatsheet.py <input> --check`. It prints JSON: `lines_per_strip`, `shrink_needed`, `lines_to_cut`, `avg_chars_per_line`, `unsupported_chars`.
4. **Shrink** — only when `shrink_needed` is true (more than one strip and the last has ≤5 lines). Goal: remove `lines_to_cut` lines so the last strip disappears. Escalate in order, re-running `--check` after each:
   1. Whitespace: add `--tight` (glues `|`/`•` separators to the next word).
   2. Filler words: write a copy `<input-stem>-condensed.md` with redundant words removed (articles, "jest to", "w celu", repetitions). Every fact, number, name and formula stays.
   3. Abbreviations: in the same copy, abbreviate frequent long words and phrases (e.g. `funkcja → f.`, `twierdzenie → tw.`, `na przykład → np.`, `i tak dalej → itd.`). Use abbreviations a reader recognises without a legend, and only characters the font has (logic symbols like ∀ ∈ ⇒ are missing; `--check` lists any that slip in).

   Budget the cut with `lines_to_cut × avg_chars_per_line` characters, plus a margin. Stop when `shrink_needed` is false. If after step 3 the tail still survives, build from the **original verbatim input** (with `--tight`) and tell the user how many lines were missing.
5. **Build.** Run the script without `--check` on the final input (plus `--tight`/`--format` as decided).
6. **Report** in one short message: output path, number of strips and lines on the last one, whether shrink ran (and what it changed: tight / filler words / abbreviations, with the condensed file's path), any `unsupported_chars` (printed as `?`; suggest a replacement), and the printing tip: print at **100% scale / "Actual size"**, never "Fit to page"; PDF prints most reliably, since some Word versions ignore embedded fonts.

Done when the output file exists and every `shrink_needed` case ended either with the tail gone or with the user told how many lines were missing.
