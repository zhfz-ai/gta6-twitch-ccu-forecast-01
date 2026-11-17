#!/usr/bin/env python3
"""Forecast GTA VI's launch-day Twitch viewers, hour by hour.

Reads one case (/input/<id>.in, schema makefun/twitch-ccu-case/v1) and
writes /output/<id>.out with five quantiles (5/25/50/75/95%) of the
category's concurrent viewers for each of hours 2..24 of release day.

Method (chosen over 20+ candidates by leave-one-out grading on the 37
past big launches shipped as comparables; see JUDGE-SCORECARD.md):

  A launch day's shape is mostly decided by how big hour 1 already is.
  When hour 1 is huge the unlock wave is already spent and the day stays
  near flat; when hour 1 is small the US unlocks still ahead lift the
  day several-fold by the evening. So for each hour h we fit, across
  past launches, a one-predictor regression of the log ratio

      log(viewers_h / viewers_1) = a_h + b_h * log(viewers_1)

  and read the forecast off that line at this case's hour-1 print. The
  spread around the line is the regression's residual standard deviation
  (floored at 0.35 log units, so the 5%-95% band stays honest even when
  the fit looks tight on a small pool), turned into quantiles with the
  standard normal's z-scores. The pool is the launches whose release day
  looks like a rolling-midnight console launch: hour-1 print at least
  5,000 viewers and a day peak 1.2x-6x hour 1 (this drops simultaneous
  global unlocks, whose hour 1 is near zero, and flat early-access days,
  whose peak is already spent). The pool is rebuilt from the case file
  on every run, so the four practice launches join it in the final.

  The house baseline (flat at hour 1, spread 0.15*sqrt(h-1)) scores
  ~0.39 pinball loss on the practice; this forecaster scores ~0.19-0.26
  leave-one-out on comparable launches and ~0.13 median on the
  rolling-midnight subset.

Python 3.13, standard library only, no network, deterministic: the
same case always yields the same bytes.
"""

import json
import math
import os
from datetime import datetime

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
# Standard normal quantiles at LEVELS.
Z = [-1.6448536269514722, -0.6744897501960817, 0.0,
     0.6744897501960817, 1.6448536269514722]
# House baseline spread, used only as a last-resort fallback.
BASELINE_SIGMA = 0.15
# Residual spread is never trusted below this many log units.
SD_FLOOR = 0.35
# A launch joins the analogue pool only when its release day is
# complete enough and its shape looks like a rolling-midnight launch.
MIN_DAY_HOURS = 20
POOL_MIN_H1 = 5000.0
POOL_PEAK_LO = 1.2
POOL_PEAK_HI = 6.0
RELAXED_MIN_H1 = 1000.0
RELAXED_PEAK_LO = 1.0
RELAXED_PEAK_HI = 10.0
MIN_POOL = 8
MIN_POOL_ANY = 3
# Sanity guard on the fitted median ratio (never binds in practice).
MEDIAN_RATIO_LO = 0.02
MEDIAN_RATIO_HI = 60.0


def parse_time(text):
    """Parse an ISO 8601 timestamp ending in Z into a datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def release_day_hours(comparable):
    """Map hour-of-day 1..24 to viewers for a comparable's release day.

    Returns {} when the comparable has no usable release-day curve.
    """
    try:
        day_start = parse_time(comparable["day_start"])
    except (KeyError, TypeError, ValueError):
        return {}
    hours = {}
    for point in comparable.get("hourly") or []:
        try:
            at = parse_time(point["from"])
            viewers = float(point["viewers"])
        except (KeyError, TypeError, ValueError):
            continue
        if viewers <= 0:
            continue
        hour = int((at - day_start).total_seconds() // 3600) + 1
        if 1 <= hour <= 24:
            hours[hour] = viewers
    return hours


def is_weak(comparable):
    """True for launches the case notes flag as weak analogues."""
    unlock = comparable.get("unlock") or ""
    return "Tier: weak" in unlock


def build_pool(comparables):
    """Pick the analogue launches for the log-ratio regressions.

    Strict pool first (rolling-midnight shape); relax the peak window
    when too few qualify; fall back to every complete launch; empty
    only when the case carries no usable comparable at all.
    """
    complete = []
    for comparable in sorted(comparables, key=lambda c: c.get("name", "")):
        if is_weak(comparable):
            continue
        hours = release_day_hours(comparable)
        if 1 not in hours or len(hours) < MIN_DAY_HOURS:
            continue
        complete.append(hours)
    if not complete:
        return []
    strict = []
    for hours in complete:
        peak = max(hours[h] / hours[1] for h in hours if h >= 2)
        if hours[1] >= POOL_MIN_H1 and POOL_PEAK_LO <= peak <= POOL_PEAK_HI:
            strict.append(hours)
    if len(strict) >= MIN_POOL:
        return strict
    relaxed = []
    for hours in complete:
        peak = max(hours[h] / hours[1] for h in hours if h >= 2)
        if hours[1] >= RELAXED_MIN_H1 and RELAXED_PEAK_LO <= peak <= RELAXED_PEAK_HI:
            relaxed.append(hours)
    if len(relaxed) >= MIN_POOL:
        return relaxed
    if len(complete) >= MIN_POOL_ANY:
        return complete
    return strict or relaxed or complete


def fit_line(xs, ys):
    """Ordinary least squares y = a + b*x; returns (a, b, resid_sd)."""
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return mean_y, 0.0, 0.5
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = mean_y - b * mean_x
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    var = sum(r * r for r in resid) / n
    return a, b, math.sqrt(var)


def round2(value):
    """Round to 2 decimals, half up for positive numbers."""
    return math.floor(value * 100 + 0.5) / 100


def forecast_hour(pool, p1, hour):
    """Five quantiles for one hour, from the fitted log-ratio line."""
    xs = []
    ys = []
    for hours in pool:
        if hour in hours and 1 in hours:
            xs.append(math.log(hours[1]))
            ys.append(math.log(hours[hour] / hours[1]))
    if len(xs) < 3:
        # Too little data for this hour: fall back to the house shape.
        sigma = BASELINE_SIGMA * math.sqrt(hour - 1)
        return [round2(max(1.0, p1 * math.exp(z * sigma))) for z in Z]
    a, b, sd = fit_line(xs, ys)
    log_p1 = math.log(p1)
    median_log_ratio = a + b * log_p1
    median_log_ratio = min(max(median_log_ratio,
                               math.log(MEDIAN_RATIO_LO)),
                           math.log(MEDIAN_RATIO_HI))
    spread = max(sd, SD_FLOOR)
    quantiles = []
    for z in Z:
        q = p1 * math.exp(median_log_ratio + z * spread)
        quantiles.append(round2(min(max(q, 1.0), 1e9)))
    # Enforce non-decreasing quantiles after rounding.
    for i in range(1, len(quantiles)):
        if quantiles[i] < quantiles[i - 1]:
            quantiles[i] = quantiles[i - 1]
    return quantiles


def baseline_hour(p1, hour):
    """House persistence forecast for one hour (fallback only)."""
    sigma = BASELINE_SIGMA * math.sqrt(hour - 1)
    return [round2(max(1.0, p1 * math.exp(z * sigma))) for z in Z]


def hour_one_print(case):
    """Hour 1's viewer count, or the last known hourly point."""
    for row in case.get("known") or []:
        if row.get("hour") == 1:
            return max(1.0, float(row["viewers"]))
    history = case.get("history_hourly") or []
    if history:
        return max(1.0, float(history[-1]["viewers"]))
    return 1.0


def forecast_case(case):
    """Forecast hours 2..24 of one case as {"hours": [...]}."""
    p1 = hour_one_print(case)
    pool = build_pool(case.get("comparables") or [])
    hours = []
    for row in sorted(case.get("forecast") or [], key=lambda r: r["hour"]):
        hour = int(row["hour"])
        if pool:
            q = forecast_hour(pool, p1, hour)
        else:
            q = baseline_hour(p1, hour)
        hours.append({"hour": hour, "q": q})
    return {"hours": hours}


def main():
    input_dir = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
    output_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    os.makedirs(output_dir, exist_ok=True)
    names = sorted(f for f in os.listdir(input_dir) if f.endswith(".in"))
    for name in names:
        case_id = name[: -len(".in")]
        with open(os.path.join(input_dir, name), "r", encoding="utf-8") as f:
            case = json.load(f)
        forecast = forecast_case(case)
        out_path = os.path.join(output_dir, case_id + ".out")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(forecast) + "\n")


if __name__ == "__main__":
    main()
