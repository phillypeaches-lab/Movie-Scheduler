#Schedules based on time of day, nothing before current time.
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from itertools import permutations, product, combinations

app = Flask(__name__)

# -------------------------------------------------
# CONFIGURATION
# -------------------------------------------------
THEATER_ID = "4343"

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


def fetch_showtimes_by_scraping():
    """Scrape current showtimes from BigScreen.com, ignoring past showtimes."""
    import re

    url = f"https://www.bigscreen.com/Marquee.php?theater={THEATER_ID}&view=sched"
    resp = requests.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    now = datetime.now()
    today_base = now.replace(hour=0, minute=0, second=0, microsecond=0)

    movies = []
    for row in soup.select("tr.graybar_0, tr.graybar_1"):
        title_elem = row.select_one("td.col_movie a.movieNameList")
        if not title_elem:
            continue
        name = title_elem.get_text(strip=True)

        # Runtime extraction
        runtime_elem = row.select_one("td.col_movie span.small")
        runtime = 120
        if runtime_elem:
            match = re.search(r"(\d+):(\d+)", runtime_elem.get_text(strip=True))
            if match:
                hours, minutes = int(match.group(1)), int(match.group(2))
                runtime = hours * 60 + minutes

        # Extract and filter showtimes
        showtime_elems = row.select("td.col_showtimes a[target='ExternalSite']")
        all_showtimes = [a.get_text(strip=True).lower()
                         for a in showtime_elems
                         if re.match(r"^\d{1,2}:\d{2}a?$", a.get_text(strip=True).lower())]

        # ✅ Filter out past showtimes
        valid_showtimes = []
        for st in all_showtimes:
            try:
                show_dt = parse_bigscreen_time(st, base_date=today_base)
                if show_dt >= now:
                    valid_showtimes.append(st)
            except Exception:
                continue

        if valid_showtimes:
            movies.append({
                "name": name,
                "runtime": runtime,
                "showtimes": valid_showtimes
            })

    return movies

# -----------------------------
# Scheduler (list version)
# -----------------------------
def schedule_movies(selected_movies, min_gap=-5):
    """
    Generate all valid schedules for any subset of selected movies.
    Returns list of valid schedules sorted by (#movies desc, total gap asc).
    """
    if not selected_movies:
        return []

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

    # ✅ Try all subset sizes (N down to 1)
    for subset_size in range(len(movie_showtimes), 0, -1):
        for subset_indices in combinations(range(len(movie_showtimes)), subset_size):
            subset = [movie_showtimes[i] for i in subset_indices]

            for order in permutations(range(len(subset))):
                ordered_sets = [subset[i] for i in order]
                for combo in product(*ordered_sets):
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

        # ✅ If we found any valid schedules for this subset size, stop early.
        if all_valid_schedules:
            break

    # Sort: most movies first, then smallest total gap
    all_valid_schedules.sort(key=lambda x: (-x["movies_count"], x["total_gap"]))

    return all_valid_schedules


# -----------------------------
# Flask routes
# -----------------------------
@app.route("/movies", methods=["GET"])
def get_movies():
    movies = fetch_showtimes_by_scraping()
    if movies is None:
        return jsonify({"error": "Could not retrieve showtimes"}), 500
    return jsonify([m["name"] for m in movies])


@app.route("/schedule", methods=["POST"])
def get_schedule():
    data = request.get_json()
    selected_titles = data.get("movies", [])
    min_gap = data.get("min_gap", -5)
    show_more = data.get("show_more", False)  # <-- shortcut can send this

    movies = fetch_showtimes_by_scraping()
    selected_movies = [m for m in movies if m["name"] in selected_titles]

    all_schedules = schedule_movies(selected_movies, min_gap)

    if not all_schedules:
        return jsonify({"error": "No valid schedules found"}), 400

    # Sort schedules: most movies first, then least total gap
    all_schedules.sort(key=lambda s: (-s["movies_count"], s["total_gap"]))

    # pick the best
    best_schedule = all_schedules[0]

    def format_schedule(s, total_selected):
        lines = []
        lines.append(f"🎬 Schedule includes {s['movies_count']} of {total_selected} selected movies\n")
        for item in s["schedule"]:
            start_str = format_showtime(item["start"])
            end_str = format_showtime(item["end"])
            lines.append(f"{item['movie']}: {start_str} - {end_str}")
        lines.append(f"\nTotal gap time: {s['total_gap']}")
        return "\n".join(lines)


    total_selected = len(selected_movies)

    if show_more:
        return jsonify([format_schedule(s, total_selected) for s in all_schedules])
    else:
        return (
            format_schedule(best_schedule, total_selected),
            200,
            {"Content-Type": "text/plain; charset=utf-8"})





# -----------------------------
# Run locally
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
