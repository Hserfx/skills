#!/usr/bin/env python3
"""Build a printable pen cheat sheet (DOCX or PDF) from text/markdown notes.

Pure standard library. Lines are broken here, deterministically, using the
metrics of the bundled Roboto Condensed fonts, so the line count printed in
the JSON summary is exactly what lands on paper.

Usage:
    cheatsheet.py INPUT|- [-o OUT] [--format docx|pdf] [--tight] [--check]
"""

import argparse
import json
import re
import struct
import sys
import uuid
import zipfile
import zlib
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"
FONT_FILES = {
    (False, False): "RobotoCondensed-Regular.ttf",
    (True, False): "RobotoCondensed-Bold.ttf",
    (False, True): "RobotoCondensed-Italic.ttf",
    (True, True): "RobotoCondensed-BoldItalic.ttf",
}
FONT_FAMILY = "Roboto Condensed"

# Layout (points). A4 portrait; margins: left 3 cm, right 2.5 cm, top/bottom 1.5 cm.
MM = 72 / 25.4
PAGE_W, PAGE_H = 210 * MM, 297 * MM
MARGIN_L, MARGIN_R, MARGIN_TB = 30 * MM, 25 * MM, 15 * MM
TEXT_W = PAGE_W - MARGIN_L - MARGIN_R  # 15.5 cm
FONT_SIZE = 4.0
LINE_H = 4.0  # interline 1.0 = exactly the font size
MAX_LINES = 11
SHORT_TAIL = 5  # last strip with <= this many lines triggers shrinking
PAD = 1 * MM  # gap between text and the dashed cut box
GAP = 3 * MM  # vertical gap between strips
LABEL_W = 15 * MM  # strip number column, sits in the left margin
LABEL_SIZE = 5.0
# Word may round glyph widths; never fill a line beyond this fraction.
SAFETY = 0.98
WRAP_W = TEXT_W * SAFETY


# ---------------------------------------------------------------- fonts


class Font:
    def __init__(self, path):
        self.path = path
        self.data = path.read_bytes()
        self.ps_name = path.stem
        tables = {}
        num = struct.unpack(">H", self.data[4:6])[0]
        for i in range(num):
            tag, _, off, length = struct.unpack(">4sIII", self.data[12 + 16 * i : 28 + 16 * i])
            tables[tag.decode("latin-1")] = (off, length)
        self.tables = tables

        head = self.table("head")
        self.upem = struct.unpack(">H", head[18:20])[0]
        self.bbox = struct.unpack(">hhhh", head[36:44])
        hhea = self.table("hhea")
        self.ascent, self.descent = struct.unpack(">hh", hhea[4:8])
        n_hmetrics = struct.unpack(">H", hhea[34:36])[0]
        hmtx = self.table("hmtx")
        self.advances = [struct.unpack(">H", hmtx[4 * i : 4 * i + 2])[0] for i in range(n_hmetrics)]
        post = self.table("post")
        self.italic_angle = struct.unpack(">i", post[4:8])[0] / 65536
        os2 = self.table("OS/2")
        version = struct.unpack(">H", os2[0:2])[0]
        self.cap_height = struct.unpack(">h", os2[88:90])[0] if version >= 2 else self.ascent
        self.cmap = self._read_cmap()

    def table(self, tag):
        off, length = self.tables[tag]
        return self.data[off : off + length]

    def _read_cmap(self):
        cmap = self.table("cmap")
        num = struct.unpack(">H", cmap[2:4])[0]
        subtables = {}
        for i in range(num):
            pid, eid, off = struct.unpack(">HHI", cmap[4 + 8 * i : 12 + 8 * i])
            subtables[(pid, eid)] = off
        result = {}
        if (3, 10) in subtables:
            off = subtables[(3, 10)]
            n_groups = struct.unpack(">I", cmap[off + 12 : off + 16])[0]
            for g in range(n_groups):
                start, end, gid = struct.unpack(">III", cmap[off + 16 + 12 * g : off + 28 + 12 * g])
                for cp in range(start, end + 1):
                    result[cp] = gid + cp - start
            return result
        off = subtables[(3, 1)]
        seg2 = struct.unpack(">H", cmap[off + 6 : off + 8])[0]
        seg = seg2 // 2
        ends = struct.unpack(f">{seg}H", cmap[off + 14 : off + 14 + seg2])
        p = off + 16 + seg2
        starts = struct.unpack(f">{seg}H", cmap[p : p + seg2])
        deltas = struct.unpack(f">{seg}h", cmap[p + seg2 : p + 2 * seg2])
        ro_pos = p + 2 * seg2
        ranges = struct.unpack(f">{seg}H", cmap[ro_pos : ro_pos + seg2])
        for i in range(seg):
            for cp in range(starts[i], ends[i] + 1):
                if cp == 0xFFFF:
                    continue
                if ranges[i] == 0:
                    gid = (cp + deltas[i]) & 0xFFFF
                else:
                    gp = ro_pos + 2 * i + ranges[i] + 2 * (cp - starts[i])
                    gid = struct.unpack(">H", cmap[gp : gp + 2])[0]
                    if gid:
                        gid = (gid + deltas[i]) & 0xFFFF
                if gid:
                    result[cp] = gid
        return result

    def advance(self, gid):
        return self.advances[min(gid, len(self.advances) - 1)]

    def width(self, text, size):
        return sum(self.advance(self.cmap.get(ord(c), 0)) for c in text) * size / self.upem


FONTS = {style: Font(FONT_DIR / name) for style, name in FONT_FILES.items()}


# ---------------------------------------------------------------- markdown -> pieces

# A piece is (text, (bold, italic)). A word is a list of pieces with no spaces.

INLINE_RE = re.compile(
    r"(?P<esc>\\[\\`*_{}\[\]()#+\-.!|>])"
    r"|(?P<code>`+)(?P<code_body>.+?)(?P=code)"
    r"|(?P<img>!\[(?P<alt>[^\]]*)\]\([^)]*\))"
    r"|(?P<link>\[(?P<link_text>[^\]]*)\]\([^)]*\))"
    r"|(?P<tag></?[A-Za-z][^>]*>)"
    r"|(?P<mark>\*\*\*|\*\*|__|\*|(?<![A-Za-z0-9])_|_(?![A-Za-z0-9]))"
)


def parse_inline(text, bold=False, italic=False):
    pieces = []
    pos = 0
    for m in INLINE_RE.finditer(text):
        if m.start() > pos:
            pieces.append((text[pos : m.start()], (bold, italic)))
        pos = m.end()
        if m.group("esc"):
            pieces.append((m.group("esc")[1], (bold, italic)))
        elif m.group("code"):
            pieces.append((m.group("code_body").strip(), (bold, italic)))
        elif m.group("img") or m.group("tag"):
            pass
        elif m.group("link"):
            pieces.extend(parse_inline(m.group("link_text"), bold, italic))
        else:
            mark = m.group("mark")
            if mark == "***":
                bold, italic = not bold, not italic
            elif mark in ("**", "__"):
                bold = not bold
            else:
                italic = not italic
    if pos < len(text):
        pieces.append((text[pos:], (bold, italic)))
    return pieces


def parse_blocks(source):
    """Return a list of (kind, text): kind is heading | item | para."""
    blocks = []
    para = []
    fence = None

    def flush():
        if para:
            blocks.append(("para", " ".join(para)))
            para.clear()

    for raw in source.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if fence:
            if stripped.startswith(fence):
                fence = None
                flush()
            elif stripped:
                para.append("`" + stripped.replace("`", "") + "`")
            continue
        m = re.match(r"(`{3,}|~{3,})", stripped)
        if m:
            flush()
            fence = m.group(1)
            continue
        if not stripped:
            flush()
            continue
        if re.fullmatch(r"=+|-+", stripped) and para:
            text = " ".join(para)
            para.clear()
            blocks.append(("heading", text))
            continue
        if re.fullmatch(r"(\*\s*){3,}|(-\s*){3,}|(_\s*){3,}", stripped):
            flush()
            continue
        while stripped.startswith(">"):
            stripped = stripped[1:].strip()
        if not stripped:
            continue
        m = re.match(r"(#{1,6})\s+(.*?)\s*#*$", stripped)
        if m:
            flush()
            blocks.append(("heading", m.group(2)))
            continue
        if stripped.startswith("|"):
            flush()
            if re.fullmatch(r"\|?[\s:|-]+\|?", stripped):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            blocks.append(("para", " | ".join(c for c in cells if c)))
            continue
        m = re.match(r"([-*+]|\d+[.)])\s+(.*)", stripped)
        if m:
            flush()
            marker = "•" if m.group(1) in "-*+" else m.group(1)
            blocks.append(("item", f"{marker} {m.group(2)}"))
            continue
        if blocks and blocks[-1][0] == "item" and not para and raw[:1] in (" ", "\t"):
            blocks[-1] = ("item", blocks[-1][1] + " " + stripped)
            continue
        para.append(stripped)
    flush()
    return blocks


def to_pieces(blocks):
    pieces = []
    after_heading = True
    for kind, text in blocks:
        if kind == "heading":
            if pieces:
                pieces.append((" | ", (False, False)))
            head = [(t, (True, it)) for t, (_, it) in parse_inline(text)]
            last = "".join(t for t, _ in head).rstrip()
            if last and last[-1] not in ".:!?":
                head.append((":", (True, False)))
            pieces.extend(head)
            pieces.append((" ", (False, False)))
            after_heading = True
            continue
        if pieces and not after_heading:
            pieces.append((" " if kind == "item" else " | ", (False, False)))
        pieces.extend(parse_inline(text))
        after_heading = False
    return pieces


# Common note symbols missing from Roboto Condensed, with ASCII stand-ins.
SUBSTITUTES = {
    "→": "->", "←": "<-", "↔": "<->", "⇒": "=>", "⇐": "<=", "⇔": "<=>",
    "✓": "v", "✔": "v", "✗": "x", "✘": "x",
}


def sanitize(pieces):
    """Replace characters the font lacks; return (pieces, unsupported chars)."""
    missing = set()
    out = []
    for text, style in pieces:
        cmap = FONTS[style].cmap
        chars = []
        for c in text:
            if c.isspace():
                chars.append(" ")
            elif ord(c) in cmap:
                chars.append(c)
            elif c in SUBSTITUTES:
                chars.append(SUBSTITUTES[c])
            else:
                missing.add(c)
                chars.append("?")
        out.append(("".join(chars), style))
    return out, sorted(missing)


def to_words(pieces, tight):
    words = []
    current = []
    for text, style in pieces:
        parts = text.split(" ")
        for i, part in enumerate(parts):
            if i > 0 and current:
                words.append(current)
                current = []
            if part:
                current.append((part, style))
    if current:
        words.append(current)
    if tight:
        # Glue standalone separators to the following word: saves a space each.
        glued = []
        for word in words:
            if glued and "".join(t for t, _ in glued[-1]) in ("|", "•"):
                glued[-1] = glued[-1] + word
            else:
                glued.append(word)
        words = glued
    return words


# ---------------------------------------------------------------- line breaking


def word_width(word):
    return sum(FONTS[style].width(text, FONT_SIZE) for text, style in word)


def space_style(word):
    return word[-1][1]


def space_width(word):
    return FONTS[space_style(word)].width(" ", FONT_SIZE)


def split_long(word):
    """Break a word wider than a line into line-sized chunks."""
    chunks, current, width = [], [], 0.0
    for text, style in word:
        for c in text:
            w = FONTS[style].width(c, FONT_SIZE)
            if current and width + w > WRAP_W:
                chunks.append(current)
                current, width = [], 0.0
            if current and current[-1][1] == style:
                current[-1] = (current[-1][0] + c, style)
            else:
                current.append((c, style))
            width += w
    if current:
        chunks.append(current)
    return chunks


def break_lines(words):
    lines, line, width = [], [], 0.0
    for word in words:
        ww = word_width(word)
        if ww > WRAP_W:
            if line:
                lines.append(line)
            chunks = split_long(word)
            lines.extend([c] for c in chunks[:-1])
            line, width = [chunks[-1]], word_width(chunks[-1])
            continue
        extra = space_width(line[-1]) + ww if line else ww
        if line and width + extra > WRAP_W:
            lines.append(line)
            line, width = [word], ww
        else:
            line.append(word)
            width += extra
    if line:
        lines.append(line)
    return lines


def line_runs(line):
    """Flatten a line (list of words) into merged (text, style) runs with spaces."""
    runs = []
    for i, word in enumerate(line):
        seq = list(word)
        if i < len(line) - 1:
            seq.append((" ", space_style(word)))
        for text, style in seq:
            if runs and runs[-1][1] == style:
                runs[-1] = (runs[-1][0] + text, style)
            else:
                runs.append((text, style))
    return runs


# ---------------------------------------------------------------- DOCX

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def twips(pt):
    return round(pt * 20)


def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def docx_run(text, style, extra_rpr=""):
    bold, italic = style
    rpr = ("<w:b/><w:bCs/>" if bold else "") + ("<w:i/><w:iCs/>" if italic else "") + extra_rpr
    rpr = f"<w:rPr>{rpr}</w:rPr>" if rpr else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{esc(text)}</w:t></w:r>'


def docx_strip(lines, index, total, final):
    label_w, pad = twips(LABEL_W), twips(PAD)
    box_w = twips(TEXT_W) + 2 * pad
    line_h = twips(LINE_H)
    height = len(lines) * line_h + 2 * pad
    body = []
    for i, line in enumerate(lines):
        body.extend(docx_run(t, s) for t, s in line_runs(line))
        # A break after every line, except the very last line of the sheet: a line
        # ending in a break is justified by Word, and the empty line it opens after
        # line 11 is clipped by the exact row height.
        if not (final and i == len(lines) - 1):
            body.append("<w:r><w:br/></w:r>")
    gray = '<w:color w:val="808080"/>'
    small = f'<w:sz w:val="{round(LABEL_SIZE * 2)}"/><w:szCs w:val="{round(LABEL_SIZE * 2)}"/>'
    label = (
        f'<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="{twips(LABEL_SIZE * 1.2)}" w:lineRule="exact"/></w:pPr>'
        + docx_run("✂", (False, False), '<w:rFonts w:ascii="Segoe UI Symbol" w:hAnsi="Segoe UI Symbol"/>' + gray + small)
        + docx_run(f" {index}/{total}", (False, False), gray + small)
        + "</w:p>"
    )
    dashed = '<w:{0} w:val="dashed" w:sz="4" w:space="0" w:color="808080"/>'
    borders = "".join(dashed.format(side) for side in ("top", "left", "bottom", "right"))
    nil = "".join(f'<w:{side} w:val="nil"/>' for side in ("top", "left", "bottom", "right"))
    margins = "".join(f'<w:{side} w:w="{pad}" w:type="dxa"/>' for side in ("top", "left", "bottom", "right"))
    zero = "".join(f'<w:{side} w:w="0" w:type="dxa"/>' for side in ("top", "left", "bottom", "right"))
    return (
        "<w:tbl><w:tblPr>"
        f'<w:tblW w:w="{label_w + box_w}" w:type="dxa"/>'
        f'<w:tblInd w:w="{-(label_w + pad)}" w:type="dxa"/>'
        f'<w:tblLayout w:type="fixed"/><w:tblCellMar>{zero}</w:tblCellMar>'
        f"</w:tblPr><w:tblGrid><w:gridCol w:w=\"{label_w}\"/><w:gridCol w:w=\"{box_w}\"/></w:tblGrid>"
        f'<w:tr><w:trPr><w:cantSplit/><w:trHeight w:val="{height}" w:hRule="exact"/></w:trPr>'
        f'<w:tc><w:tcPr><w:tcW w:w="{label_w}" w:type="dxa"/><w:tcBorders>{nil}</w:tcBorders></w:tcPr>{label}</w:tc>'
        f'<w:tc><w:tcPr><w:tcW w:w="{box_w}" w:type="dxa"/><w:tcBorders>{borders}</w:tcBorders>'
        f"<w:tcMar>{margins}</w:tcMar></w:tcPr>"
        f'<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="{line_h}" w:lineRule="exact"/>'
        f'<w:jc w:val="both"/></w:pPr>{"".join(body)}</w:p></w:tc></w:tr></w:tbl>'
        f'<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="{twips(GAP)}" w:lineRule="exact"/></w:pPr></w:p>'
    )


def obfuscate(font_bytes, key):
    data = bytearray(font_bytes)
    guid = bytes.fromhex(key.strip("{}").replace("-", ""))[::-1]
    for i in range(32):
        data[i] ^= guid[i % 16]
    return bytes(data)


def write_docx(strips, out):
    body = "".join(
        docx_strip(lines, i + 1, len(strips), i == len(strips) - 1) for i, lines in enumerate(strips)
    )
    sect = (
        f'<w:sectPr><w:pgSz w:w="{twips(PAGE_W)}" w:h="{twips(PAGE_H)}"/>'
        f'<w:pgMar w:top="{twips(MARGIN_TB)}" w:right="{twips(MARGIN_R)}" w:bottom="{twips(MARGIN_TB)}" '
        f'w:left="{twips(MARGIN_L)}" w:header="0" w:footer="0" w:gutter="0"/></w:sectPr>'
    )
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}"><w:body>{body}{sect}</w:body></w:document>'
    )
    half_points = round(FONT_SIZE * 2)
    styles = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="{W_NS}">'
        "<w:docDefaults><w:rPrDefault><w:rPr>"
        f'<w:rFonts w:ascii="{FONT_FAMILY}" w:hAnsi="{FONT_FAMILY}" w:cs="{FONT_FAMILY}" w:eastAsia="{FONT_FAMILY}"/>'
        f'<w:sz w:val="{half_points}"/><w:szCs w:val="{half_points}"/>'
        "</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>"
        f'<w:spacing w:before="0" w:after="0" w:line="{twips(LINE_H)}" w:lineRule="exact"/>'
        "</w:pPr></w:pPrDefault></w:docDefaults>"
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
        "</w:styles>"
    )
    settings = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:settings xmlns:w="{W_NS}">'
        "<w:embedTrueTypeFonts/><w:compat>"
        '<w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/>'
        "</w:compat></w:settings>"
    )
    embed_tags = {
        (False, False): "embedRegular",
        (True, False): "embedBold",
        (False, True): "embedItalic",
        (True, True): "embedBoldItalic",
    }
    font_parts, embeds, font_rels = {}, [], []
    for n, (style, tag) in enumerate(embed_tags.items(), start=1):
        key = "{" + str(uuid.uuid4()).upper() + "}"
        font_parts[f"word/fonts/font{n}.odttf"] = obfuscate(FONTS[style].data, key)
        embeds.append(f'<w:{tag} r:id="rIdFont{n}" w:fontKey="{key}"/>')
        font_rels.append(f'<Relationship Id="rIdFont{n}" Type="{REL_TYPE}font" Target="fonts/font{n}.odttf"/>')
    font_table = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:fonts xmlns:w="{W_NS}" xmlns:r="{R_NS}">'
        f'<w:font w:name="{FONT_FAMILY}"><w:charset w:val="00"/><w:family w:val="swiss"/>'
        f'<w:pitch w:val="variable"/>{"".join(embeds)}</w:font></w:fonts>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="odttf" ContentType="application/vnd.openxmlformats-officedocument.obfuscatedFont"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/settings.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
        '<Override PartName="/word/fontTable.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"/>'
        "</Types>"
    )
    rels = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{REL_NS}">'
        f'<Relationship Id="rId1" Type="{REL_TYPE}officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    doc_rels = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{REL_NS}">'
        f'<Relationship Id="rId1" Type="{REL_TYPE}styles" Target="styles.xml"/>'
        f'<Relationship Id="rId2" Type="{REL_TYPE}settings" Target="settings.xml"/>'
        f'<Relationship Id="rId3" Type="{REL_TYPE}fontTable" Target="fontTable.xml"/>'
        "</Relationships>"
    )
    font_table_rels = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{REL_NS}">'
        f'{"".join(font_rels)}</Relationships>'
    )
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/settings.xml", settings)
        z.writestr("word/fontTable.xml", font_table)
        z.writestr("word/_rels/fontTable.xml.rels", font_table_rels)
        for name, data in font_parts.items():
            z.writestr(name, data)


# ---------------------------------------------------------------- PDF


class PdfFontUse:
    def __init__(self, font):
        self.font = font
        self.used = {}  # gid -> char

    def encode(self, text):
        out = []
        for c in text:
            gid = self.font.cmap.get(ord(c), 0)
            self.used.setdefault(gid, c)
            out.append(f"{gid:04X}")
        return "<" + "".join(out) + ">"


def pdf_num(x):
    return f"{x:.3f}".rstrip("0").rstrip(".")


def circle(cx, cy, r):
    k = 0.5523 * r
    return (
        f"{pdf_num(cx + r)} {pdf_num(cy)} m "
        f"{pdf_num(cx + r)} {pdf_num(cy + k)} {pdf_num(cx + k)} {pdf_num(cy + r)} {pdf_num(cx)} {pdf_num(cy + r)} c "
        f"{pdf_num(cx - k)} {pdf_num(cy + r)} {pdf_num(cx - r)} {pdf_num(cy + k)} {pdf_num(cx - r)} {pdf_num(cy)} c "
        f"{pdf_num(cx - r)} {pdf_num(cy - k)} {pdf_num(cx - k)} {pdf_num(cy - r)} {pdf_num(cx)} {pdf_num(cy - r)} c "
        f"{pdf_num(cx + k)} {pdf_num(cy - r)} {pdf_num(cx + r)} {pdf_num(cy - k)} {pdf_num(cx + r)} {pdf_num(cy)} c S"
    )


def scissors(x, y):
    """Small vector scissors, bottom-left at (x, y), about 5 x 4 pt."""
    return (
        "q 0.4 w 0.5 G [] 0 d "
        + circle(x + 0.9, y + 0.9, 0.7) + " " + circle(x + 0.9, y + 3.1, 0.7) + " "
        + f"{pdf_num(x + 1.5)} {pdf_num(y + 1.3)} m {pdf_num(x + 5)} {pdf_num(y + 3.2)} l S "
        + f"{pdf_num(x + 1.5)} {pdf_num(y + 2.7)} m {pdf_num(x + 5)} {pdf_num(y + 0.8)} l S Q"
    )


def write_pdf(strips, out):
    uses = {style: PdfFontUse(font) for style, font in FONTS.items()}
    names = {style: f"F{i}" for i, style in enumerate(FONTS)}
    regular = (False, False)
    baseline = LINE_H * FONTS[regular].ascent / (FONTS[regular].ascent - FONTS[regular].descent)

    pages, ops = [], []
    y = PAGE_H - MARGIN_TB
    for idx, lines in enumerate(strips):
        final = idx == len(strips) - 1
        box_h = len(lines) * LINE_H + 2 * PAD
        if y - box_h < MARGIN_TB and ops:
            pages.append(ops)
            ops, y = [], PAGE_H - MARGIN_TB
        top = y
        x0 = MARGIN_L - PAD
        ops.append(
            f"q 0.25 w 0.5 G [1.5 1.5] 0 d {pdf_num(x0)} {pdf_num(top - box_h)} "
            f"{pdf_num(TEXT_W + 2 * PAD)} {pdf_num(box_h)} re S Q"
        )
        label_x = x0 - LABEL_W
        ops.append(scissors(label_x, top - 4.5))
        ops.append(
            f"BT /{names[regular]} {pdf_num(LABEL_SIZE)} Tf 0.5 g {pdf_num(label_x + 6.5)} {pdf_num(top - 4.2)} Td "
            f"{uses[regular].encode(f'{idx + 1}/{len(strips)}')} Tj ET"
        )
        for i, line in enumerate(lines):
            runs = line_runs(line)
            natural = sum(FONTS[s].width(t, FONT_SIZE) for t, s in runs)
            spaces = sum(t.count(" ") for t, _ in runs)
            last = final and i == len(lines) - 1
            extra = 0.0 if last or spaces == 0 else (TEXT_W - natural) / spaces
            adjust = pdf_num(-extra * 1000 / FONT_SIZE)
            ly = top - PAD - i * LINE_H - baseline
            parts = [f"BT 0 g {pdf_num(MARGIN_L)} {pdf_num(ly)} Td"]
            for text, style in runs:
                items = []
                for k, chunk in enumerate(text.split(" ")):
                    if k > 0:
                        items.append(uses[style].encode(" ") + (f" {adjust}" if extra else ""))
                    if chunk:
                        items.append(uses[style].encode(chunk))
                parts.append(f"/{names[style]} {pdf_num(FONT_SIZE)} Tf [{' '.join(items)}] TJ")
            parts.append("ET")
            ops.append(" ".join(parts))
        y = top - box_h - GAP
    pages.append(ops)

    objects = []  # index i -> object number i + 1

    def add(body):
        objects.append(body)
        return len(objects)

    def stream(data, extra=""):
        comp = zlib.compress(data)
        return f"<< /Length {len(comp)} /Filter /FlateDecode {extra}>>\nstream\n".encode() + comp + b"\nendstream"

    font_refs = {}
    for style, use in uses.items():
        if not use.used:
            continue
        f = use.font
        scale = 1000 / f.upem
        file_ref = add(stream(f.data, f"/Length1 {len(f.data)} "))
        flags = 32 | (64 if style[1] else 0)
        bbox = " ".join(str(round(v * scale)) for v in f.bbox)
        desc_ref = add(
            f"<< /Type /FontDescriptor /FontName /{f.ps_name} /Flags {flags} /FontBBox [{bbox}] "
            f"/ItalicAngle {pdf_num(f.italic_angle)} /Ascent {round(f.ascent * scale)} "
            f"/Descent {round(f.descent * scale)} /CapHeight {round(f.cap_height * scale)} "
            f"/StemV {120 if style[0] else 80} /FontFile2 {file_ref} 0 R >>".encode()
        )
        gids = sorted(use.used)
        widths = " ".join(f"{g} [{round(f.advance(g) * scale)}]" for g in gids)
        cid_ref = add(
            f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{f.ps_name} "
            f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            f"/FontDescriptor {desc_ref} 0 R /CIDToGIDMap /Identity /DW 1000 /W [{widths}] >>".encode()
        )
        bf = []
        for start in range(0, len(gids), 100):
            chunk = gids[start : start + 100]
            entries = "\n".join(
                f"<{g:04X}> <{use.used[g].encode('utf-16-be').hex().upper()}>" for g in chunk
            )
            bf.append(f"{len(chunk)} beginbfchar\n{entries}\nendbfchar")
        cmap = (
            "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /Adobe-Identity-UCS def /CMapType 2 def\n"
            "1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            + "\n".join(bf)
            + "\nendcmap CMapName currentdict /CMap defineresource pop end end"
        )
        tu_ref = add(stream(cmap.encode()))
        font_refs[style] = add(
            f"<< /Type /Font /Subtype /Type0 /BaseFont /{f.ps_name} /Encoding /Identity-H "
            f"/DescendantFonts [{cid_ref} 0 R] /ToUnicode {tu_ref} 0 R >>".encode()
        )

    font_dict = " ".join(f"/{names[s]} {ref} 0 R" for s, ref in font_refs.items())
    pages_ref = len(objects) + 1 + 2 * len(pages)  # reserved: after all page/content objects
    page_refs = []
    for ops_list in pages:
        content_ref = add(stream("\n".join(ops_list).encode("latin-1")))
        page_refs.append(
            add(
                f"<< /Type /Page /Parent {pages_ref} 0 R /MediaBox [0 0 {pdf_num(PAGE_W)} {pdf_num(PAGE_H)}] "
                f"/Resources << /Font << {font_dict} >> >> /Contents {content_ref} 0 R >>".encode()
            )
        )
    kids = " ".join(f"{r} 0 R" for r in page_refs)
    assert add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_refs)} >>".encode()) == pages_ref
    catalog_ref = add(f"<< /Type /Catalog /Pages {pages_ref} 0 R >>".encode())

    buf = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(buf)
    buf += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    buf += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    buf += f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_ref} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    Path(out).write_bytes(buf)


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="notes file (.md/.txt), or - for stdin")
    ap.add_argument("-o", "--output", help="output path (default: <input>-cheatsheet.<format>)")
    ap.add_argument("--format", choices=("docx", "pdf"), help="default: from -o extension, else docx")
    ap.add_argument("--tight", action="store_true", help="glue | and • separators to the next word")
    ap.add_argument("--check", action="store_true", help="only print the JSON summary, write nothing")
    args = ap.parse_args()

    if args.input == "-":
        source = sys.stdin.read()
    else:
        source = Path(args.input).read_text(encoding="utf-8")

    fmt = args.format
    if not fmt and args.output and Path(args.output).suffix.lower() in (".docx", ".pdf"):
        fmt = Path(args.output).suffix.lower()[1:]
    fmt = fmt or "docx"
    if args.output:
        out = Path(args.output)
    elif args.input == "-":
        out = Path(f"cheatsheet.{fmt}")
    else:
        src = Path(args.input)
        out = src.with_name(f"{src.stem}-cheatsheet.{fmt}")

    pieces, missing = sanitize(to_pieces(parse_blocks(source)))
    lines = break_lines(to_words(pieces, args.tight))
    strips = [lines[i : i + MAX_LINES] for i in range(0, len(lines), MAX_LINES)] or [[]]
    last = len(strips[-1])
    shrink = len(strips) > 1 and last <= SHORT_TAIL
    chars = [sum(len(t) for t, _ in line_runs(line)) for line in lines]

    summary = {
        "output": None if args.check else str(out),
        "format": fmt,
        "strips": len(strips),
        "lines_per_strip": [len(s) for s in strips],
        "total_lines": len(lines),
        "last_strip_lines": last,
        "shrink_needed": shrink,
        "lines_to_cut": last if shrink else 0,
        "avg_chars_per_line": round(sum(chars) / len(chars)) if chars else 0,
        "unsupported_chars": missing,
    }
    if not args.check:
        (write_pdf if fmt == "pdf" else write_docx)(strips, out)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
