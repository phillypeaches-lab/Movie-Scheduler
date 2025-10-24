#2+day selector+output formatting
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from itertools import permutations, product
import re

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
    """Convert BigScreen time (like '10:30a' or '7:20') into datetime using a fixed base date."""
    if base_date is None:
        base_date = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)

    t_str = t_str.strip().lower()
    is_am = t_str.endswith("a")
    if is_am:
        t_str = t_str[:-1]

    hour, minute = map(int, t_str.split(":"))
    if not is_am and hour != 12:
        hour += 12

    return base_date.replace(hour=hour, minute=minute)


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

    today = datetime.today().date()
    target_date = today if showdate is None else datetime.strptime(showdate, "%Y-%m-%d").date()
    now = datetime.now()

    merged = {}

    for row in soup.select("tr.graybar_0, tr.graybar_1"):
        title_elem = row.select_one("td.col_movie a.movieNameList")
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

        showtime_elems = row.select("td.col_showtimes a[target='ExternalSite']")
        showtimes = []
        for a in showtime_elems:
            st = a.get_text(strip=True).lower()
            if not re.match(r"^\d{1,2}:\d{2}a?$", st):
                continue

            dt = parse_bigscreen_time(st)
            # Only skip past times if showdate is today
            if target_date == today and dt < now:
                continue

            showtimes.append(st)

        if not showtimes:
            continue

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
    # Get JSON from request
    data = request.get_json()
    if not data:
        return jsonify({"error": "Must provide JSON body"}), 400

    selected_titles = data.get("movies", [])
    min_gap = data.get("min_gap", -5)
    showdate = data.get("showdate")
    show_more = data.get("show_more", False)
    MoviesSelected = data.get("MoviesSelected")

    # Require showdate
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

    # Generate all schedules
    all_schedules = schedule_movies(selected_movies, min_gap)
    if not all_schedules:
        return jsonify({"error": "No valid schedules found"}), 400

    # Pick the best schedule
    best_schedule = all_schedules[0]

    # Format output
    def format_schedule(s):
        lines = [f"Date: {showdate}", f"{len(s['schedule'])} out of {MoviesSelected} movies selected"  # ✅ use the variable from Shortcuts
    ]
        for item in s["schedule"]:
            start_str = format_showtime(item["start"])
            end_str = format_showtime(item["end"])
            lines.append(f"{item['movie']}: {start_str} - {end_str}")
        lines.append(f"\nTotal gap time: {s['total_gap']}")
        return "\n".join(lines)

    if show_more:
        # Return all schedules
        return jsonify([format_schedule(s) for s in all_schedules])
    else:
        # Return only the best schedule
        return format_schedule(best_schedule), 200, {"Content-Type": "text/plain; charset=utf-8"}


# -------------------------------------------------
# RUN LOCALLY
# -------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002)
