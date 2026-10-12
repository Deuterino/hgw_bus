"""Convert the Greifswald Stadtbus timetable PDFs to one tidy CSV per line.

Each CSV row is one stop of one trip:
    line, day, direction, trip_id, seq, stop, time, note, page

day is week / sat / sun (sun = Sonn- und Feiertage). trip_id encodes where the
trip came from in the PDF (p<page>t<table>c<column>), seq is the row of the stop
in that table. Stops a trip does not serve are simply absent.

Needs pdfplumber. Usage:
    python bus_pdf_to_csv.py [pdf_dir] [csv_dir]
"""
import csv
import re
import sys
from pathlib import Path

import pdfplumber

PDF_DIR = Path(__file__).parent  # the Linie_*.pdf sit next to this script, the CSVs go to csv/
FIELDS = ["line", "day", "direction", "trip_id", "seq", "stop", "time", "note", "page"]

TIME_RE = re.compile(r"^\d\d:\d\d$")
SKIP_TOKENS = {"...", ":"}  # printed where a trip does not serve the stop
DAY_KEYS = {"montagfreitag": "week", "samstag": "sat", "sonnundfeiertage": "sun", "sonntag": "sun"}
LEGEND_RE = re.compile(r"^([A-Z])\s+(verkehrt\b.*)$")


def day_key(text):
    return DAY_KEYS.get(re.sub(r"[\s\-–]", "", text).lower())


def cluster_lines(words, tol=2.5):
    """Group words into text lines by their vertical centre."""
    lines = []
    for w in sorted(words, key=lambda w: (w["top"] + w["bottom"]) / 2):
        yc = (w["top"] + w["bottom"]) / 2
        if lines and abs(yc - lines[-1]["yc"]) <= tol:
            lines[-1]["words"].append(w)
        else:
            lines.append({"yc": yc, "words": [w]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w["x0"])
    return lines


def column_edges(table):
    """x-ranges of the table columns, taken from the row with the most cells."""
    best = max(table.rows, key=lambda r: sum(c is not None for c in r.cells))
    cells = sorted((c for c in best.cells if c is not None), key=lambda c: c[0])
    return [(c[0], c[2]) for c in cells]


def column_of(word, edges):
    xc = (word["x0"] + word["x1"]) / 2
    for i, (x0, x1) in enumerate(edges):
        if x0 <= xc <= x1:
            return i
    return None


def page_legends(page):
    legends = {}
    for ln in (page.extract_text() or "").splitlines():
        m = LEGEND_RE.match(ln.strip())
        if m:
            if legends.setdefault(m.group(1), m.group(2)) != m.group(2):
                raise ValueError(f"conflicting footnote {m.group(1)} on one page")
    return legends


def parse_table(page, table, page_lines):
    """Return (stops, columns): stops = row names, columns = {col: {day, note, times{row: time}}}"""
    edges = column_edges(table)
    x0, top, x1, bottom = table.bbox
    words = [w for w in page.extract_words(x_tolerance=1.5)
             if x0 - 1 <= (w["x0"] + w["x1"]) / 2 <= x1 + 1 and top - 1 <= (w["top"] + w["bottom"]) / 2 <= bottom + 1]

    # day type per column: from header cells inside the table, else the nearest day line above it
    day_spans = []
    for row in table.rows:
        for c in row.cells:
            if c is None:
                continue
            key = day_key(page.crop(c).extract_text() or "")
            if key:
                day_spans.append((c[0], c[2], key))
    if day_spans:
        # a second day type (Samstag | Sonn-und Feiertage) can sit in a header cell that has no
        # border of its own: its label lies right of the detected spans and takes the rest of the table
        covered = max(b for _, b, _ in day_spans)
        for ln in cluster_lines(words):
            texts = [w["text"] for w in ln["words"]]
            if any(TIME_RE.match(t) or t in SKIP_TOKENS for t in texts):
                continue
            rest = [w["text"] for w in ln["words"] if (w["x0"] + w["x1"]) / 2 > covered]
            if rest and day_key(" ".join(rest)):
                day_spans.append((covered, x1, day_key(" ".join(rest))))
                break
    else:
        above = [ln for ln in page_lines if ln["yc"] < top and day_key(" ".join(w["text"] for w in ln["words"]))]
        if not above:
            raise ValueError("no day type found for table")
        day_spans = [(x0, x1, day_key(" ".join(w["text"] for w in above[-1]["words"])))]

    stops, columns = [], {}
    notes = {}
    for ln in cluster_lines(words):
        cells = {}
        for w in ln["words"]:
            col = column_of(w, edges)
            if col is None:
                raise ValueError(f"word {w['text']!r} outside all columns")
            cells.setdefault(col, []).append(w["text"])
        name = cells.pop(0, [])
        if name and name[-1] in ("ab", "an"):
            name = name[:-1]
        name = " ".join(name)
        if not cells and (not name or day_key(name) or name == "Fußnote"):
            continue
        tokens = [t for c in sorted(cells) for t in cells[c]]
        if day_key(" ".join(name.split() + tokens)):
            continue  # day header row
        if not name and not any(TIME_RE.match(t) or t in SKIP_TOKENS for t in tokens):
            continue  # header row with several day types side by side
        if name == "Fußnote":
            for col, toks in cells.items():
                notes[col] = " ".join(toks)
            continue
        if not name:
            raise ValueError(f"row without stop name: {cells}")
        row = len(stops)
        stops.append(name)
        for col, toks in cells.items():
            if len(toks) != 1:
                raise ValueError(f"{name}: several tokens in one cell {toks}")
            tok = toks[0]
            if tok in SKIP_TOKENS:
                continue
            if not TIME_RE.match(tok):
                raise ValueError(f"{name}: unexpected cell content {tok!r}")
            columns.setdefault(col, {})[row] = tok

    out = {}
    for col, times in columns.items():
        xc = sum(edges[col]) / 2
        days = [k for a, b, k in day_spans if a <= xc < b]
        if len(days) != 1:
            raise ValueError(f"column {col}: cannot assign day type ({days})")
        out[col] = {"day": days[0], "note": notes.get(col, ""), "times": times}
    for col in notes:
        if col not in out:
            raise ValueError(f"footnote in column {col} without times")
    return stops, out


def convert_pdf(pdf_path):
    line = pdf_path.name.split("_")[1]
    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            legends = page_legends(page)
            page_lines = cluster_lines(page.extract_words(x_tolerance=1.5))
            tables = sorted(page.find_tables(), key=lambda t: (round(t.bbox[1]), t.bbox[0]))
            for tno, table in enumerate(tables, start=1):
                stops, columns = parse_table(page, table, page_lines)
                direction = f"{stops[0]} -> {stops[-1]}"
                for col in sorted(columns):
                    c = columns[col]
                    note = c["note"]
                    if note:
                        note = "; ".join(f"{k}: {legends[k]}" for k in note.split())
                    trip_id = f"{line}-{c['day']}-p{pno}t{tno}c{col:02d}"
                    for row in sorted(c["times"]):
                        rows.append({"line": line, "day": c["day"], "direction": direction, "trip_id": trip_id,
                                     "seq": row, "stop": stops[row], "time": c["times"][row], "note": note,
                                     "page": pno})
    return line, rows


def main(pdf_dir=PDF_DIR, csv_dir=None):
    pdf_dir = Path(pdf_dir)
    csv_dir = Path(csv_dir) if csv_dir else pdf_dir / "csv"
    csv_dir.mkdir(exist_ok=True)
    for pdf_path in sorted(pdf_dir.glob("Linie_*.pdf")):
        line, rows = convert_pdf(pdf_path)
        out = csv_dir / f"linie_{line}.csv"
        with open(out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        trips = len({r["trip_id"] for r in rows})
        print(f"{pdf_path.name} -> {out.name}: {trips} trips, {len(rows)} stop times")


if __name__ == "__main__":
    main(*sys.argv[1:3])
