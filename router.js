// Route search over the timetable CSVs (csv/linie_*.csv). Same method as bus_plan.py:
// a connection scan from every possible departure in the window, one result per number of legs,
// then only journeys that no other journey beats on departure, arrival and transfers are kept.
const BusRouter = (() => {
  "use strict";

  function toMin(text) {
    const [h, m] = text.trim().split(":");
    return parseInt(h, 10) * 60 + parseInt(m, 10);
  }

  function fmt(minutes) {
    const h = Math.floor(minutes / 60) % 24;
    return String(h).padStart(2, "0") + ":" + String(minutes % 60).padStart(2, "0");
  }

  // CSV text -> array of objects keyed by the header row
  function parseCsv(text) {
    const records = [];
    let record = [], field = "", quoted = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (quoted) {
        if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
        else if (ch === '"') quoted = false;
        else field += ch;
      } else if (ch === '"') quoted = true;
      else if (ch === ",") { record.push(field); field = ""; }
      else if (ch === "\n") { record.push(field); records.push(record); record = []; field = ""; }
      else if (ch !== "\r") field += ch;
    }
    if (field !== "" || record.length) { record.push(field); records.push(record); }
    const header = records.shift();
    return records.filter(r => r.length === header.length)
                  .map(r => Object.fromEntries(header.map((name, i) => [name, r[i]])));
  }

  // rows of all lines -> trips of one day type, times in minutes running forward
  function buildTrips(rows, day) {
    const byTrip = new Map();
    for (const r of rows) {
      if (r.day !== day) continue;
      if (!byTrip.has(r.trip_id)) byTrip.set(r.trip_id, []);
      byTrip.get(r.trip_id).push(r);
    }
    const trips = [];
    for (const tripRows of byTrip.values()) {
      tripRows.sort((a, b) => parseInt(a.seq, 10) - parseInt(b.seq, 10));
      const times = [];
      for (const r of tripRows) {
        let t = toMin(r.time);
        while (times.length && t < times[times.length - 1]) t += 1440;  // trip runs past midnight
        times.push(t);
      }
      trips.push({ line: tripRows[0].line, direction: tripRows[0].direction, note: tripRows[0].note,
                   stops: tripRows.map(r => r.stop), times });
    }
    return trips;
  }

  // earliest arrival at any end stop when starting at tStart, for every number of legs
  function scan(connections, trips, starts, ends, tStart, maxLegs, minTransfer) {
    const arrival = [], parent = [], boarded = [];
    for (let k = 0; k <= maxLegs; k++) { arrival.push(new Map()); parent.push(new Map()); boarded.push(new Map()); }
    for (const [dep, arr, ti, i] of connections) {
      if (dep < tStart) continue;
      const trip = trips[ti];
      for (let k = 1; k <= maxLegs; k++) {
        if (!boarded[k].has(ti)) {
          const stop = trip.stops[i];
          let ok;
          if (k === 1) ok = starts.has(stop);
          else ok = arrival[k - 1].has(stop) && arrival[k - 1].get(stop) + minTransfer <= dep
                    && parent[k - 1].get(stop)[0] !== ti;
          if (!ok) continue;
          boarded[k].set(ti, i);
        }
        const target = trip.stops[i + 1];
        if (!arrival[k].has(target) || arr < arrival[k].get(target)) {
          arrival[k].set(target, arr);
          parent[k].set(target, [ti, boarded[k].get(ti), i + 1]);
        }
      }
    }

    const journeys = [];
    let best = Infinity;
    for (let k = 1; k <= maxLegs; k++) {
      let bestHere = Infinity, stop = null;
      for (const s of [...ends].sort()) {
        if (arrival[k].has(s) && arrival[k].get(s) < bestHere) { bestHere = arrival[k].get(s); stop = s; }
      }
      if (stop === null || bestHere >= best) continue;
      best = bestHere;
      const legs = [];
      for (let kk = k; kk >= 1; kk--) {
        const [ti, b, a] = parent[kk].get(stop);
        const trip = trips[ti];
        legs.push({ line: trip.line, direction: trip.direction, note: trip.note,
                    boardStop: trip.stops[b], boardTime: trip.times[b],
                    alightStop: trip.stops[a], alightTime: trip.times[a], nStops: a - b });
        stop = trip.stops[b];
      }
      legs.reverse();
      journeys.push({ depart: legs[0].boardTime, arrive: legs[legs.length - 1].alightTime, legs });
    }
    return journeys;
  }

  // true if the trip's footnotes limit it to school days, school holidays or single weekdays
  function isLimited(note) {
    return /nur an Schultagen|nur in den Ferien|^nur /.test(note);
  }

  // Best journeys leaving one of `starts` within [tFrom, tTo] ("HH:MM") and reaching one of `ends`.
  function findRoutes(trips, tFrom, tTo, starts, ends, opts = {}) {
    const maxTransfers = opts.maxTransfers ?? 2, minTransfer = opts.minTransfer ?? 2, sort = opts.sort ?? "duration";
    starts = new Set(starts); ends = new Set(ends);
    const t0 = toMin(tFrom), t1 = toMin(tTo);

    // Searched twice: over all trips, and over only the trips that run every day of the day type.
    // A trip limited to school days, holidays or single weekdays must not hide one that always runs.
    const found = new Map();
    for (const subset of [trips, trips.filter(t => !isLimited(t.note))]) {
      const connections = [];
      const departures = new Set();
      subset.forEach((t, ti) => {
        for (let i = 0; i < t.stops.length - 1; i++) {
          connections.push([t.times[i], t.times[i + 1], ti, i]);
          if (starts.has(t.stops[i]) && t0 <= t.times[i] && t.times[i] <= t1) departures.add(t.times[i]);
        }
      });
      connections.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2] || a[3] - b[3]);
      for (const dep of [...departures].sort((a, b) => a - b)) {
        for (const j of scan(connections, subset, starts, ends, dep, maxTransfers + 1, minTransfer)) {
          if (j.depart <= t1) found.set(JSON.stringify(j.legs), j);
        }
      }
    }
    const journeys = [...found.values()];

    const limited = j => j.legs.some(l => isLimited(l.note));
    const dominated = a => journeys.some(b => b !== a && b.depart >= a.depart && b.arrive <= a.arrive
      && b.legs.length <= a.legs.length
      && !(b.depart === a.depart && b.arrive === a.arrive && b.legs.length === a.legs.length)
      && !(limited(b) && !limited(a)));
    const best = [], seen = new Set();
    for (const j of journeys) {
      const key = j.depart + "|" + j.arrive + "|" + j.legs.length + "|" + limited(j);
      if (!dominated(j) && !seen.has(key)) { seen.add(key); best.push(j); }  // equal alternatives: keep one
    }
    const keys = {
      duration: j => [j.arrive - j.depart, j.legs.length, j.depart],
      arrival: j => [j.arrive, -j.depart, j.legs.length],
      departure: j => [j.depart, j.arrive, j.legs.length],
    };
    const key = keys[sort];
    return best.sort((a, b) => {
      const ka = key(a), kb = key(b);
      return ka[0] - kb[0] || ka[1] - kb[1] || ka[2] - kb[2];
    });
  }

  return { toMin, fmt, parseCsv, buildTrips, findRoutes };
})();
