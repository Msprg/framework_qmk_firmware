#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tally the limit log: what held the CPU and dGPU back, how long, and when.

    fw16-sysmon.exe --limit-report [--report-days N] [--export-csv DIR]
    python limitreport.py [DB] [--days N] [--csv DIR]

Standard library only, so it also runs with any Python 3.8+ on a copy of
limitlog.sqlite (see limitlog.py for what the tables hold). The database is
opened read-only; the service keeps writing while this reads.

Times come from the tally table (exact: every poll-loop tick is counted), the
distributions from the samples (each weighted by the time to the next one,
capped at SAMPLE_CAP_S), episode counts and lengths from the episodes.
"""

import argparse
import csv
import json
import os
import sqlite3
import struct
import sys
import time
import zlib
from pathlib import Path

DB_NAME = "limitlog.sqlite"
SAMPLE_CAP_S = 6.0  # a sample stands for at most this long (3x the busy cadence)


def default_db():
    try:
        import paths
        return os.path.join(paths.data_dir(), DB_NAME)
    except Exception:
        base = os.environ.get("ProgramData") or r"C:\ProgramData"
        return os.path.join(base, "FW16Sysmon", DB_NAME)


def connect(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    return db


# --- formatting ------------------------------------------------------------

def dur(s):
    if s is None:
        return "-"
    s = float(s)
    if s >= 3600:
        return f"{s / 3600:.1f} h"
    if s >= 60:
        return f"{int(s // 60)}m{int(s % 60):02d}s"
    return f"{s:.0f} s"


def pct(a, b):
    return "-" if not b else f"{100.0 * (a or 0) / b:.0f}%"


def num(v, nd=1, unit=""):
    return "-" if v is None else f"{v:.{nd}f}{unit}"


def local(ts):
    return "-" if ts is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def table(out, headers, rows, indent="  "):
    if not rows:
        out.write(f"{indent}(none)\n")
        return
    cells = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)]
    line = lambda r: indent + "  ".join(
        (c.ljust(w) if i == 0 else c.rjust(w)) for i, (c, w) in enumerate(zip(r, widths)))
    out.write(line(headers).rstrip() + "\n")
    for r in cells:
        out.write(line(r).rstrip() + "\n")


def quantile(vals, q):
    if not vals:
        return None
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(q * len(vals)))]


# --- the report ------------------------------------------------------------

class Report:
    def __init__(self, db, since, out):
        self.db = db
        self.since = since
        self.day = time.strftime("%Y-%m-%d", time.localtime(since)) if since else "0000"
        self.out = out

    def q(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def one(self, sql, *args):
        r = self.db.execute(sql, args).fetchone()
        return r[0] if r else None

    def w(self, text=""):
        self.out.write(text + "\n")

    def tally(self, comp, reason, col="bound_s", where="", *args):
        return self.one(f"SELECT SUM({col}) FROM tally WHERE component=? AND reason=? "
                        f"AND day>=? {where}", comp, reason, self.day, *args) or 0.0

    def run(self, path):
        self.header(path)
        observed = self.tally("all", "_observed")
        if not observed:
            self.w("\nNothing tallied in this window yet.")
            return
        self.sources(observed)
        for comp, title in (("cpu", "CPU (the whole APU)"), ("gpu", "dGPU")):
            self.component(comp, title, observed)
        top = self.q("SELECT reason, SUM(primary_s) p FROM tally WHERE component='cpu' "
                     "AND reason NOT LIKE '\\_%' ESCAPE '\\' AND day>=? GROUP BY reason "
                     "ORDER BY p DESC", self.day)
        reasons = [r["reason"] for r in top if r["p"]]
        for reason in (["stapm"] + [r for r in reasons[:2] if r != "stapm"]):
            if reason in reasons and reason in LIMIT_COLS:
                self.deep_dive(reason)
        self.limit_changes()
        self.processes()
        self.events()

    def header(self, path):
        size = os.path.getsize(path) if os.path.exists(path) else 0
        first, last, n = self.db.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*) FROM samples WHERE ts>=?",
            (self.since,)).fetchone()
        eps = self.one("SELECT COUNT(*) FROM episodes WHERE start>=?", self.since)
        evs = self.one("SELECT COUNT(*) FROM events WHERE ts>=?", self.since)
        machine = {}
        try:
            machine = json.loads(self.one("SELECT value FROM meta WHERE key='machine'") or "{}")
        except ValueError:
            pass
        self.w(f"Limit log   {path}  ({size / 1048576:.1f} MB)")
        if machine:
            self.w(f"Machine     {machine.get('SystemProductName', '?')}, "
                   f"{machine.get('ProcessorNameString', '?')}, BIOS "
                   f"{machine.get('BIOSVersion', '?')} ({machine.get('BIOSReleaseDate', '?')})")
        self.w(f"Window      {'all of it' if not self.since else 'since ' + local(self.since)}"
               f"; samples {local(first)} .. {local(last)}")
        self.w(f"Rows        {n} samples, {eps} episodes, {evs} events")

    def sources(self, observed):
        self.w(f"\nObserved {dur(observed)} (the service running, the machine awake)")
        for col, title in (("power_src", "power source"), ("power_mode", "power mode")):
            rows = self.q(f"SELECT {col} k, SUM(bound_s) s FROM tally WHERE component='all' "
                          f"AND reason='_observed' AND day>=? GROUP BY k ORDER BY s DESC",
                          self.day)
            self.w(f"  by {title}: " + ", ".join(
                f"{r['k']} {dur(r['s'])} ({pct(r['s'], observed)})" for r in rows))

    def component(self, comp, title, observed):
        loaded = self.tally(comp, "_loaded")
        limited = self.tally(comp, "_limited")
        limited_loaded = self.tally(comp, "_limited", "loaded_s")
        self.w(f"\n== {title}: what held it back ==")
        what = "a core at 80%+" if comp == "cpu" else "30%+ or drawing 15 W+"
        self.w(f"  busy ({what}) {dur(loaded)} "
               f"({pct(loaded, observed)} of observed); limited "
               f"{dur(limited)}, of which while busy {dur(limited_loaded)} "
               f"({pct(limited_loaded, loaded)} of busy time)")
        rows = self.q("SELECT reason, SUM(primary_s) p, SUM(bound_s) b, SUM(loaded_s) l "
                      "FROM tally WHERE component=? AND reason NOT LIKE '\\_%' ESCAPE '\\' "
                      "AND day>=? GROUP BY reason ORDER BY p DESC, b DESC", comp, self.day)
        if not rows:
            self.w("  no limit bound in this window")
            return
        out = []
        for r in rows:
            durs = [d["duration"] or 0 for d in self.q(
                "SELECT duration FROM episodes WHERE component=? AND reason=? AND start>=?",
                comp, r["reason"], self.since)]
            out.append([r["reason"], dur(r["p"]), dur(r["b"]), pct(r["l"], loaded),
                        len(durs), dur(quantile(durs, 0.5)), dur(quantile(durs, 0.9)),
                        dur(max(durs) if durs else None)])
        self.w("  primary = the closest to its limit of those binding at once; "
               "%busy = bound while busy / busy time")
        table(self.out, ["reason", "primary", "bound", "%busy", "episodes", "median",
                         "p90", "longest"], out)
        for col, label in (("power_src", "power source"), ("power_mode", "power mode")):
            piv = self.q(f"SELECT reason, {col} k, SUM(primary_s) p FROM tally WHERE "
                         f"component=? AND reason NOT LIKE '\\_%' ESCAPE '\\' AND day>=? "
                         f"GROUP BY reason, k HAVING p > 0 ORDER BY reason, p DESC",
                         comp, self.day)
            base = {r["k"]: r["s"] for r in self.q(
                f"SELECT {col} k, SUM(bound_s) s FROM tally WHERE component='all' AND "
                f"reason='_observed' AND day>=? GROUP BY k", self.day)}
            by = {}
            for r in piv:
                by.setdefault(r["reason"], []).append(
                    f"{r['k']} {dur(r['p'])} ({pct(r['p'], base.get(r['k']))} of its time)")
            if by:
                self.w(f"  primary time by {label}:")
                for reason, parts in by.items():
                    self.w(f"    {reason:12s} " + "; ".join(parts))

    def weighted(self, where, *args):
        """Samples matching `where`, each with its weight w (seconds)."""
        return (f"WITH s AS (SELECT *, MIN(COALESCE(LEAD(ts) OVER (ORDER BY ts) - ts, "
                f"{SAMPLE_CAP_S}), {SAMPLE_CAP_S}) AS w FROM samples WHERE ts>=?) "
                f"SELECT {{cols}} FROM s WHERE {where}"), (self.since,) + args

    def deep_dive(self, reason):
        lcol, vcol, unit = LIMIT_COLS[reason]
        self.w(f"\n== {reason}: while it was the CPU's primary limit ==")
        sql, args = self.weighted("cpu_primary=?", reason)
        tot = self.db.execute(sql.format(cols="SUM(w)"), args).fetchone()[0]
        if not tot:
            self.w("  no samples")
            return
        # (no SMU socket power here: libryzenadj misdecodes it on some tables
        # -- on Strix Point 0x5d000b it reads the boost setpoint)
        cols = (f"ROUND({lcol}) lim, SUM(w) t, AVG({vcol}) v, AVG(cpu_ghz_avg) ghz, "
                f"AVG(slow_v / slow_l) slr, AVG(pkg_w) pkg, AVG(ec_apu) apu, "
                f"AVG(ec_ambient) amb, "
                f"AVG(MAX(COALESCE(fan_l,0), COALESCE(fan_r,0))) fan, AVG(cpu_util) util, "
                f"AVG(CASE WHEN batt_state='charging' THEN 1.0 ELSE 0 END) chg, "
                f"AVG(pd_w) pd")
        rows = self.db.execute(sql.format(cols=cols) + " GROUP BY lim ORDER BY lim",
                               args).fetchall()
        self.w(f"  by the limit's own value (time-weighted; it moves):")
        table(self.out, ["limit", "time", "share", "value", "busy GHz", "slow PPT",
                         "pkg W", "EC apu C", "EC amb C", "fan rpm", "load %", "charging",
                         "PD W"],
              [[num(r["lim"], 0, unit), dur(r["t"]), pct(r["t"], tot), num(r["v"]),
                num(r["ghz"], 2), pct(r["slr"], 1), num(r["pkg"]), num(r["apu"], 0),
                num(r["amb"], 0), num(r["fan"], 0), num(r["util"], 0),
                pct(r["chg"], 1), num(r["pd"], 0)] for r in rows], indent="    ")
        for col, label in (("power_mode", "power mode"), ("power_src", "power source"),
                           ("batt_state", "battery")):
            rows = self.db.execute(
                sql.format(cols=f"{col} k, SUM(w) t, AVG({lcol}) lim") +
                " GROUP BY k ORDER BY t DESC", args).fetchall()
            self.w(f"  by {label}: " + ", ".join(
                f"{r['k']} {pct(r['t'], tot)} (limit {num(r['lim'], 1, unit)})" for r in rows))
        if reason == "stapm":
            r = self.db.execute(sql.format(
                cols="AVG(stapm_l) st, AVG(slow_l) sl, AVG(fast_l) fa, AVG(slow_v/slow_l) slr, "
                     "AVG(stapm_time) tc, AVG(stt_apu_l) sttl, AVG(stt_apu_v) sttv"),
                args).fetchone()
            if r["st"] and r["sl"]:
                self.w(f"  headroom: STAPM {num(r['st'])} W against slow PPT "
                       f"{num(r['sl'])} W and fast PPT {num(r['fa'])} W; slow PPT sat at "
                       f"{pct(r['slr'], 1)} of its limit. Raising STAPM to the slow PPT "
                       f"would allow +{num(r['sl'] - r['st'])} W "
                       f"(+{pct(r['sl'] - r['st'], r['st'])}) before the next limit.")
            self.w(f"  STAPM time constant {num(r['tc'])}; skin-temperature (STT) limit "
                   f"{num(r['sttl'])} C, value {num(r['sttv'])} C")
        # what the CPU did when it was not held back, at a like load: a
        # single busy thread boosts far higher than all cores can, so compare
        # all-core load with all-core load
        for label, lo, hi in (("all-core load (>=80%)", 80, 101),
                              ("partial load (25-80%)", 25, 80)):
            band = f"cpu_util>={lo} AND cpu_util<{hi}"
            sql1, args1 = self.weighted(f"cpu_primary=? AND {band}", reason)
            sql2, args2 = self.weighted(f"cpu_bound IS NULL AND {band}")
            cols = "SUM(w) t, AVG(cpu_ghz_avg) ghz, AVG(cpu_ghz_max) gmax, AVG(pkg_w) pkg"
            r1 = self.db.execute(sql1.format(cols=cols), args1).fetchone()
            r2 = self.db.execute(sql2.format(cols=cols), args2).fetchone()
            if not (r1["t"] or r2["t"]):
                continue
            self.w(f"  {label}: {reason} bound {dur(r1['t'])}, busy CPUs at "
                   f"{num(r1['ghz'], 2)} GHz (fastest {num(r1['gmax'], 2)}), "
                   f"{num(r1['pkg'])} W package; nothing bound {dur(r2['t'])}, "
                   f"{num(r2['ghz'], 2)} GHz (fastest {num(r2['gmax'], 2)}), "
                   f"{num(r2['pkg'])} W")

    def limit_changes(self):
        rows = []
        for r in self.q("SELECT ts, detail FROM events WHERE kind='smu_limits' AND ts>=? "
                        "ORDER BY ts", self.since):
            d = json.loads(r["detail"] or "{}")
            # stt_dgpu_l: misdecoded on Strix Point and moving every read; the
            # first build logged its changes (see limitlog.SMU_LIMIT_KEYS)
            d["changed"] = {k: v for k, v in d.get("changed", {}).items()
                            if k not in IGNORED_LIMITS}
            if d["changed"]:
                rows.append({"ts": r["ts"], "detail": json.dumps(d)})
        self.w(f"\n== SMU limit changes: {len(rows)} ==")
        if not rows:
            return
        counts = {}
        for r in rows:
            for k in json.loads(r["detail"] or "{}").get("changed", {}):
                counts[k] = counts.get(k, 0) + 1
        self.w("  changes per limit: " + ", ".join(f"{k} {n}" for k, n in
                                                   sorted(counts.items(), key=lambda x: -x[1])))
        self.w("  the last 20, with what the machine was doing:")
        out = []
        for r in rows[-20:]:
            d = json.loads(r["detail"] or "{}")
            ch = "; ".join(f"{k} {num(a)}->{num(b)}" for k, (a, b) in d.get("changed", {}).items())
            out.append([local(r["ts"]), ch, d.get("power_src") or "-", d.get("power_mode") or "-",
                        num(d.get("pd_w"), 0), f"{d.get('batt_state') or '-'} "
                        f"{num(d.get('batt_rate_w'), 0)}W", num(d.get("ec_apu"), 0),
                        num(d.get("ec_ambient"), 0), num(d.get("fan_l"), 0),
                        num(d.get("cpu_util"), 0), num(d.get("stapm_v"))])
        table(self.out, ["when", "changed", "source", "mode", "PD W", "battery", "apu C",
                         "amb C", "fan", "load%", "stapm W"], out, indent="    ")

    def processes(self):
        rows = self.q("SELECT component, reason, duration, top_procs FROM episodes WHERE "
                      "start>=? AND top_procs IS NOT NULL", self.since)
        by = {}
        for r in rows:
            agg = by.setdefault((r["component"], r["reason"]), {})
            for part in (r["top_procs"] or "").split(";"):
                name, _, p = part.rpartition(":")
                try:
                    agg[name] = agg.get(name, 0.0) + float(p) * (r["duration"] or 0)
                except ValueError:
                    pass
        if not by:
            return
        self.w("\n== Busiest processes during episodes (CPU % of the machine, "
               "time-weighted) ==")
        for (comp, reason), agg in sorted(by.items()):
            tot = sum(r["duration"] or 0 for r in rows
                      if r["component"] == comp and r["reason"] == reason) or 1
            top = sorted(agg.items(), key=lambda kv: -kv[1])[:6]
            self.w(f"  {comp} {reason:12s} " + ", ".join(f"{n} {v / tot:.0f}%" for n, v in top))

    def events(self):
        rows = self.q("SELECT kind, COUNT(*) n FROM events WHERE ts>=? GROUP BY kind "
                      "ORDER BY n DESC", self.since)
        self.w("\n== Events ==")
        self.w("  " + ", ".join(f"{r['kind']} {r['n']}" for r in rows))
        last = self.q("SELECT ts, kind, detail FROM events WHERE ts>=? AND kind NOT IN "
                      "('smu_limits','message') ORDER BY ts DESC LIMIT 12", self.since)
        for r in reversed(last):
            d = json.loads(r["detail"] or "{}")
            brief = {k: d[k] for k in ("value", "was", "ac", "w", "port", "changed",
                                       "seconds", "connected", "version") if k in d}
            self.w(f"    {local(r['ts'])}  {r['kind']:12s} {json.dumps(brief)}")


IGNORED_LIMITS = {"stt_dgpu_l"}

# reason -> (limit column, value column, unit) for the deep dive
LIMIT_COLS = {
    "stapm": ("stapm_l", "stapm_v", "W"), "slow": ("slow_l", "slow_v", "W"),
    "fast": ("fast_l", "fast_v", "W"), "tdc": ("tdc_l", "tdc_v", "A"),
    "edc": ("edc_l", "edc_v", "A"), "tdc_soc": ("tdc_soc_l", "tdc_soc_v", "A"),
    "edc_soc": ("edc_soc_l", "edc_soc_v", "A"), "thm": ("thm_l", "thm_v", "C"),
    "stt_apu": ("stt_apu_l", "stt_apu_v", "C"),
}


# --- CSV export ------------------------------------------------------------

def export_csv(db, since, out_dir, out):
    os.makedirs(out_dir, exist_ok=True)
    for name, tcol in (("samples", "ts"), ("episodes", "start"), ("events", "ts"),
                       ("tally", None), ("meta", None)):
        where, args = "", ()
        if tcol and since:
            where, args = f" WHERE {tcol}>=?", (since,)
        cur = db.execute(f"SELECT * FROM {name}{where}", args)
        cols = [c[0] for c in cur.description]
        path = os.path.join(out_dir, f"{name}.csv")
        n = 0
        with open(path, "w", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            wr.writerow((["local_time"] if tcol else []) + cols)
            for row in cur:
                lt = [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row[tcol]))
                      if row[tcol] is not None else ""] if tcol else []
                wr.writerow(lt + list(row))
                n += 1
        out.write(f"  {path}: {n} rows\n")
    # the raw SMU tables, decoded to float32s, one table per row
    path = os.path.join(out_dir, "pmtable.csv")
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        width = 0
        rows = db.execute("SELECT ts, version, episode, why, data FROM pmtable WHERE ts>=? "
                          "ORDER BY ts", (since or 0,)).fetchall()
        decoded = []
        for r in rows:
            raw = zlib.decompress(r["data"])
            vals = struct.unpack(f"<{len(raw) // 4}f", raw[:len(raw) // 4 * 4])
            width = max(width, len(vals))
            decoded.append((r, vals))
        wr.writerow(["local_time", "ts", "version", "episode", "why"]
                    + [f"f{i}" for i in range(width)])
        for r, vals in decoded:
            wr.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
                         r["ts"], r["version"], r["episode"], r["why"]]
                        + [f"{v:.6g}" for v in vals])
            n += 1
    out.write(f"  {path}: {n} rows\n")


def report(db_path=None, days=None, since=None, csv_dir=None, out=None):
    """Print the report (and optionally export CSVs); returns an exit code."""
    out = out or sys.stdout
    path = db_path or default_db()
    if not os.path.exists(path):
        out.write(f"No limit log at {path} yet. It is written by the FW16Sysmon "
                  f"service (LIMITLOG_ENABLE in sysmon.ini).\n")
        return 1
    if days:
        since = time.time() - float(days) * 86400.0
    try:
        db = connect(path)
    except sqlite3.Error as err:
        out.write(f"Cannot open {path}: {err}\n")
        return 1
    try:
        Report(db, since or 0.0, out).run(path)
        if csv_dir:
            out.write(f"\nExporting to {csv_dir}\n")
            export_csv(db, since or 0.0, csv_dir, out)
    except sqlite3.Error as err:
        out.write(f"\nReading {path} failed: {err}\n")
        return 1
    finally:
        db.close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=None,
                    help=f"the database (default: {default_db()})")
    ap.add_argument("--days", type=float, default=None, help="only the last N days")
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="only from this local date on")
    ap.add_argument("--csv", default=None, metavar="DIR",
                    help="also export every table as CSV into DIR")
    a = ap.parse_args(argv)
    since = None
    if a.since:
        since = time.mktime(time.strptime(a.since, "%Y-%m-%d"))
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    return report(a.db, days=a.days, since=since, csv_dir=a.csv)


if __name__ == "__main__":
    sys.exit(main())
