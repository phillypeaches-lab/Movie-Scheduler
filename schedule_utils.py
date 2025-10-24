from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from itertools import permutations, product

app = Flask(__name__)

# -------------------------------------------------
# CONFIGURATION
# -------------------------------------------------
THEATER_ID = "4343"

# -------------------------------------------------
# UTILITIES
# -------------------------------------------------
def fetch_available_days():
    url = "https://www.bigscreen.com/Marquee.php?theater=4343&view=sched"
    resp = requests.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    days = []
    for td in soup.select("td.scheddaterow"):
        a = td.find("a")
        if a:
            day_text = a.get_text(separator=" ", strip=True)  # e.g., "Sat 10/18"
            day_href = a['href']  # e.g., "...&showdate=2025-10-18..."
            # Extract showdate from href
            import re
            match = re.search(r"showdate=(\d{4}-\d{2}-\d{2})", day_href)
            if match:
                showdate = match.group(1)
                days.append({"label": day_text, "date": showdate})
    return days


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


def parse_bigscreen_time(t_str, selected_date=None):
    """
    Converts BigScreen showtime string like '10:30a' or '7:20' into a datetime object.
    If selected_date is provided (YYYY-MM-DD), showtime is anchored on that day.
    """
    import datetime

    t_str = t_str.strip().lower()
    is_am = t_str.endswith("a")
    if is_am:
        t_str = t_str[:-1]  # remove trailing 'a'

    hour, minute = map(int, t_str.split(":"))
    if not is_am and hour != 12:  # PM times except 12 PM
        hour += 12

    # Determine base date
    if selected_date:
        base_date = datetime.datetime.strptime(selected_date, "%Y-%m-%d")
    else:
        base_date = datetime.datetime.today()

    return base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)



def fetch_showtimes_by_scraping():
    """Scrape current showtimes from BigScreen.com."""
    import requests, re
    from bs4 import BeautifulSoup

    base_url = "https://www.bigscreen.com/Marquee.php"
    params = {
        "theater": "4343",
        "view": "sched",
        "sort": "date"
    }
    if selected_date:
        params["showdate"] = selected_date

    resp = requests.get(base_url, params=params)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    
    movies = []
    for row in soup.select("tr.graybar_0, tr.graybar_1"):
        title_elem = row.select_one("td.col_movie a.movieNameList")
        if not title_elem:
            continue
        name = title_elem.get_text(strip=True)

        runtime_elem = row.select_one("td.col_movie span.small")
        runtime = 120
        if runtime_elem:
            match = re.search(r"(\d+):(\d+)", runtime_elem.get_text(strip=True))
            if match:
                hours, minutes = int(match.group(1)), int(match.group(2))
                runtime = hours * 60 + minutes

        showtime_elems = row.select("td.col_showtimes a[target='ExternalSite']")
        showtimes = [a.get_text(strip=True).lower()
                     for a in showtime_elems
                     if re.match(r"^\d{1,2}:\d{2}a?$", a.get_text(strip=True).lower())]

        if showtimes:
            movies.append({
                "name": name,
                "runtime": runtime,
                "showtimes": showtimes
            })

    return movies


# -------------------------------------------------
# SCHEDULER
# -------------------------------------------------
from itertools import product, permutations
from datetime import timedelta

def schedule_movies(selected_movies, min_gap=-5,selected_date=None):
    """
    Generate all valid schedules and return a list of dicts:
    [{'schedule': [...], 'total_gap': ...}, ...]
    """
    from itertools import product, permutations
    from datetime import timedelta

    movie_showtimes = []
    for m in selected_movies:
        showtimes = []
        for st in m["showtimes"]:
            try:
                start = parse_bigscreen_time(st,selected_date)
            except:
                continue
            end = start + timedelta(minutes=m["runtime"])
            showtimes.append({"movie": m["name"], "start": start, "end": end})
        if showtimes:
            movie_showtimes.append(showtimes)

    all_schedules = []

    for order in permutations(range(len(movie_showtimes))):
        ordered_sets = [movie_showtimes[i] for i in order]
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
                all_schedules.append({
                    "schedule": combo,
                    "total_gap": total_gap,
                    "movies_count": len(combo)
                })

    # Sort schedules: most movies first, then least total gap
    # Sort schedules: most movies first, then least total gap
    all_schedules.sort(key=lambda s: ( -s["movies_count"], s["total_gap"].total_seconds() ))


    return all_schedules  # ✅ now returns a list