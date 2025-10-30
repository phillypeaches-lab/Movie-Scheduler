#4+new version to tackle timezone shift
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from itertools import permutations, product
import re
from datetime import datetime, timezone
import sys  # 👈 add this import at the top
import pytz
THEATER_TZ = pytz.timezone("America/New_York")

def get_current_time():
    """Return the current UTC time and a formatted string for debugging."""
    now_utc = datetime.now(timezone.utc)
    print(f"[DEBUG] Current UTC time: {now_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}", flush=True)
    sys.stdout.flush()  # 👈 ensure it appears in logs
    return now_utc
def get_today_utc_date():
    """Return today's date in UTC (for consistent comparison on Render)."""
    return datetime.now(timezone.utc).date()


app = Flask(__name__)

# -------------------------------------------------
# CONFIGURATION
# -------------------------------------------------
THEATER_ID = "4343"
BASE_URL = f"https://www.bigscreen.com/Marquee.php?theater={THEATER_ID}&view=sched"


# -------------------------------------------------
# UTILITIES
# -------------------------------------------------
def format_showtime(dt):
    """Format datetime to BigScreen style (AM -> add 'a', PM -> just time)."""
    hour = dt.hour
    minute = dt.minute
    if hour < 12:
        display_hour = hour if hour != 0 else 12
        return f"{display_hour}:{minute:02d}a"
    else:
        display_hour = hour if hour <= 12 else hour - 12
        return f"{display_hour}:{minute:02d}"


def parse_bigscreen_time(t_str, base_date=None):
    """Convert BigScreen time (like '10:30a' or '7:20') into UTC-aware datetime."""
    if base_date is None:
        base_date = datetime.now(THEATER_TZ).replace(hour=0, minute=0, second=0, microsecond=0)

    t_str = t_str.strip().lower()
    is_am = t_str.endswith("a")
    if is_am:
        t_str = t_str[:-1]

    hour, minute = map(int, t_str.split(":"))
    if not is_am and hour != 12:
        hour += 12
    if is_am and hour == 12:
        hour = 0  # handle 12:xxa as midnight

    local_dt = base_date.replace(hour=hour, minute=minute, tzinfo=THEATER_TZ)
    return local_dt.astimezone(timezone.utc)




# -------------------------------------------------
# SCRAPER FUNCTIONS
# -------------------------------------------------
def fetch_available_days():
    """Scrape available date tabs from BigScreen, only future/present days."""
    url = f"https://www.bigscreen.com/Marquee.php?theater={THEATER_ID}&view=sched"
    resp = requests.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    today = get_today_utc_date()
    days = []

    for cell in soup.select("td.scheddaterow, td.scheddaterow_sel"):
        title = cell.get_text(strip=True).replace("\n", " ")

        href = ""
        link = cell.find("a")
        if link and link.has_attr("href"):
            href = link["href"]
        else:
            href = cell.get("onclick", "")

        match = re.search(r"showdate=(\d{4}-\d{2}-\d{2})", href)
        if match:
            date_obj = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        else:
            date_obj = today

        if date_obj < today:
            continue

        days.append({
            "title": title,
            "date": date_obj.strftime("%Y-%m-%d")
        })

    return days


def fetch_showtimes_by_scraping(showdate=None):
    """Scrape movie showtimes from BigScreen and merge exact duplicate titles. Prints all detected showtimes for debugging."""
    url = f"https://www.bigscreen.com/Marquee.php?theater={THEATER_ID}&view=sched"
    if showdate:
        url += f"&showdate={showdate}"


    resp = requests.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    today = get_today_utc_date()
    target_date = today if showdate is None else datetime.strptime(showdate, "%Y-%m-%d").date()
    now = datetime.now(timezone.utc)

    merged = {}

    for row in soup.select("tr.graybar_0, tr.graybar_1"):
        title_elem = row.select_one("td.col_movie a[href*='NowShowing.php?movie=']")
        if not title_elem:
            continue

        name = title_elem.get_text(strip=True)
        title_key = name.strip().lower()

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
            # Get entire text of the showtime cell
            cell_text = showtime_cell.get_text(" ", strip=True).lower()
            # Find ALL times like "11:40a" or "3:55"
            found_times = re.findall(r"\b\d{1,2}:\d{2}a?\b", cell_text)
            showtimes.extend(found_times)


        # Deduplicate and filter out past times
        showtimes = sorted(set(showtimes))
        filtered_showtimes = []
        for st in showtimes:
            try:
                dt = parse_bigscreen_time(st)
            except Exception:
                continue


            local_now = datetime.now(THEATER_TZ)
            dt_local = dt.astimezone(THEATER_TZ)
            if target_date == local_now.date() and dt_local < local_now:
                continue


            filtered_showtimes.append(st)


        if not filtered_showtimes:
            continue

        if title_key in merged:
            merged[title_key]["showtimes"].extend(filtered_showtimes)
        else:
            merged[title_key] = {
                "name": name.strip(),
                "runtime": runtime,
                "showtimes": filtered_showtimes
            }


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
            except:
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
    url = f"https://www.bigscreen.com/Marquee.php?theater={THEATER_ID}&view=sched"
    resp = requests.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    dates = set()
    sel = soup.select_one("td.scheddaterow_sel")
    if sel:
        text = sel.get_text(strip=True)
        a = sel.select_one("a")
        if a and "showdate" in a["href"]:
            m = re.search(r"showdate=(\d{4}-\d{2}-\d{2})", a["href"])
            if m:
                dates.add(m.group(1))
        else:
            dates.add(datetime.now().strftime("%Y-%m-%d"))

    for td in soup.select("td.scheddaterow"):
        a = td.select_one("a")
        if a and "showdate" in a["href"]:
            m = re.search(r"showdate=(\d{4}-\d{2}-\d{2})", a["href"])
            if m:
                dates.add(m.group(1))

    today = datetime.now().date()
    showdates = sorted([d for d in dates if datetime.strptime(d, "%Y-%m-%d").date() >= today])
    return jsonify(showdates)


@app.route("/movies", methods=["GET"])
def get_movies():
    # Debug print to confirm UTC time on Render
    current_time = get_current_time()

    showdate = request.args.get("showdate")
    movies = fetch_showtimes_by_scraping(showdate)
    return jsonify([m["name"] for m in movies])


@app.route("/schedule", methods=["POST"])
def get_schedule():
    print(f"[DEBUG] User selected window: {start_time_str}–{end_time_str} local ({THEATER_TZ})", flush=True)
    print(f"[DEBUG] Converted to UTC window: {start_limit_utc}–{end_limit_utc}", flush=True)

    current_time = get_current_time()  # 🔥 This should print to Render logs
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
    if not movies:
        return jsonify({"error": f"No movies found for {showdate}"}), 404

    selected_movies = [m for m in movies if m["name"] in selected_titles]
    if not selected_movies:
        return jsonify({"error": "None of the selected movies are available on this date"}), 404

    def filter_timeframe(m):
        filtered_showtimes = []
        for st in m["showtimes"]:
            # Parse showtime and make it UTC-aware
            start_dt = parse_bigscreen_time(st, base_date=datetime.strptime(showdate, "%Y-%m-%d"))
            start_dt = start_dt.replace(tzinfo=timezone.utc)
            end_dt = start_dt + timedelta(minutes=m["runtime"])

            # Filter by start_time if provided
            if start_time_str:
                # interpret user input as local time
                start_limit_local = THEATER_TZ.localize(datetime.strptime(f"{showdate} {start_time_str}", "%Y-%m-%d %H:%M"))
                start_limit_utc = start_limit_local.astimezone(timezone.utc)
                if start_dt < start_limit_utc:
                    continue


            # Filter by end_time if provided
            if end_time_str:
                end_limit_local = THEATER_TZ.localize(datetime.strptime(f"{showdate} {end_time_str}", "%Y-%m-%d %H:%M"))
                end_limit_utc = end_limit_local.astimezone(timezone.utc)
                if end_dt > end_limit_utc:
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
        lines = [
            f"Date: {showdate}",
            f"{len(s['schedule'])} out of {MoviesSelected} movies selected"
        ]
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
    app.run(host="0.0.0.0", port=5004)
