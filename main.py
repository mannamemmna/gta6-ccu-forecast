"""gta6-ccu-forecast: hour-by-hour forecast of GTA VI's launch-day Twitch concurrency.

The model is an analogue (comparable-launch) forecaster on the log scale.  Hour 1 of the
release day is known; for every later hour it asks what the comparable launches did between
their own hour 1 and that hour, weights each comparable by how close its hour-1 audience was
to ours, and reads weighted quantiles off the resulting distribution of log growth ratios.
Everything runs on the standard library, with no randomness and no clock, so two runs on the
same case write the same bytes.

Input:  $MAKEFUN_INPUT_DIR (default /input)  -- every *.in case file, in name order.
Output: $MAKEFUN_OUTPUT_DIR (default /output) -- <id>.out, {"hours": [{"hour", "q": [5]}, ...]}.
"""

import json
import math
import os

INPUT_DIR = os.environ.get("MAKEFUN_INPUT_DIR") or "/input"
OUTPUT_DIR = os.environ.get("MAKEFUN_OUTPUT_DIR") or "/output"

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.6448536269514722, -0.6744897501960817, 0.0, 0.6744897501960817, 1.6448536269514722]

BANDWIDTH = 1.0      # log-viewers bandwidth of the hour-1 similarity weight
MIN_PAIRS = 3        # below this many comparables for an hour, fall back to the baseline shape
BASE_SIGMA = 0.15    # baseline's per-sqrt-hour log spread
FLOOR = 1e-9         # smallest value the format allows above zero
CAP = 1e9            # largest value the format allows


def release_day_hours(comparable):
    """{hour: viewers} for one comparable's own release day, hours numbered from its first hour."""
    day = str(comparable.get("day_start") or "")[:10]
    out = {}
    for row in comparable.get("hourly") or ():
        start = str(row.get("from") or "")
        if len(start) < 13 or start[:10] != day:
            continue
        viewers = row.get("viewers")
        if viewers is None:
            continue
        hour = int(start[11:13]) + 1
        out[hour] = max(1.0, float(viewers))
    return out


def similarity_weight(comparable_first, target_first, bandwidth=BANDWIDTH):
    """Gaussian weight on how far a comparable's hour-1 audience sits from the case's, in logs."""
    gap = math.log(comparable_first / target_first) / bandwidth
    return math.exp(-0.5 * gap * gap)


def quantile_levels(values, weights, levels=LEVELS):
    """Weighted quantiles of `values`: sort ascending, accumulate weight, interpolate between neighbours.

    The level's position is where the cumulative weight reaches level * total weight; the value
    there is read by straight-line interpolation between the r of the bracketing observations,
    not by snapping to the lower one. Interpolating matters because the weights are a kernel over
    hour-1 size: under a step rule a whole quantile jumps when one comparable's weight crosses a
    level, while the interpolated quantile slides.
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    ordered = [values[i] for i in order]
    ordered_weights = [weights[i] for i in order]
    total = sum(ordered_weights)
    if not (total > 0.0):
        return [ordered[len(ordered) // 2] for _ in levels]
    cumulative = []
    running = 0.0
    for weight in ordered_weights:
        running += weight
        cumulative.append(running / total)
    out = []
    for level in levels:
        if level <= cumulative[0]:
            out.append(ordered[0])
            continue
        if level >= cumulative[-1]:
            out.append(ordered[-1])
            continue
        for j in range(1, len(cumulative)):
            right = cumulative[j]
            if level <= right:
                left = cumulative[j - 1]
                span = right - left
                frac = 0.0 if span <= 0.0 else (level - left) / span
                out.append(ordered[j - 1] + frac * (ordered[j] - ordered[j - 1]))
                break
    return out


def baseline_quantiles(first, hour):
    """The house shape: persistence with a random walk's widening spread in log space."""
    scale = BASE_SIGMA * math.sqrt(hour - 1)
    return [first * math.exp(z * scale) for z in Z]


def forecast_hour(first, hour, pool):
    """Five quantiles of hour `hour`'s audience, from the comparables' log growth ratios.

    The fallback is not decoration: the final case's pool is the 42 comparables the house ships,
    but a comparable whose release day is short, or whose hour 1 is missing, contributes nothing,
    and a case with fewer than MIN_PAIRS usable neighbours has no empirical shape to read. Then
    the only honest answer is the house baseline's: persistence, with the spread of a random walk
    in log space widening as the square root of the hours since the print.
    """
    ratios = []
    weights = []
    for hours in pool:
        start = hours.get(1)
        later = hours.get(hour)
        if not start or not later or start <= 0 or later <= 0:
            continue
        ratios.append(math.log(later / start))
        weights.append(similarity_weight(start, first))
    if len(ratios) < MIN_PAIRS:
        return baseline_quantiles(first, hour)
    total_weight = sum(weights)
    if not (total_weight > 0.0) or not math.isfinite(total_weight):
        # Every comparable sits absurdly far from this hour-1 size in logs: the kernel has
        # underflowed to zero and the weighted quantiles would be meaningless. Fall back.
        return baseline_quantiles(first, hour)
    level_ratios = quantile_levels(ratios, weights)
    return [first * math.exp(r) for r in level_ratios]


def forecast_case(case):
    known = case.get("known") or []
    first_row = next((row for row in known if row.get("hour") == 1), None)
    first = 1.0
    if first_row is not None:
        viewers = first_row.get("viewers")
        if viewers is not None:
            first = float(viewers)
    if not (first >= 1.0):
        first = 1.0
    pool = [release_day_hours(comparable) for comparable in case.get("comparables") or ()]
    rows = []
    for row in case.get("forecast") or ():
        hour = int(row["hour"])
        rows.append({"hour": hour, "q": forecast_hour(first, hour, pool)})
    return {"hours": rows}


def sanitise(row):
    """Clamp a row's quantiles into (0, CAP], force them non-decreasing, round for stable output.

    exp() can overflow to inf on a daft ratio, and a float can land on 0.0; both would make the
    row invalid and lose the whole case, so clamp instead of trusting the model. Rounding to six
    decimals keeps the JSON identical run to run without moving a viewer we could measure.
    """
    cleaned = []
    previous = 0.0
    for value in row:
        if not isinstance(value, float):
            value = float(value)
        if value != value:  # NaN, which should never reach here but must not be written out
            value = CAP
        value = min(CAP, max(FLOOR, value))
        if value < previous:
            value = previous
        previous = value
        cleaned.append(round(value, 6))
    return cleaned


def write_case(in_path, out_path):
    with open(in_path, "r", encoding="utf-8") as handle:
        case = json.load(handle)
    forecast = forecast_case(case)
    for row in forecast["hours"]:
        row["q"] = sanitise(row["q"])
    payload = json.dumps(forecast, separators=(",", ":"), allow_nan=False)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(payload + "\n")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for name in sorted(os.listdir(INPUT_DIR)):
        if not name.endswith(".in"):
            continue
        stem = name[:-3]
        write_case(os.path.join(INPUT_DIR, name), os.path.join(OUTPUT_DIR, stem + ".out"))


if __name__ == "__main__":
    main()
