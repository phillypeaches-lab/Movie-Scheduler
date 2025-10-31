# version: Render timezone-safe
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone
import requests
from bs4 import BeautifulSoup
from itertools import permutations, product
import re
import pytz
import sys

app = Flask(__name__)

# -------------------------------------------------
# CONFIGURATION
# -------------------------------------------------
THEATER_ID = "4343"
BASE_URL = f"https://www.bigscreen.com/Marquee.php?theater={THEATER_ID}&view=sched"
THEATER_TZ = pytz.timezone("America/New_York")  # always interpret showtimes in this zone


# -------------------------------------------------
# TIME HELPERS
# -------------------------------------------------
def get_current_times():
    """Return current UTC and local theater time (Render runs in UTC)."""
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(THEATER_TZ)
    print(f"[DEBUG] Render UTC time:   {now_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}", flush=True)
    print(f"[DEBUG] Theater local time: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}", flush=True)
    sys.stdout.flush()
    return now_utc, now_local


def parse_bigscreen_time(t_str, base_date=None):
    """Convert BigScreen time (like '10:30a' or '7:20') → localized datetime (aware)."""
    if base_date is None:
        base_date = datetime.now(THEATER_TZ).replace(hour=0, minute=0, second=0, microsecond=0)

    t_str = t_str.strip().lower()
    is_am = t_str.endswith("a")
    if is_am:
        t_str = t_str[:-1]

    hour, minute = map(int, t_str.split(":"))
    if not is_am and hour != 12:
        hour += 12

    # Localize to theater timezone
    local_dt = THEATER_TZ.localize(base_date.replace(hour=hour, minute=minute))
    return local_dt


def format_showtime(dt):
    """Format datetime in local theater time for display."""
    local_dt = dt.astimezone(THEATER_TZ)
    hour = local_dt.hour
    minute = local_dt.minute
    if hour < 12:
        display_hour = hour if hour != 0 else 12
        return f"{display_hour}:{minute:02d}a"
    else:
        display_hour = hour if hour <= 12 else hour - 12
        return f"{display_hour}:{minute:02d}"


# -------------------------------------------------
# SCRAPER FUNCTIONS
# -------------------------------------------------
def fetch_available_days():
    url = BASE_URL
    resp = requests.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    today_local = datetime.now(THEATER_TZ).date()
    days = []

    for cell in soup.select("td.scheddaterow, td.scheddaterow_sel"):
        title = cell.get_text(strip=True).replace("\n", " ")
        href = cell.get("onclick", "") or ""
        link = cell.find("a")
        if link and link.has_attr("href"):
            href = link["href"]

        match = re.search(r"showdate=(\d{4}-\d{2}-\d{2})", href)
        if match:
            date_obj = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        else:
            date_obj = today_local

        if date_obj >= today_local:
            days.append({"title": title, "date": date_obj.strftime("%Y-%m-%d")})

    return days


def fetch_showtimes_by_scraping(showdate=None):
    """Scrape all showtimes and filter out past ones (based on theater local time)."""
    url = BASE_URL
    if showdate:
        url += f"&showdate={showdate}"

    resp = requests.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    today_local = datetime.now(THEATER_TZ).date()
    target_date = today_local if showdate is None else datetime.strptime(showdate, "%Y-%m-%d").date()
    now_local = datetime.now(THEATER_TZ)

    merged = {}

    for row in soup.select("tr.graybar_0, tr.graybar_1"):
        title_elem = row.select_one("td.col_movie a[href*='NowShowing.php?movie=']")
        if not title_elem:
            continue

        name = title_elem.get_text(strip=True)
        title_key = name.lower()

        runtime_elem = row.select_one("td.col_movie span.small")
        runtime = 120
        if runtime_elem:
            match = re.search(r"(\d+):(\d+)", runtime_elem.get_text(strip=True))
            if match:
                hours, minutes = int(match.group(1)), int(match.group(2))
                runtime = hours * 60 + minutes

        showtime_cell = row.select_one("td.col_showtimes")
        showtimes = []
        if showtime_cell:
            cell_text = showtime_cell.get_text(" ", strip=True).lower()
            found_times = re.findall(r"\b\d{1,2}:\d{2}a?\b", cell_text)
            showtimes.extend(found_times)

        showtimes = sorted(set(showtimes))
        filtered_showtimes = []
        for st in showtimes:
            try:
                dt_local = parse_bigscreen_time(st, base_date=datetime.strptime(showdate, "%Y-%m-%d"))
            except Exception:
                continue
            if target_date == today_local and dt_local < now_local:
                continue
            filtered_showtimes.append(st)

        if filtered_showtimes:
            merged.setdefault(title_key, {"name": name, "runtime": runtime, "showtimes": []})
            merged[title_key]["showtimes"].extend(filtered_showtimes)

    for v in merged.values():
        v["showtimes"] = sorted(set(v["showtimes"]))

    return list(merged.values())


# -------------------------------------------------
# SCHEDULER
# -------------------------------------------------
def schedule_movies(selected_movies, min_gap=-5):
    movie_showtimes = []

    for m in selected_movies:
        showtimes = []
        for st in m["showtimes"]:
            try:
                start = parse_bigscreen_time(st)
            except Exception:
                continue
            end = start + timedelta(minutes=m["runtime"])
            showtimes.append({"movie": m["name"], "start": start, "end": end})
        if showtimes:
            movie_showtimes.append(showtimes)

    all_valid_schedules = []
    total_selected = len(selected_movies)

    for n in range(total_selected, 0, -1):
        for subset in permutations(movie_showtimes, n):
            for combo in product(*subset):
                valid = True
                total_gap = timedelta(0)
                for i in range(len(combo) - 1):
                    gap = combo[i + 1]["start"] - combo[i]["end"]
                    if gap < timedelta(minutes=min_gap):
                        valid = False
                        break
                    total_gap += gap
                if valid:
                    all_valid_schedules.append({
                        "schedule": combo,
                        "total_gap": total_gap,
                        "movies_count": len(combo)
                    })
        if all_valid_schedules:
            break

    all_valid_schedules.sort(key=lambda x: (-x["movies_count"], x["total_gap"]))
    return all_valid_schedules


# -------------------------------------------------
# ROUTES
# -------------------------------------------------
@app.route("/days", methods=["GET"])
def get_days():
    return jsonify(fetch_available_days())


@app.route("/movies", methods=["GET"])
def get_movies():
    showdate = request.args.get("showdate")
    get_current_times()  # debug
    movies = fetch_showtimes_by_scraping(showdate)
    return jsonify([m["name"] for m in movies])


@app.route("/schedule", methods=["POST"])
def get_schedule():
    get_current_times()  # debug on Render

    data = request.get_json()
    if not data:
        return jsonify({"error": "Must provide JSON body"}), 400

    selected_titles = data.get("movies", [])
    min_gap = data.get("min_gap", -5)
    showdate = data.get("showdate")
    show_more = data.get("show_more", False)
    MoviesSelected = data.get("MoviesSelected")
    start_time_str = data.get("start_time")
    end_time_str = data.get("end_time")

    if not showdate:
        return jsonify({"error": "Must provide showdate"}), 400

    movies = fetch_showtimes_by_scraping(showdate)
    selected_movies = [m for m in movies if m["name"] in selected_titles]

    def filter_timeframe(m):
        filtered_showtimes = []
        for st in m["showtimes"]:
            start_dt = parse_bigscreen_time(st, base_date=datetime.strptime(showdate, "%Y-%m-%d"))
            end_dt = start_dt + timedelta(minutes=m["runtime"])

            if start_time_str:
                start_limit = THEATER_TZ.localize(datetime.strptime(f"{showdate} {start_time_str}", "%Y-%m-%d %H:%M"))
                if start_dt < start_limit:
                    continue

            if end_time_str:
                end_limit = THEATER_TZ.localize(datetime.strptime(f"{showdate} {end_time_str}", "%Y-%m-%d %H:%M"))
                if end_dt > end_limit:
                    continue

            filtered_showtimes.append(st)
        m["showtimes"] = filtered_showtimes
        return bool(filtered_showtimes)

    selected_movies = list(filter(filter_timeframe, selected_movies))
    if not selected_movies:
        return jsonify({"error": "No movies match the selected time window"}), 400

    all_schedules = schedule_movies(selected_movies, min_gap)
    if not all_schedules:
        return jsonify({"error": "No valid schedules found"}), 400

    best_schedule = all_schedules[0]

    def format_schedule(s):
        lines = [f"Date: {showdate}", f"{len(s['schedule'])} out of {MoviesSelected} movies selected"]
        for item in s["schedule"]:
            start_str = format_showtime(item["start"])
            end_str = format_showtime(item["end"])
            lines.append(f"{item['movie']}: {start_str} - {end_str}")
        lines.append(f"\nTotal gap time: {s['total_gap']}")
        return "\n".join(lines)

    if show_more:
        return jsonify([format_schedule(s) for s in all_schedules])
    else:
        return format_schedule(best_schedule), 200, {"Content-Type": "text/plain; charset=utf-8"}


# -------------------------------------------------
# RUN LOCALLY
# -------------------------------------------------
if __name__ == "__main__":
    get_current_times()  # print local+UTC when starting
    app.run(host="0.0.0.0", port=5003)
