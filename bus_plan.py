"""Find the quickest Greifswald bus connections between two sets of stops.

Reads the timetable CSVs written by bus_pdf_to_csv.py. Given a departure window,
a day type and lists of possible start and end stops, it lists the best journeys
(direct or with transfers), ranked by total time from boarding to arrival, which
includes the waiting at transfers.

Set the inputs in the first cell, then run the cells in the interactive window.

Stop names are matched case-insensitively; a unique part of the name is enough and
"Ostseeviertel*" style patterns select several stops at once. Transfers are only
made between buses at a stop of the same name.
"""
# %% Inputs
from pathlib import Path

DAY = "week"                        # week = Mon-Fri, sat, sun = Sundays and holidays
T_FROM = "15:00"                    # earliest departure
T_TO = "18:00"                      # latest departure
END = ["Neunmorgenstraße", "Bahnhofstraße", "Stephanistraße", "Gützkower Straße"]  # possible start stops
START = ["Elisen Park", "IPP"]            # possible end stops

TOP = 5                             # number of options to print
MAX_TRANSFERS = 2
MIN_TRANSFER = 2                    # minutes needed to change bus
SORT = "duration"                   # duration (boarding to arrival), arrival or departure
CSV_DIR = Path(__file__).parent / "csv"

# %% Functions
import csv
import fnmatch
from collections import defaultdict, namedtuple

DAYS = {"week": "Montag - Freitag", "sat": "Samstag", "sun": "Sonn- und Feiertage"}
INF = float("inf")

Trip = namedtuple("Trip", "line direction note stops times")  # times in minutes, running forward
Leg = namedtuple("Leg", "line direction note board_stop board_time alight_stop alight_time n_stops")
Journey = namedtuple("Journey", "depart arrive legs")


def to_min(text):
    h, m = text.strip().split(":")
    return int(h) * 60 + int(m)


def fmt(minutes):
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"


def load_trips(day, csv_dir=CSV_DIR):
    """All trips of one day type as a list of Trip."""
    files = sorted(Path(csv_dir).glob("linie_*.csv"))
    if not files:
        raise FileNotFoundError(f"no linie_*.csv in {csv_dir} - run bus_pdf_to_csv.py first")
    by_trip = defaultdict(list)
    for f in files:
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r["day"] == day:
                    by_trip[r["trip_id"]].append(r)
    trips = []
    for rows in by_trip.values():
        rows.sort(key=lambda r: int(r["seq"]))
        times = []
        for r in rows:
            t = to_min(r["time"])
            while times and t < times[-1]:  # trip runs past midnight
                t += 1440
            times.append(t)
        trips.append(Trip(rows[0]["line"], rows[0]["direction"], rows[0]["note"], [r["stop"] for r in rows], times))
    return trips


def resolve_stops(patterns, all_stops):
    """Turn user input into exact stop names."""
    found = []
    for pat in patterns:
        low = pat.lower()
        exact = [s for s in all_stops if s.lower() == low]
        if exact:
            hits = exact
        elif any(ch in pat for ch in "*?"):
            hits = [s for s in all_stops if fnmatch.fnmatch(s.lower(), low)]
        else:
            hits = [s for s in all_stops if low in s.lower()]
            if len(hits) > 1:
                raise ValueError(f"stop {pat!r} is ambiguous: {', '.join(hits)}  (use e.g. \"{pat}*\" to take all)")
        if not hits:
            raise ValueError(f"no stop served on this day type matches {pat!r} (see the last cell for all stops)")
        found += [s for s in hits if s not in found]
    return found


def scan(connections, trips, starts, ends, t_start, max_legs, min_transfer):
    """Earliest arrival at any end stop when starting at t_start, for every number of legs.

    Connection scan over the stop-to-stop hops sorted by departure. Returns {legs: Journey}
    holding only leg counts that arrive strictly earlier than every option with fewer legs.
    """
    arrival = [dict() for _ in range(max_legs + 1)]  # arrival[k][stop] = earliest arrival with k legs
    parent = [dict() for _ in range(max_legs + 1)]   # parent[k][stop] = (trip, board index, alight index)
    boarded = [dict() for _ in range(max_legs + 1)]  # boarded[k][trip] = index where the trip was boarded as leg k
    for dep, arr, ti, i in connections:
        if dep < t_start:
            continue
        trip = trips[ti]
        for k in range(1, max_legs + 1):
            if ti not in boarded[k]:
                stop = trip.stops[i]
                if k == 1:
                    ok = stop in starts
                else:
                    ok = arrival[k - 1].get(stop, INF) + min_transfer <= dep and parent[k - 1][stop][0] != ti
                if not ok:
                    continue
                boarded[k][ti] = i
            target = trip.stops[i + 1]
            if arr < arrival[k].get(target, INF):
                arrival[k][target] = arr
                parent[k][target] = (ti, boarded[k][ti], i + 1)

    journeys, best = {}, INF
    for k in range(1, max_legs + 1):
        reached = [(arrival[k][s], s) for s in ends if s in arrival[k]]
        if not reached or min(reached)[0] >= best:
            continue
        best, stop = min(reached)
        legs = []
        for kk in range(k, 0, -1):
            ti, b, a = parent[kk][stop]
            trip = trips[ti]
            legs.append(Leg(trip.line, trip.direction, trip.note, trip.stops[b], trip.times[b],
                            trip.stops[a], trip.times[a], a - b))
            stop = trip.stops[b]
        legs.reverse()
        journeys[k] = Journey(legs[0].board_time, legs[-1].alight_time, legs)
    return journeys


def find_routes(day, t_from, t_to, starts, ends, max_transfers=2, min_transfer=2, sort="duration", csv_dir=CSV_DIR):
    """Best journeys leaving a start stop within [t_from, t_to] (HH:MM) on a day type (week/sat/sun).

    starts / ends are lists of stop names or patterns. A journey is kept unless another one leaves
    no earlier, arrives no later and needs no more transfers (a journey using a trip with a day
    restriction never displaces one without). Returns a list of Journey sorted by
    `sort`: duration (boarding to arrival), arrival or departure.
    """
    if day not in DAYS:
        raise ValueError(f"day must be one of {', '.join(DAYS)}")
    trips = load_trips(day, csv_dir)
    all_stops = sorted({s for t in trips for s in t.stops})
    starts, ends = set(resolve_stops(starts, all_stops)), set(resolve_stops(ends, all_stops))
    if starts & ends:
        raise ValueError(f"stop(s) in both start and end: {', '.join(sorted(starts & ends))}")
    t0, t1 = to_min(t_from), to_min(t_to)
    if t1 < t0:
        raise ValueError("time range ends before it starts")

    # Searched twice: over all trips, and over only the trips that run every day of the day type.
    # A trip limited to school days, holidays or single weekdays must not hide one that always runs.
    found = {}
    for subset in (trips, [t for t in trips if not restrictions(t.note)]):
        connections = sorted((t.times[i], t.times[i + 1], ti, i)
                             for ti, t in enumerate(subset) for i in range(len(t.stops) - 1))
        departures = sorted({t.times[i] for t in subset for i in range(len(t.stops) - 1)
                             if t.stops[i] in starts and t0 <= t.times[i] <= t1})
        for dep in departures:
            for j in scan(connections, subset, starts, ends, dep, max_transfers + 1, min_transfer).values():
                if j.depart <= t1:
                    found[tuple(j.legs)] = j
    journeys = list(found.values())

    def limited(j):
        return any(restrictions(leg.note) for leg in j.legs)

    def dominated(a):
        return any(b is not a and b.depart >= a.depart and b.arrive <= a.arrive and len(b.legs) <= len(a.legs)
                   and (b.depart, b.arrive, len(b.legs)) != (a.depart, a.arrive, len(a.legs))
                   and not (limited(b) and not limited(a)) for b in journeys)

    best, seen = [], set()
    for j in journeys:
        key = (j.depart, j.arrive, len(j.legs), limited(j))
        if not dominated(j) and key not in seen:  # equal alternatives: keep one
            seen.add(key)
            best.append(j)
    keys = {"duration": lambda j: (j.arrive - j.depart, len(j.legs), j.depart),
            "arrival": lambda j: (j.arrive, -j.depart, len(j.legs)),
            "departure": lambda j: (j.depart, j.arrive, len(j.legs))}
    return sorted(best, key=keys[sort])


def restrictions(note):
    """Day restrictions hidden in a trip's footnotes: listed under Monday - Friday, but not run every weekday."""
    out = []
    if "nur an Schultagen" in note:
        out.append("on school days only")
    if "nur in den Ferien" in note:
        out.append("in school holidays only")
    if note.startswith("nur "):
        out.append("only on " + note[4:].split(";")[0])
    return out


def describe(journey, rank=None):
    """Multi-line text for one journey."""
    legs = journey.legs
    ride = sum(l.alight_time - l.board_time for l in legs)
    total = journey.arrive - journey.depart
    kind = "direct" if len(legs) == 1 else f"{len(legs) - 1} transfer{'s' if len(legs) > 2 else ''}, {total - ride} min waiting"
    head = f"{fmt(journey.depart)} -> {fmt(journey.arrive)}   {total} min   ({kind})"
    lines = [f"{rank:2d}. {head}" if rank is not None else head]
    for leg in legs:
        if restrictions(leg.note):
            lines.append(f"      !! Linie {leg.line} runs {', '.join(restrictions(leg.note))} - not every weekday")
    for n, leg in enumerate(legs):
        if n:
            lines.append(f"      wait {leg.board_time - legs[n - 1].alight_time} min at {leg.board_stop}")
        lines.append(f"      Linie {leg.line:<3} {fmt(leg.board_time)} {leg.board_stop} -> {fmt(leg.alight_time)} "
                     f"{leg.alight_stop}   ({leg.alight_time - leg.board_time} min, {leg.n_stops} stops, "
                     f"direction {leg.direction.split(' -> ')[-1]})")
        if leg.note:
            lines.append(f"            note {leg.note}")
    return "\n".join(lines)

# %% Compute
journeys = find_routes(DAY, T_FROM, T_TO, START, END, MAX_TRANSFERS, MIN_TRANSFER, SORT, CSV_DIR)

print(f"{DAYS[DAY]}, leaving {T_FROM}-{T_TO}: {', '.join(START)} -> {', '.join(END)}")
if journeys:
    print(f"{min(TOP, len(journeys))} of {len(journeys)} options, sorted by {SORT}\n")
    for n, j in enumerate(journeys[:TOP], start=1):
        print(describe(j, n))
else:
    print("no connection found")

# %% All stop names with their lines
stop_lines = defaultdict(set)
for day in DAYS:
    for t in load_trips(day, CSV_DIR):
        for s in t.stops:
            stop_lines[s].add(t.line)
for s in sorted(stop_lines, key=str.lower):
    print(f"{s:<28} {', '.join(sorted(stop_lines[s]))}")
