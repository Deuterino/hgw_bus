"""Convert the regional bus timetables (VVG / AVG layout) to the same CSV format as the city lines.

Only the lines in LINES are read, and of each trip only the stops inside Greifswald are kept
(rows printed as "Greifswald, ..." or "Gw. ..."), renamed to the city timetable's stop names so
that transfers work. Trips that touch fewer than two Greifswald stops are dropped.

After writing, every CSV is checked against a plain-text re-read of its PDF pages (see verify).

Needs pdfplumber. Usage:
    python regional_pdf_to_csv.py
"""
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import pdfplumber

HERE = Path(__file__).parent
CSV_DIR = HERE / "csv"
PDFS = [HERE / "Gesamtfahrplan-ab-24.08.2026.pdf", HERE / "Linie-271.pdf"]
LINES = ["109", "126", "127", "135", "138", "271"]
FIELDS = ["line", "day", "direction", "trip_id", "seq", "stop", "time", "note", "page"]

# regional spelling (leader dots removed, "Greifswald," / "Gw." prefix stripped) -> city stop name
ALIASES = {
    "ZOB (DB)": "ZOB", "ZOB (EA)": "ZOB",
    "J-Stelling-Straße": "Johann-Stelling-Straße",
    "H-Beimler-Str": "Hans-Beimler-Straße",
    "D-Teßmann-Straße": "D.-Teßmann-Straße",
    "Bahnhof Süd (DB)": "Bahnhof Süd",
    "Ostseeviertel I": "Ostseeviertel 1", "Ostseeviertel II": "Ostseeviertel 2",
    "Schönwalde I": "Schönwalde 1", "Schönwalde II": "Schönwalde 2",
    "Am StGeorgsfeld": "Am St.Georgsfeld", "Am St Georgsfeld": "Am St.Georgsfeld",
}
CITY_PREFIX = re.compile(r"^(Greifswald,?|Gw)\s+")
TIME_RE = re.compile(r"^([A-Za-z]?)(\d{1,2}):(\d\d)$")  # optional mark such as R = on-demand stop
DAY_KEYS = {"montagfreitag": "week", "mofr": "week", "samstag": "sat", "sonntag": "sun"}
WEEKDAYS = {"Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"}
LEGEND_RE = re.compile(r"^(\S{1,2}) [-=] (.+)$")
MAX_GAP = 15  # minutes between consecutive kept stops before a trip is flagged


def day_key(text):
    return DAY_KEYS.get(re.sub(r"[\s\-–]", "", text).lower())


def no_dots(page):
    """The page without '.' characters: the leader dots run underneath the first time columns."""
    return page.filter(lambda o: not (o["object_type"] == "char" and o["text"] == "."))


def cluster_lines(words, tol=2.5):
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


def city_stop(label):
    """City stop name for a row label, or None if the stop is outside Greifswald."""
    if not CITY_PREFIX.match(label):
        return None
    name = CITY_PREFIX.sub("", label).strip()
    name = re.sub(r"^(\w) -", r"\1-", name)  # "J -Stelling-Straße": gap left by the removed dot
    return ALIASES.get(name, name)


def pretty(label):
    return city_stop(label) or label


def line_pages():
    """{line: [(pdf path, page number)]} for the wanted lines, found by the page title."""
    found = defaultdict(list)
    for path in PDFS:
        with pdfplumber.open(path) as pdf:
            for pno, page in enumerate(pdf.pages, start=1):
                m = re.match(r"\s*Linie (\d+)\b", page.extract_text() or "")
                if m and m.group(1) in LINES:
                    found[m.group(1)].append((path, pno))
    return found


def parse_table(page, table):
    """Columns of one table: {col: {day, marks, times{row: time}}} plus the row labels."""
    cells = sorted((c for c in max(table.rows, key=lambda r: sum(c is not None for c in r.cells)).cells
                    if c is not None), key=lambda c: c[0])
    edges = [(c[0], c[2]) for c in cells]
    if len(edges) < 2:
        return [], {}
    x0, top, x1, bottom = table.bbox
    clean = no_dots(page)

    day_spans = []
    for row in table.rows:
        for c in row.cells:
            if c is not None and day_key(clean.crop(c).extract_text() or ""):
                day_spans.append((c[0], c[2], day_key(clean.crop(c).extract_text())))

    def column_of(w):
        xc = (w["x0"] + w["x1"]) / 2
        for i, (a, b) in enumerate(edges):
            if a <= xc <= b:
                return i
        raise ValueError(f"word {w['text']!r} outside all columns")

    words = [w for w in clean.extract_words(x_tolerance=1.5)
             if x0 - 1 <= (w["x0"] + w["x1"]) / 2 <= x1 + 1 and top - 1 <= (w["top"] + w["bottom"]) / 2 <= bottom + 1]
    if not day_spans:
        # header cells not usable: a "Verkehrstage" line naming a single day type covers the whole table
        for ln in cluster_lines(words):
            texts = [w["text"] for w in ln["words"]]
            if "Verkehrstage" in texts and day_key(" ".join(t for t in texts if t != "Verkehrstage")):
                day_spans = [(x0, x1 + 1, day_key(" ".join(t for t in texts if t != "Verkehrstage")))]
    labels, times, marks = [], defaultdict(dict), defaultdict(list)
    started = False
    for ln in cluster_lines(words):
        row = defaultdict(list)
        for w in ln["words"]:
            row[column_of(w)].append(w["text"])
        label = row.pop(0, [])
        if label and label[-1] in ("ab", "an"):
            label = label[:-1]
        label = " ".join(label)
        if LEGEND_RE.match(label):
            break  # footnote legend inside the table frame: the stop rows are over
        started = started or label.startswith("Fahrtnummer")
        if not row or not started or label.startswith("Fahrtnummer"):
            continue  # title rows above the trip numbers, and rows no trip serves
        if label.startswith(("Verkehrshinweise", "Fußnoten")):
            for col, toks in row.items():
                marks[col] += [t for t in toks if t not in marks[col]]
            continue
        if not label:
            raise ValueError(f"row without stop name: {dict(row)}")
        r = len(labels)
        labels.append(label)
        for col, toks in row.items():
            tok = "".join(toks)  # a mark can be printed apart from its time: "R 06:33", "R :"
            if re.fullmatch(r"[A-Za-z]?:", tok):
                continue
            m = TIME_RE.match(tok)
            if not m:
                raise ValueError(f"{label}: unexpected cell content {toks!r}")
            times[col][r] = (f"{int(m.group(2)):02d}:{m.group(3)}", m.group(1))

    columns = {}
    for col, t in times.items():
        xc = sum(edges[col]) / 2
        days = [k for a, b, k in day_spans if a <= xc < b]
        if len(days) != 1:
            raise ValueError(f"column {col}: cannot assign day type ({days})")
        columns[col] = {"day": days[0], "marks": marks.get(col, []), "times": t}
    return labels, columns


def note_text(marks, legends):
    weekdays = [legends[m] for m in marks if legends.get(m) in WEEKDAYS]
    parts = [f"{m}: {legends[m]}" for m in marks if legends[m] not in WEEKDAYS]
    if weekdays:
        parts.insert(0, "nur " + ", ".join(weekdays))
    return "; ".join(parts)


def convert_line(line, pages):
    legends = {}
    for path, pno in pages:
        with pdfplumber.open(path) as pdf:
            for ln in (pdf.pages[pno - 1].extract_text() or "").splitlines():
                m = LEGEND_RE.match(ln.strip())
                if m and legends.setdefault(m.group(1), m.group(2).strip()) != m.group(2).strip():
                    raise ValueError(f"Linie {line}: conflicting legend for {m.group(1)}")

    rows, dropped = [], 0
    for path, pno in pages:
        with pdfplumber.open(path) as pdf:
            page = pdf.pages[pno - 1]
            tables = sorted(page.find_tables(), key=lambda t: (round(t.bbox[1]), t.bbox[0]))
            for tno, table in enumerate(tables, start=1):
                labels, columns = parse_table(page, table)
                for col in sorted(columns):
                    c = columns[col]
                    served = sorted(c["times"])
                    # times printed with a mark (R = only after booking by phone) are not kept
                    kept = [r for r in served if city_stop(labels[r]) and not c["times"][r][1]]
                    if len(kept) < 2:
                        dropped += 1
                        continue
                    direction = f"{pretty(labels[served[0]])} -> {pretty(labels[served[-1]])}"
                    trip_id = f"{line}-{c['day']}-p{pno}t{tno}c{col:02d}"
                    note = note_text(c["marks"], legends)
                    for r in kept:
                        rows.append({"line": line, "day": c["day"], "direction": direction, "trip_id": trip_id, "seq": r,
                                     "stop": city_stop(labels[r]), "time": c["times"][r][0], "note": note, "page": pno})
    return rows, dropped


def verify(line, pages, rows):
    """Compare the CSV rows of one line with a plain-text read of the PDF. Returns a list of problems.

    The text read knows nothing of the table geometry. Per Greifswald stop row it gives the
    left-to-right list of printed cells; a row printed with one cell per trip column therefore fixes
    which column each time belongs to. Trips must also run forward in time without large jumps.
    """
    errors = []
    by_page = defaultdict(list)
    for r in rows:
        by_page[r["page"]].append(r)
    for path, pno in pages:
        with pdfplumber.open(path) as pdf:
            text = no_dots(pdf.pages[pno - 1]).extract_text() or ""
        # text side: per table (a "Fahrtnummer" line starts one) the Greifswald rows in print order
        text_tables = []
        for ln in text.splitlines():
            toks = ln.split()
            if toks[:1] == ["Fahrtnummer"]:
                text_tables.append({"ncols": len(toks) - 1, "rows": []})
                continue
            cells = [t for t in toks if t == ":" or TIME_RE.match(t)]
            label = [t for t in toks if not (t == ":" or TIME_RE.match(t))]
            if label and label[-1] in ("ab", "an"):
                label = label[:-1]
            stop = city_stop(" ".join(label))
            if text_tables and stop and any(m and not m.group(1) for m in map(TIME_RE.match, cells)):
                text_tables[-1]["rows"].append((stop, cells))

        csv_tables = defaultdict(lambda: defaultdict(dict))
        names = {}
        for r in by_page.get(pno, []):
            tno, col = map(int, re.search(r"t(\d+)c(\d+)$", r["trip_id"]).groups())
            csv_tables[tno][r["seq"]][col] = r["time"]
            names[(tno, r["seq"])] = r["stop"]
        text_tables = [t for t in text_tables if t["rows"]]
        if len(text_tables) != len(csv_tables):
            errors.append(f"p{pno}: {len(text_tables)} tables with Greifswald stops in pdf, {len(csv_tables)} in csv")
            continue
        for tt, tno in zip(text_tables, sorted(csv_tables)):
            ct = csv_tables[tno]
            kept_cols = {c for s in ct.values() for c in s}
            if len(tt["rows"]) != len(ct):
                errors.append(f"p{pno} t{tno}: {len(tt['rows'])} Greifswald rows in pdf, {len(ct)} in csv")
                continue
            for (stop, cells), seq in zip(tt["rows"], sorted(ct)):
                if stop != names[(tno, seq)]:
                    errors.append(f"p{pno} t{tno}: stop {stop!r} in pdf, {names[(tno, seq)]!r} in csv")
                printed = [f"{int(m.group(2)):02d}:{m.group(3)}" if m and not m.group(1) else None
                           for m in map(TIME_RE.match, cells)]
                if len(cells) == tt["ncols"]:
                    # full row: cell i belongs to trip column i + 1
                    want = {i + 1: t for i, t in enumerate(printed) if t and i + 1 in kept_cols}
                    if want != ct[seq]:
                        errors.append(f"p{pno} t{tno} {stop}: column alignment differs\n   pdf {want}\n   csv {ct[seq]}")
                else:
                    # row with blank cells: the csv times must appear in print order
                    it = iter(t for t in printed if t)
                    if not all(any(t == p for p in it) for _, t in sorted(ct[seq].items())):
                        errors.append(f"p{pno} t{tno} {stop}: times not found in print order {ct[seq]}")
                    if len(cells) > tt["ncols"]:
                        errors.append(f"p{pno} t{tno} {stop}: more cells than trip columns")

    trips = defaultdict(list)
    for r in rows:
        trips[r["trip_id"]].append(r)
    for tid, trip in trips.items():
        trip.sort(key=lambda r: r["seq"])
        mins = [int(r["time"][:2]) * 60 + int(r["time"][3:]) for r in trip]
        for a, b, ra, rb in zip(mins, mins[1:], trip, trip[1:]):
            if not 0 <= b - a <= MAX_GAP:
                errors.append(f"{tid}: {ra['stop']} {ra['time']} -> {rb['stop']} {rb['time']}")
    return errors


def main():
    pages = line_pages()
    city_stops = set()
    for f in CSV_DIR.glob("linie_*.csv"):
        if f.stem.split("_")[1] not in LINES:
            with open(f, newline="", encoding="utf-8") as fh:
                city_stops |= {r["stop"] for r in csv.DictReader(fh)}
    failed = False
    for line in LINES:
        if line not in pages:
            print(f"Linie {line}: no page found")
            failed = True
            continue
        rows, dropped = convert_line(line, pages[line])
        errors = verify(line, pages[line], rows)
        out = CSV_DIR / f"linie_{line}.csv"
        with open(out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        trips = len({r["trip_id"] for r in rows})
        new = sorted({r["stop"] for r in rows} - city_stops)
        print(f"Linie {line}: pages {[p for _, p in pages[line]]} -> {out.name}: {trips} trips, {len(rows)} stop times, "
              f"{dropped} trips without two Greifswald stops dropped -> {'FAILED' if errors else 'verified'}")
        if new:
            print(f"   stops not in the city timetable: {', '.join(new)}")
        for e in errors:
            print("   ERROR", e)
        failed |= bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
