#!/usr/bin/env python3
"""A tiny, dependency-free .xlsx writer -- just enough for BallSheet's export.

An .xlsx file is a zip of XML parts.  This writes the handful of parts Excel,
LibreOffice and Google Sheets need to open a plain multi-sheet grid of numbers
and text, with an optional bold header row.  No formulas, no styling beyond
bold, no dates.

    wb = Workbook()
    ws = wb.add_sheet("1998")
    ws.append(["Player", "HR"], header=True)
    ws.append(["Aaron Judge", 62])
    wb.save("out.xlsx")
"""
from __future__ import annotations

import datetime as _dt
import re
import zipfile

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
{sheets}
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

# font 0 = normal, font 1 = bold; cellXfs 0 = normal, 1 = bold.
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

_INVALID_SHEET = re.compile(r"[\[\]:*?/\\]")


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _col_letter(idx: int) -> str:
    idx += 1
    out = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


class Sheet:
    def __init__(self, name: str):
        self.name = name
        self._rows: list[tuple[list, bool]] = []

    def append(self, values, header: bool = False) -> None:
        self._rows.append((list(values), header))

    def _xml(self) -> str:
        body = []
        for r, (values, header) in enumerate(self._rows, start=1):
            cells = []
            style = ' s="1"' if header else ""
            for c, value in enumerate(values):
                ref = f"{_col_letter(c)}{r}"
                if isinstance(value, bool) or value is None or value == "":
                    cells.append(f'<c r="{ref}"{style}/>')
                elif isinstance(value, (int, float)):
                    cells.append(f'<c r="{ref}"{style}><v>{value!r}</v></c>')
                else:
                    cells.append(
                        f'<c r="{ref}"{style} t="inlineStr">'
                        f"<is><t xml:space=\"preserve\">{_esc(str(value))}</t></is></c>"
                    )
            body.append(f'<row r="{r}">{"".join(cells)}</row>')
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(body)}</sheetData></worksheet>'
        )


class Workbook:
    def __init__(self):
        self._sheets: list[Sheet] = []
        self._used_names: set[str] = set()

    def add_sheet(self, name: str) -> Sheet:
        clean = _INVALID_SHEET.sub("-", str(name)).strip() or "Sheet"
        clean = clean[:31]
        base, n = clean, 2
        while clean.lower() in self._used_names:
            suffix = f" ({n})"
            clean = base[:31 - len(suffix)] + suffix
            n += 1
        self._used_names.add(clean.lower())
        sheet = Sheet(clean)
        self._sheets.append(sheet)
        return sheet

    def save(self, path) -> None:
        if not self._sheets:
            self.add_sheet("Sheet1")

        overrides = "\n".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(self._sheets) + 1)
        )
        sheet_tags = "".join(
            f'<sheet name="{_esc(s.name)}" sheetId="{i}" r:id="rId{i}"/>'
            for i, s in enumerate(self._sheets, start=1)
        )
        styles_rid = len(self._sheets) + 1
        wb_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{sheet_tags}</sheets></workbook>"
        )
        rels = ["<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
        for i in range(1, len(self._sheets) + 1):
            rels.append(
                f'<Relationship Id="rId{i}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{i}.xml"/>'
            )
        rels.append(
            f'<Relationship Id="rId{styles_rid}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>'
        )
        rels.append("</Relationships>")

        now = _dt.datetime.now().replace(microsecond=0)
        zinfo_date = (now.year, now.month, now.day, now.hour, now.minute, now.second)

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            def write(name, text):
                info = zipfile.ZipInfo(name, date_time=zinfo_date)
                zf.writestr(info, text)

            write("[Content_Types].xml", _CONTENT_TYPES.format(sheets=overrides))
            write("_rels/.rels", _ROOT_RELS)
            write("xl/workbook.xml", wb_xml)
            write("xl/_rels/workbook.xml.rels", "\n".join(rels))
            write("xl/styles.xml", _STYLES)
            for i, sheet in enumerate(self._sheets, start=1):
                write(f"xl/worksheets/sheet{i}.xml", sheet._xml())
