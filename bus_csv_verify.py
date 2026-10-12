"""Check the timetable CSVs written by bus_pdf_to_csv.py against the PDFs.

The converter places times by table geometry; this check deliberately does not.
It re-reads every page as plain text lines and compares:
  1. every stop row: name and left-to-right list of times        (values, row order)
  2. rows without blank cells: i-th printed cell <-> i-th column  (column alignment)
  3. number of time tokens on the page <-> number of CSV rows     (nothing lost/invented)
  4. day type of the page header <-> day column
  5. number of footnote marks <-> number of trips with a note
  6. every trip runs forward in time without implausible jumps

Rows that do contain blank cells get their column from geometry alone; they are
listed so they can be compared with the PDF by eye.

Usage:
    python bus_csv_verify.py [pdf_dir] [csv_dir]
"""
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import pdfplumber

PDF_DIR = Path(__file__).parent  # the Linie_*.pdf sit next to this script, the CSVs are in csv/
TIME_RE = re.compile(r"^\d\d:\d\d$")
SKIP_TOKENS = {"...", ":"}
DAY_KEYS = {"montagfreitag": "week", "samstag": "sat", "sonnundfeiertage": "sun", "sonntag": "sun"}
DAY_RE = re.compile(r"Montag\s*-\s*Freitag|Samstag|Sonn-\s*und\s*Feiertage|Sonntag")
MAX_GAP = 10  # minutes between consecutive served stops before a trip is flagged


def minutes(t):
    return int(t[:2]) * 60 + int(t[3:])


def text_streams(page):
    """Stop rows of a page from plain text. stream k = k-th (name, cells) segment of each line."""
    streams = defaultdict(list)
    for ln in (page.extract_text() or "").splitlines():
        segments, name, cells = [], [], []
        for tok in ln.split():
            is_cell = bool(TIME_RE.match(tok)) or tok in SKIP_TOKENS
            if is_cell:
                cells.append(tok)
            else:
                if cells:
                    segments.append((name, cells))
                    name, cells = [], []
                name.append(tok)
        if cells:
            segments.append((name, cells))
        for k, (name, cells) in enumerate(segments):
            if name and name[-1] in ("ab", "an"):
                name = name[:-1]
            if any(c not in SKIP_TOKENS for c in cells):  # rows no trip serves are not in the csv
                streams[k].append((" ".join(name), cells))
    return [streams[k] for k in sorted(streams)]


def verify_line(pdf_path, csv_path):
    errors, blanks = [], []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    by_page = defaultdict(list)
    for r in rows:
        by_page[int(r["page"])].append(r)

    with pdfplumber.open(pdf_path) as pdf:
        if set(by_page) != set(range(1, len(pdf.pages) + 1)):
            errors.append(f"pages in csv {sorted(by_page)} != pages in pdf 1..{len(pdf.pages)}")
        for pno, page in enumerate(pdf.pages, start=1):
            where = f"{pdf_path.name} p{pno}"
            text = page.extract_text() or ""
            prows = by_page.get(pno, [])

            # 3. token count
            n_text = len(re.findall(r"(?<![\d:])\d\d:\d\d(?![\d:])", text))
            if n_text != len(prows):
                errors.append(f"{where}: {n_text} times in pdf, {len(prows)} in csv")

            # tables as written in the csv: table -> seq -> {col: time}
            tables = defaultdict(lambda: defaultdict(dict))
            names = {}
            for r in prows:
                tno, col = re.search(r"t(\d+)c(\d+)$", r["trip_id"]).groups()
                tables[int(tno)][int(r["seq"])][int(col)] = r["time"]
                if names.setdefault((int(tno), int(r["seq"])), r["stop"]) != r["stop"]:
                    errors.append(f"{where}: two stop names for one table row")

            # 1. + 2. rows
            streams = text_streams(page)
            if len(streams) == 1:
                groups = [sorted(tables)]
            elif len(streams) == len(tables):
                groups = [[t] for t in sorted(tables)]
            else:
                errors.append(f"{where}: {len(streams)} text streams vs {len(tables)} tables")
                continue
            for stream, group in zip(streams, groups):
                expected = [(t, seq) for t in group for seq in sorted(tables[t])]
                if len(stream) != len(expected):
                    errors.append(f"{where}: {len(stream)} stop rows in pdf, {len(expected)} in csv")
                    continue
                for (name, cells), (t, seq) in zip(stream, expected):
                    cols = sorted({c for s in tables[t].values() for c in s})
                    got = tables[t][seq]
                    times = [c for c in cells if c not in SKIP_TOKENS]
                    if name != names[(t, seq)]:
                        errors.append(f"{where}: stop {name!r} in pdf, {names[(t, seq)]!r} in csv")
                    if times != [got[c] for c in sorted(got)]:
                        errors.append(f"{where} {name}: times differ\n   pdf {times}\n   csv {[got[c] for c in sorted(got)]}")
                    if len(cells) == len(cols):
                        want = {c: tok for c, tok in zip(cols, cells) if tok not in SKIP_TOKENS}
                        if want != got:
                            errors.append(f"{where} {name}: column alignment differs")
                    elif len(cells) > len(cols):
                        errors.append(f"{where} {name}: {len(cells)} cells in pdf, {len(cols)} columns in csv")
                    else:
                        blanks.append(f"{where} t{t} {name}: times only in columns "
                                      f"{[cols.index(c) + 1 for c in sorted(got)]} of {len(cols)}")

            # 4. day type
            days_text = [DAY_KEYS[re.sub(r"[\s\-]", "", d).lower()] for d in DAY_RE.findall(text)]
            days_csv = {r["day"] for r in prows}
            if set(days_text) != days_csv:
                errors.append(f"{where}: day types pdf {sorted(set(days_text))} vs csv {sorted(days_csv)}")
            if len(set(days_text)) > 1:
                # several day types on one page: the pdf lists them in order, the trips must follow that order
                order = list(dict.fromkeys(days_text))
                seen = list(dict.fromkeys(r["day"] for r in sorted(prows, key=lambda r: r["trip_id"].split("-p")[1])))
                if seen != order:
                    errors.append(f"{where}: day order pdf {order} vs csv {seen}")

            # 5. footnotes
            marks = sum(tok != "Fußnote" for ln in text.splitlines() if ln.startswith("Fußnote") for tok in ln.split())
            noted = len({r["trip_id"] for r in prows if r["note"]})
            if marks != noted:
                errors.append(f"{where}: {marks} footnote marks in pdf, {noted} trips with note in csv")

    # 6. trips run forward in time; within a table and day the trips are sorted
    trips = defaultdict(list)
    for r in rows:
        trips[r["trip_id"]].append(r)
    starts = defaultdict(list)
    for tid, trip in trips.items():
        trip.sort(key=lambda r: int(r["seq"]))
        if len(trip) < 2:
            errors.append(f"{tid}: only {len(trip)} stop")
        for a, b in zip(trip, trip[1:]):
            gap = (minutes(b["time"]) - minutes(a["time"])) % 1440
            if gap > MAX_GAP:
                errors.append(f"{tid}: {a['stop']} {a['time']} -> {b['stop']} {b['time']}")
        starts[tid.rsplit("c", 1)[0]].append((int(tid.rsplit("c", 1)[1]), trip[-1]["time"]))
    for table, cols in starts.items():
        ends = [minutes(t) for _, t in sorted(cols)]
        if any(b <= a for a, b in zip(ends, ends[1:])):
            errors.append(f"{table}: trips not in ascending order {[t for _, t in sorted(cols)]}")

    return len(rows), len(trips), errors, blanks


def main(pdf_dir=PDF_DIR, csv_dir=None):
    pdf_dir = Path(pdf_dir)
    csv_dir = Path(csv_dir) if csv_dir else pdf_dir / "csv"
    failed = False
    for pdf_path in sorted(pdf_dir.glob("Linie_*.pdf")):
        line = pdf_path.name.split("_")[1]
        n_rows, n_trips, errors, blanks = verify_line(pdf_path, csv_dir / f"linie_{line}.csv")
        print(f"Linie {line}: {n_trips} trips, {n_rows} stop times -> {'FAILED' if errors else 'ok'}"
              f" ({len(blanks)} rows with blank cells)")
        for e in errors:
            print("   ERROR", e)
        if "-v" in sys.argv:
            for b in blanks:
                print("   blank", b)
        failed |= bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(*[a for a in sys.argv[1:] if a != "-v"][:2]))
