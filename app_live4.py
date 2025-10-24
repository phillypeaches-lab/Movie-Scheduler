#3+time constraints (not working for show more schedules, try again)
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from itertools import permutations, product
import re
import pytz
from datetime import datetime

THEATER_TZ = pytz.timezone("America/New_York")

def now_local():
    return datetime.now(THEATER_TZ)

app = Flask(__name__)
print(f"[DEBUG] Render server time (UTC): {datetime.utcnow()}")
print(f"[DEBUG] Render local time (NY): {now_local()}")

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
    """Convert BigScreen time (like '10:30a' or '7:20') into timezone-aware datetime."""
    if base_date is None:
        base_date = now_local().replace(hour=0, minute=0, second=0, microsecond=0)

    t_str = t_str.strip().lower()
    is_am = t_str.endswith("a")
    if is_am:
        t_str = t_str[:-1]

    hour, minute = map(int, t_str.split(":"))
    if not is_am and hour != 12:
        hour += 12

    local_dt = base_date.replace(hour=hour, minute=minute)
    return THEATER_TZ.localize(local_dt) if local_dt.tzinfo is None else local_dt



# -------------------------------------------------
# SCRAPER FUNCTIONS
# -------------------------------------------------
def fetch_available_days():
    """Scrape available date tabs from BigScreen, only future/present days."""
    import re

    url = f"https://www.bigscreen.com/Marquee.php?theater={THEATER_ID}&view=sched"
    resp = requests.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    today = datetime.today().date()
    days = []

    for cell in soup.select("td.scheddaterow, td.scheddaterow_sel"):
        # Get display text like "Sat 10/18"
        title = cell.get_text(strip=True).replace("\n", " ")

        # Extract date from href or onclick if available
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
            # For selected day with no link, assume today
            date_obj = today

        # Skip past days
        if date_obj < today:
            continue

        days.append({
            "title": title,
            "date": date_obj.strftime("%Y-%m-%d")
        })

    return days




def fetch_showtimes_by_scraping(showdate=None):
    """Scrape movie showtimes from BigScreen and merge exact duplicate titles."""
    url = f"https://www.bigscreen.com/Marquee.php?theater={THEATER_ID}&view=sched"
    if showdate:
        url += f"&showdate={showdate}"
    resp = requests.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    now = now_local()
    today = now.date()
    target_date = now.date() if showdate is None else datetime.strptime(showdate, "%Y-%m-%d").date()
    print(f"[DEBUG] Server current time (NY local): {now}")

    print(f"[DEBUG] Server current time: {now} (local system time)")
    print(f"[DEBUG] Target date: {target_date}")

    merged = {}

    for row in soup.select("tr:has(td.col_movie a[href*='NowShowing.php?movie='])"):
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

        # --- Extract showtimes robustly ---
        showtimes = []

        showtimes_cell = row.select_one("td.col_showtimes")
        if showtimes_cell:
            # 1️⃣ Collect all visible text (including times outside <a>)
            text = showtimes_cell.get_text(" ", strip=True)

            # 2️⃣ Find all time-like patterns
            # Matches 11:40a, 2:00, 10:15p, 7:00pm, etc.
            time_matches = re.findall(r"\b\d{1,2}:\d{2}\s*(?:a|p|am|pm)?\b", text, flags=re.I)

            for st in time_matches:
                st_clean = st.lower().replace(" ", "")
                if not re.match(r"^\d{1,2}:\d{2}(?:a|p|am|pm)?$", st_clean):
                    continue

                # Normalize 'am'/'pm' -> 'a'/''
                st_clean = re.sub(r"am$", "a", st_clean)
                st_clean = re.sub(r"pm$", "", st_clean)

                try:
                    dt = parse_bigscreen_time(st_clean)
                    if target_date == today and dt < now:
                        continue
                    showtimes.append(st_clean)
                except Exception as e:
                    print(f"⚠️ Skipping invalid time '{st}' for {name}: {e}")

        if not showtimes:
            print(f"⚠️ No showtimes found for {name}, keeping movie anyway.")
            showtimes = ["(showtimes unavailable)"]


        # ✅ Merge if title already exists
        if title_key in merged:
            merged[title_key]["showtimes"].extend(showtimes)
        else:
            merged[title_key] = {
                "name": name.strip(),
                "runtime": runtime,
                "showtimes": showtimes
            }

    # ✅ Deduplicate and sort showtimes
    for v in merged.values():
        v["showtimes"] = sorted(set(v["showtimes"]))

    return list(merged.values())



# -------------------------------------------------
# SCHEDULER
# -------------------------------------------------
def schedule_movies(selected_movies, min_gap=-5):
    from itertools import product, permutations

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
            break  # Stop once we find schedules with max possible movies

    all_valid_schedules.sort(key=lambda x: (-x["movies_count"], x["total_gap"]))
    return all_valid_schedules


# -------------------------------------------------
# ROUTES
# -------------------------------------------------
@app.route("/days", methods=["GET"])
def get_days():
    import re
    url = f"https://www.bigscreen.com/Marquee.php?theater={THEATER_ID}&view=sched"
    resp = requests.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    dates = set()

    # currently selected day
    sel = soup.select_one("td.scheddaterow_sel")
    if sel:
        text = sel.get_text(strip=True)
        # Try to find the showdate in a sibling <a> if exists, otherwise use today
        a = sel.select_one("a")
        if a and "showdate" in a["href"]:
            m = re.search(r"showdate=(\d{4}-\d{2}-\d{2})", a["href"])
            if m:
                dates.add(m.group(1))
        else:
            dates.add(datetime.now().strftime("%Y-%m-%d"))

    # all other selectable days
    for td in soup.select("td.scheddaterow"):
        a = td.select_one("a")
        if a and "showdate" in a["href"]:
            m = re.search(r"showdate=(\d{4}-\d{2}-\d{2})", a["href"])
            if m:
                dates.add(m.group(1))

    # filter out past days
    today = datetime.now().date()
    showdates = sorted([d for d in dates if datetime.strptime(d, "%Y-%m-%d").date() >= today])

    return jsonify(showdates)








@app.route("/movies", methods=["GET"])
def get_movies():
    showdate = request.args.get("showdate")  # get date from URL
    movies = fetch_showtimes_by_scraping(showdate)
    return jsonify([m["name"] for m in movies])







@app.route("/schedule", methods=["POST"])
def get_schedule():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Must provide JSON body"}), 400

    selected_titles = data.get("movies", [])
    min_gap = data.get("min_gap", -5)
    showdate = data.get("showdate")
    show_more = data.get("show_more", False)
    MoviesSelected = data.get("MoviesSelected")

    # Optional timeframe
    start_time_str = data.get("start_time")  # e.g., "16:00"
    end_time_str = data.get("end_time")      # e.g., "20:00"

    if not showdate:
        return jsonify({"error": "Must provide showdate"}), 400

    # Fetch movies for the selected date
    movies = fetch_showtimes_by_scraping(showdate)
    if not movies:
        return jsonify({"error": f"No movies found for {showdate}"}), 404

    # Filter movies by selection
    selected_movies = [m for m in movies if m["name"] in selected_titles]
    if not selected_movies:
        return jsonify({"error": "None of the selected movies are available on this date"}), 404

    # ✅ Apply time window filter
    def filter_timeframe(m):
        filtered_showtimes = []
        for st in m["showtimes"]:
            start_dt = parse_bigscreen_time(st, base_date=datetime.strptime(showdate, "%Y-%m-%d"))
            end_dt = start_dt + timedelta(minutes=m["runtime"])

            # Filter by start_time
            if start_time_str:
                start_limit = datetime.strptime(f"{showdate} {start_time_str}", "%Y-%m-%d %H:%M")
                if start_dt < start_limit:
                    continue

            # Filter by end_time
            if end_time_str:
                end_limit = datetime.strptime(f"{showdate} {end_time_str}", "%Y-%m-%d %H:%M")
                if end_dt > end_limit:
                    continue

            filtered_showtimes.append(st)

        m["showtimes"] = filtered_showtimes
        return bool(filtered_showtimes)  # keep movie only if at least one showtime remains

    selected_movies = list(filter(filter_timeframe, selected_movies))

    if not selected_movies:
        return jsonify({"error": "No movies match the selected time window"}), 400

    # Generate schedules
    all_schedules = schedule_movies(selected_movies, min_gap)
    if not all_schedules:
        return jsonify({"error": "No valid schedules found"}), 400

    best_schedule = all_schedules[0]

    # Format output
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
    app.run(host="0.0.0.0", port=5003)
