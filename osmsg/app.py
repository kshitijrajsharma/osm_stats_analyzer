import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
from datetime import datetime

import dataframe_image as dfi
import geopandas as gpd
import humanize
import osmium
import pandas as pd
from shapely.geometry import box
from tqdm import tqdm

from osmsg.utils import (
    create_charts,
    create_profile_link,
    download_osm_files,
    get_file_path_from_url,
    sum_tags,
    verify_me_osm,
)

from .changefiles import (
    get_download_urls_changefiles,
    get_prev_hour,
    get_prev_year_dates,
    in_local_timezone,
    last_days_count,
    previous_day,
    previous_month,
    previous_week,
    seq_to_timestamp,
    strip_utc,
)
from .changesets import ChangesetToolKit

users_temp = {}
users = {}
summary_interval = {}
summary_interval_temp = {}
hashtag_changesets = {}
countries_changesets = {}
whitelisted_users = []

print("Initializing ....")
# read the GeoJSON file
countries_df = gpd.read_file(
    "https://raw.githubusercontent.com/kshitijrajsharma/OSMSG/master/data/countries_un.geojson"
)
geofabrik_countries = pd.read_csv(
    "https://raw.githubusercontent.com/kshitijrajsharma/OSMSG/feature/country_name_url/data/countries.csv"
)


def collect_changefile_stats(
    user, uname, changeset, version, tags, osm_type, timestamp, osm_obj_nodes=None
):
    tags_to_collect = list(additional_tags) if additional_tags else None
    if version == 1:
        action = "create"
    if version > 1:
        action = "modify"
    if version == 0:
        action = "delete"
    timestamp = timestamp.strftime("%Y-%m-%d")
    len_feature = 0
    if length and osm_obj_nodes:
        try:
            len_feature = osmium.geom.haversine_distance(osm_obj_nodes)
        except Exception as ex:
            # print("WARNING: way  incomplete." % w.id)
            pass

    # set default
    users.setdefault(
        user,
        {
            "name": uname,
            "uid": user,
            "changesets": 0,
            "nodes": {"create": 0, "modify": 0, "delete": 0},
            "ways": {"create": 0, "modify": 0, "delete": 0},
            "relations": {"create": 0, "modify": 0, "delete": 0},
            "poi": {"create": 0, "modify": 0},  # nodes that has tags
        },
    )
    if summary:
        summary_interval.setdefault(
            timestamp,
            {
                "timestamp": timestamp,
                "users": 0,
                "changesets": 0,
                "nodes": {"create": 0, "modify": 0, "delete": 0},
                "ways": {"create": 0, "modify": 0, "delete": 0},
                "relations": {"create": 0, "modify": 0, "delete": 0},
                "poi": {"create": 0, "modify": 0},
            },
        )

    # changeset count
    users_temp.setdefault(user, {"changesetrs": []})
    if summary:
        summary_interval_temp.setdefault(timestamp, {"changesets": [], "users": []})
        if changeset not in summary_interval_temp[timestamp]["changesets"]:
            summary_interval_temp[timestamp]["changesets"].append(changeset)
        summary_interval[timestamp]["changesets"] = len(
            summary_interval_temp[timestamp]["changesets"]
        )
        if user not in summary_interval_temp[timestamp]["users"]:
            summary_interval_temp[timestamp]["users"].append(user)
        summary_interval[timestamp]["users"] = len(
            summary_interval_temp[timestamp]["users"]
        )
    users_temp[user].setdefault("changesets", [])
    if changeset not in users_temp[user]["changesets"]:
        users_temp[user]["changesets"].append(changeset)
    users[user]["changesets"] = len(users_temp[user]["changesets"])

    # hashtags & countries block
    if hashtags or changeset_meta:
        users[user].setdefault("countries", [])
        users[user].setdefault("hashtags", [])

        try:
            for ch in hashtag_changesets[changeset]["countries"]:
                if ch not in users[user]["countries"]:
                    users[user]["countries"].append(ch)
            for ch in hashtag_changesets[changeset]["hashtags"]:
                if ch not in users[user]["hashtags"]:
                    users[user]["hashtags"].append(ch)
        except Exception as ex:
            pass

    # osm element count
    users[user][osm_type][action] += 1
    if summary:
        summary_interval[timestamp][osm_type][action] += 1

    # POI block
    if osm_type == "nodes" and tags and action != "delete":
        users[user]["poi"][action] += 1
        if summary:
            summary_interval[timestamp]["poi"][action] += 1

    # all tags block
    if all_tags:
        users[user].setdefault("tags_create", {})
        users[user].setdefault("tags_modify", {})
        if summary:
            summary_interval[timestamp].setdefault("tags_create", {})
            summary_interval[timestamp].setdefault("tags_modify", {})
        if tags:
            for key, value in tags:
                if action != "delete":  # we don't need deleted tags
                    users[user][f"tags_{action}"].setdefault(key, 0)
                    users[user][f"tags_{action}"][key] += 1
                    if summary:
                        summary_interval[timestamp][f"tags_{action}"].setdefault(key, 0)
                        summary_interval[timestamp][f"tags_{action}"][key] += 1

    # for user supplied tags
    if tags_to_collect and action != "delete" and tags:
        for tag in tags_to_collect:
            if summary:
                summary_interval[timestamp].setdefault(tag, {"create": 0, "modify": 0})
            users[user].setdefault(tag, {"create": 0, "modify": 0})
            if tag in tags:
                if summary:
                    summary_interval[timestamp][tag][action] += 1
                users[user][tag][action] += 1

    # for length calculation
    if length:
        for t in length:
            users[user].setdefault(f"{t}_create_len", 0)
            if summary:
                summary_interval[timestamp].setdefault(f"{t}_create_len", 0)
            if t in tags and action != "modify" and action != "delete":
                if summary:
                    summary_interval[timestamp][f"{t}_create_len"] += round(len_feature)
                users[user][f"{t}_create_len"] += round(len_feature)


def calculate_stats(
    user, uname, changeset, version, tags, osm_type, timestamp, osm_obj_nodes=None
):
    if hashtags:  # intersect with changesets
        if (
            len(hashtag_changesets) > 0 or len(whitelisted_users) > 0
        ):  # make sure there are changesets to intersect if not meaning hashtag changeset not found no need to go for changefiles

            if changeset in hashtag_changesets.keys() or uname in whitelisted_users:
                collect_changefile_stats(
                    user,
                    uname,
                    changeset,
                    version,
                    tags,
                    osm_type,
                    timestamp,
                    osm_obj_nodes,
                )
    elif len(whitelisted_users) > 0:
        if uname in whitelisted_users:
            collect_changefile_stats(
                user,
                uname,
                changeset,
                version,
                tags,
                osm_type,
                timestamp,
                osm_obj_nodes,
            )
    else:  # collect everything
        collect_changefile_stats(
            user, uname, changeset, version, tags, osm_type, timestamp, osm_obj_nodes
        )


class ChangesetHandler(osmium.SimpleHandler):
    def __init__(self):
        super(ChangesetHandler, self).__init__()

    def changeset(self, c):
        run_hashtag_check_logic = False
        if changeset_meta and not hashtags:
            if "comment" in c.tags:
                run_hashtag_check_logic = True
        if hashtags:
            if "comment" in c.tags:
                if exact_lookup:
                    hashtags_comment = re.findall(r"#[\w-]+", c.tags["comment"])
                    if any(
                        elem.lower() in map(str.lower, hashtags_comment)
                        for elem in hashtags
                    ):
                        run_hashtag_check_logic = True
                elif any(
                    elem.lower() in c.tags["comment"].lower() for elem in hashtags
                ):
                    run_hashtag_check_logic = True

        if run_hashtag_check_logic:
            if c.id not in hashtag_changesets.keys():
                hashtag_changesets[c.id] = {"hashtags": [], "countries": []}
                # get bbox
                bounds = str(c.bounds)
                if "invalid" not in bounds:
                    bbox_list = bounds.strip("()").split(" ")
                    minx, miny = bbox_list[0].split("/")
                    maxx, maxy = bbox_list[1].split("/")
                    bbox = box(float(minx), float(miny), float(maxx), float(maxy))
                    # Create a point for the centroid of the bounding box
                    centroid = bbox.centroid
                    intersected_rows = countries_df[countries_df.intersects(centroid)]
                    hashtags_comment = re.findall(r"#[\w-]+", c.tags["comment"])
                    for hash_tag in hashtags_comment:
                        if hash_tag not in hashtag_changesets[c.id]["hashtags"]:
                            hashtag_changesets[c.id]["hashtags"].append(hash_tag)
                    for i, row in intersected_rows.iterrows():
                        if row["name"] not in hashtag_changesets[c.id]["countries"]:
                            hashtag_changesets[c.id]["countries"].append(row["name"])


class ChangefileHandler(osmium.SimpleHandler):
    def __init__(self):
        super(ChangefileHandler, self).__init__()

    def node(self, n):
        if n.timestamp >= start_date_utc and n.timestamp < end_date_utc:
            version = n.version
            if n.deleted:
                version = 0
            try:
                calculate_stats(
                    n.uid, n.user, n.changeset, version, n.tags, "nodes", n.timestamp
                )
            except Exception as ex:
                print(f"Warning: {n.id} parse error")
                print(ex)

    def way(self, w):
        if w.timestamp >= start_date_utc and w.timestamp < end_date_utc:
            version = w.version
            if w.deleted:
                version = 0
            try:
                calculate_stats(
                    w.uid,
                    w.user,
                    w.changeset,
                    version,
                    w.tags,
                    "ways",
                    w.timestamp,
                    w.nodes if length else None,
                )
            except Exception as ex:
                print(f"Warning: {w.id} parse error")
                print(ex)

    def relation(self, r):
        if r.timestamp >= start_date_utc and r.timestamp < end_date_utc:
            version = r.version
            if r.deleted:
                version = 0
            try:
                calculate_stats(
                    r.uid,
                    r.user,
                    r.changeset,
                    version,
                    r.tags,
                    "relations",
                    r.timestamp,
                )
            except Exception as ex:
                print(f"Warning: {r.id} parse error")
                print(ex)


def process_changefiles(url):
    # Check that the request was successful
    # Send a GET request to the URL
    if "minute" not in url:
        print(f"Processing {url}")
    file_path = get_file_path_from_url(url, "changefiles")
    # Open the .osc.gz file in read-only mode
    handler = ChangefileHandler()
    if length:
        handler.apply_file(file_path[:-3], locations=True)
    else:
        handler.apply_file(file_path[:-3])


def process_changesets(url):
    # print(f"Processing {url}")
    file_path = get_file_path_from_url(url, "changeset")
    handler = ChangesetHandler()
    handler.apply_file(file_path[:-3])
    # print(f"Finished {url}")


def auth(username, password):
    print("Authenticating...")
    try:
        cookies = verify_me_osm(username, password)
    except Exception as ex:
        raise ValueError("OSM Authentication Failed")

    print("Authenticated !")
    return cookies


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--start_date",
        help="Start date in the format YYYY-MM-DD HH:M:Sz eg: 2023-01-28 17:43:09+05:45",
    )
    parser.add_argument(
        "--end_date",
        help="End date in the format YYYY-MM-DD HH:M:Sz eg: 2023-01-28 17:43:09+05:45",
        default=dt.datetime.now(),
    )
    parser.add_argument(
        "--username",
        default=None,
        help="Your OSM Username : Only required for Geofabrik Internal Changefiles",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Your OSM Password : Only required for Geofabrik Internal Changefiles",
    )
    parser.add_argument(
        "--timezone",
        default="UTC",
        choices=["Nepal", "UTC"],
        help="Your Timezone : Currently Supported Nepal, Default : UTC",
    )

    parser.add_argument(
        "--name",
        default="stats",
        help="Output stat file name",
    )
    parser.add_argument(
        "--country",
        nargs="+",
        default=None,
        help="List of country name to extract (get id from data/countries), It will use geofabrik countries updates so it will require OSM USERNAME. Only Available for Daily Updates",
    )

    parser.add_argument(
        "--tags",
        nargs="+",
        default=None,
        type=str,
        help="Additional stats to collect : List of tags key",
    )

    parser.add_argument(
        "--hashtags",
        nargs="+",
        default=None,
        type=str,
        help="Hashtags Statistics to Collect : List of hashtags , Limited until daily stats for now , Only lookups if hashtag is contained on the string , not a exact string lookup on beta",
    )
    parser.add_argument(
        "--length",
        nargs="+",
        default=None,
        type=str,
        help="Calculate length of osm features , Only Supported for way created features , Pass list of tags key to calculate eg : --length highway waterway , Unit is in Meters",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Force for the Hashtag Replication fetch if it is greater than a day interval",
        default=False,
    )

    parser.add_argument(
        "--rows",
        type=int,
        default=None,
        help="No. of top rows to extract , to extract top 100 , pass 100",
    )

    parser.add_argument(
        "--users",
        type=str,
        nargs="+",
        default=None,
        help="List of user names to look for , You can use it to only produce stats for listed users or pass it with hashtags , it will act as or filter. Case sensitive use ' ' to enter names with space in between",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="No. of Parallel workers to assign : Default is no of cpu available , Be aware to use this max no of workers may cause overuse of resources",
    )

    parser.add_argument(
        "--url",
        nargs="+",
        default=["https://planet.openstreetmap.org/replication/minute"],
        help="Your public list of OSM Change Replication URL , 'minute,hour,day' option by default will translate to planet replciation url. You can supply multiple urls for geofabrik country updates , Url should not have trailing / at the end",
    )

    parser.add_argument(
        "--last_week",
        action="store_true",
        help="Extract stats for last week",
        default=False,
    )
    parser.add_argument(
        "--last_day",
        action="store_true",
        help="Extract Stats for last day",
        default=False,
    )
    parser.add_argument(
        "--last_month",
        action="store_true",
        help="Extract Stats for last Month",
        default=False,
    )
    parser.add_argument(
        "--last_year",
        action="store_true",
        help="Extract stats for last year",
        default=False,
    )
    parser.add_argument(
        "--last_hour",
        action="store_true",
        help="Extract stats for Last hour",
        default=False,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="N nof of last days to extract , for eg if 3 is supplied script will generate stats for last 3 days",
    )
    parser.add_argument(
        "--charts",
        action="store_true",
        help="Exports Summary Charts along with stats",
        default=False,
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Produces Summary.md file with summary of Run and also a summary.csv which will have summary of stats per day",
        default=False,
    )
    parser.add_argument(
        "--exact_lookup",
        action="store_true",
        help="Exact lookup for hashtags to match exact hashtag supllied , without this hashtag search will search for the existence of text on hashtags and comments",
        default=False,
    )

    parser.add_argument(
        "--changeset",
        help="Include hashtag and country informations on the stats. It forces script to process changeset replciation , Careful to use this since changeset replication is minutely according to your internet speed and cpu cores",
        action="store_true",
        default=False,
    )

    parser.add_argument(
        "--all_tags",
        action="store_true",
        help="Extract statistics of all of the unique tags and its count",
        default=False,
    )
    parser.add_argument(
        "--temp",
        action="store_true",
        help="Deletes downloaded osm files from machine after processing is done , if you want to run osmsg on same files again keep this option turn off",
        default=False,
    )

    parser.add_argument(
        "--exclude_date_in_name",
        action="store_true",
        help="By default from and to date will be added to filename , You can skip this behaviour with this option",
        default=False,
    )

    parser.add_argument(
        "--format",
        nargs="+",
        choices=["csv", "json", "excel", "image", "text"],
        default="csv",
        help="Stats output format",
    )
    parser.add_argument(
        "--read_from_metadata",
        help="Location of metadata to pick start date from previous run's end_date , Generally used if you want to run bot on regular interval using cron/service",
    )

    args = parser.parse_args()
    return args


def main():
    args = parse_args()
    if args.start_date:
        start_date = strip_utc(
            dt.datetime.strptime(args.start_date, "%Y-%m-%d %H:%M:%S%z"), args.timezone
        )

    if not args.start_date:
        if (
            args.last_week
            or args.last_day
            or args.last_month
            or args.last_year
            or args.last_hour
            or args.days
        ):
            pass
        else:
            print(
                "ERR: Supply start_date or extraction parameters such as last_day , last_hour"
            )
            sys.exit()

    if args.end_date:
        end_date = args.end_date
        if not isinstance(end_date, datetime):
            end_date = dt.datetime.strptime(args.end_date, "%Y-%m-%d %H:%M:%S%z")

        end_date = strip_utc(end_date, args.timezone)
    if args.country:
        osc_url_temp = []
        for ctr in args.country:
            if not geofabrik_countries["id"].isin([ctr.lower()]).any():
                print(
                    f"Error : {ctr} doesn't exists : Refer to data/countries.csv id column"
                )
                sys.exit()
            osc_url_temp.append(
                geofabrik_countries.loc[
                    geofabrik_countries["id"] == ctr.lower(), "update_url"
                ].values[0]
            )
        print(f"Ignoring --url , and using Geofabrik Update URL for {args.country}")
        args.url = osc_url_temp
    if args.changeset:
        if args.hashtags:
            assert (
                args.changeset
            ), "You can not use include changeset meta option along with hashtags"

    start_time = time.time()

    global additional_tags
    global cookies
    global all_tags
    global hashtags
    global length
    global changeset_meta
    global exact_lookup
    global summary

    all_tags = args.all_tags
    additional_tags = args.tags
    hashtags = args.hashtags
    cookies = None
    changeset_meta = args.changeset
    exact_lookup = args.exact_lookup
    length = args.length
    summary = args.summary

    if args.url:
        args.url = list(set(args.url))  # remove duplicates
        for url in args.url:
            if urllib.parse.urlparse(url).scheme == "":
                # The URL is not valid
                if url == "minute":
                    args.url = ["https://planet.openstreetmap.org/replication/minute"]
                elif url == "hour":
                    args.url = ["https://planet.openstreetmap.org/replication/hour"]
                elif url == "day":
                    args.url = ["https://planet.openstreetmap.org/replication/day"]
                else:
                    print(f"Invalid input for urls {url}")
                    sys.exit()
            if url.endswith("/"):
                print(f"{url} should not end with trailing /")
                sys.exit()

        if any("geofabrik" in url.lower() for url in args.url):
            if args.username is None:
                print(os.environ.get("OSM_USERNAME"))
                args.username = os.environ.get("OSM_USERNAME")
            if args.password is None:
                args.password = os.environ.get("OSM_PASSWORD")

            if not (args.username and args.password):
                assert (
                    args.username and args.password
                ), "OSM username and password are required for geofabrik url"
            cookies = auth(args.username, args.password)

    count = sum(
        [
            args.last_hour,
            args.last_year,
            args.last_month,
            args.last_day,
            args.last_week,
            bool(args.days),
        ]
    )
    if count > 1:
        print(
            "Error: only one of --last_hour, --last_year, --last_month, --last_day, --last_week, or --days should be specified."
        )
        sys.exit()

    if args.users:
        for u in args.users:
            whitelisted_users.append(u)

    if args.last_hour:
        start_date, end_date = get_prev_hour(args.timezone)

    if args.last_year:
        start_date, end_date = get_prev_year_dates(args.timezone)

    if args.last_month:
        start_date, end_date = previous_month(args.timezone)

    if args.last_day:
        start_date, end_date = previous_day(args.timezone)
    if args.last_week:
        start_date, end_date = previous_week(args.timezone)

    if args.days:
        if args.days > 0:
            start_date, end_date = last_days_count(args.timezone, args.days)
        else:
            print(f"Error : {args.days} should be greater than 0")
            sys.exit()
    if args.read_from_metadata:
        if os.path.exists(args.read_from_metadata):
            with open(args.read_from_metadata, "r") as openfile:
                # Reading from json file
                meta_json = json.load(openfile)
            if "end_date" in meta_json:
                start_date = datetime.strptime(
                    meta_json["end_date"], "%Y-%m-%d %H:%M:%S%z"
                )

                print(f"Start date changed to {start_date} after reading from metajson")
            else:
                print("no end_date in meta json")
        else:
            print("couldn't read start_date from metajson")
    if start_date == end_date:
        print("Err: Start date and end date are equal")
        sys.exit()
    if (end_date - start_date).days < 1 or args.last_hour and args.country:
        print(
            "Warning : Use --changeset Option to include country info on stats , You can filter your country from output itself for stats lesser than a day, Use --country option for more than Day Statistics"
        )
        sys.exit()
    if (end_date - start_date).days > 1:

        if args.hashtags:
            print(
                "Warning : Replication for Changeset is minutely , To download more than day data it might take a while depending upon your internet speed, Use --force to ignore this warning"
            )
            if not args.force:
                sys.exit()
    print(f"Supplied start_date: {start_date} and end_date: {end_date}")

    if args.hashtags or args.changeset:

        Changeset = ChangesetToolKit()
        (
            changeset_download_urls,
            changeset_start_seq,
            changeset_end_seq,
        ) = Changeset.get_download_urls(start_date, end_date)
        print(
            f"Processing Changeset from {strip_utc(Changeset.sequence_to_timestamp(changeset_start_seq),args.timezone)} to {strip_utc(Changeset.sequence_to_timestamp(changeset_end_seq),args.timezone)}"
        )

        temp_path = os.path.join(os.getcwd(), "temp/changeset", "changesets")
        if not os.path.exists(temp_path):
            os.makedirs(temp_path)

        max_workers = os.cpu_count() if not args.workers else args.workers
        print(f"Using {max_workers} Threads")
        print(
            "Downloading Changeset files using https://planet.openstreetmap.org/replication/changesets/"
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Use `map` to apply the `download osm files` function to each element in the `urls` list
            for _ in tqdm(
                executor.map(
                    lambda x: download_osm_files(x, mode="changeset", cookies=cookies),
                    changeset_download_urls,
                ),
                total=len(changeset_download_urls),
                unit_scale=True,
                unit="changesets",
                leave=True,
            ):
                pass

        print("Processing Changeset Files")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Use `map` to apply the `download_image` function to each element in the `urls` list
            for _ in tqdm(
                executor.map(process_changesets, changeset_download_urls),
                total=len(changeset_download_urls),
                unit_scale=True,
                unit="changesets",
                leave=True,
            ):
                pass
            # executor.shutdown(wait=True)

        print("Changeset Processing Finished")
        end_seq_timestamp = Changeset.sequence_to_timestamp(changeset_end_seq)
        if end_date > end_seq_timestamp:
            end_date = strip_utc(end_seq_timestamp, args.timezone)
    for url in args.url:
        print(f"Changefiles : Generating Download Urls Using {url}")
        (
            download_urls,
            server_ts,
            start_seq,
            end_seq,
            start_seq_url,
            end_seq_url,
        ) = get_download_urls_changefiles(start_date, end_date, url, args.timezone)
        if server_ts < end_date:
            print(
                f"Warning : End date data is not available at server, Changing to latest available date {server_ts}"
            )
            end_date = server_ts
            if start_date >= server_ts:
                print("Err: Data is not available after start date ")
                sys.exit()
        global end_date_utc
        global start_date_utc

        start_date_utc = start_date.astimezone(dt.timezone.utc)
        end_date_utc = end_date.astimezone(dt.timezone.utc)
        print(
            f"Final UTC Date time to filter stats : {start_date_utc} to {end_date_utc}"
        )
        # Use the ThreadPoolExecutor to download the images in parallel
        max_workers = os.cpu_count() if not args.workers else args.workers
        print(f"Using {max_workers} Threads")

        print("Downloading Changefiles")
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Use `map` to apply the `download osm files` function to each element in the `urls` list
            for _ in tqdm(
                executor.map(
                    lambda x: download_osm_files(
                        x, mode="changefiles", cookies=cookies
                    ),
                    download_urls,
                ),
                total=len(download_urls),
                unit_scale=True,
                unit="changefiles",
                leave=True,
            ):
                pass
        print("Processing Changefiles")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Use `map` to apply the `download_image` function to each element in the `urls` list
            # executor.map(process_changefiles, download_urls)
            for _ in tqdm(
                executor.map(process_changefiles, download_urls),
                total=len(download_urls),
                unit_scale=True,
                unit="changefiles",
                leave=True,
            ):
                pass

            # executor.shutdown(wait=True)
        print(f"Changefiles Processing Finished using {url}")
    os.chdir(os.getcwd())
    if args.temp:
        shutil.rmtree("temp")
    if len(users) >= 1:
        # print(users)
        if args.all_tags:
            for user in users:
                for action in ["create", "modify"]:
                    users[user][f"tags_{action}"] = json.dumps(
                        dict(
                            sorted(
                                users[user][f"tags_{action}"].items(),
                                key=lambda item: item[1],
                                reverse=True,
                            )
                        )
                    )
            if summary:
                for timestamp in summary_interval:
                    for action in ["create", "modify"]:
                        summary_interval[timestamp][f"tags_{action}"] = json.dumps(
                            dict(
                                sorted(
                                    summary_interval[timestamp][
                                        f"tags_{action}"
                                    ].items(),
                                    key=lambda item: item[1],
                                    reverse=True,
                                )
                            )
                        )
        if args.summary:
            summary_df = pd.json_normalize(list(summary_interval.values()))
            summary_df = summary_df.assign(
                changes=summary_df["nodes.create"]
                + summary_df["nodes.modify"]
                + summary_df["nodes.delete"]
                + summary_df["ways.create"]
                + summary_df["ways.modify"]
                + summary_df["ways.delete"]
                + summary_df["relations.create"]
                + summary_df["relations.modify"]
                + summary_df["relations.delete"]
            )
            summary_df.insert(3, "map_changes", summary_df["changes"], True)
            summary_df = summary_df.drop(columns=["changes"])

            summary_df = summary_df.sort_values("timestamp", ascending=True)

        df = pd.json_normalize(list(users.values()))

        if hashtags or changeset_meta:
            df["countries"] = df["countries"].apply(lambda x: ",".join(map(str, x)))

        df = df.assign(
            changes=df["nodes.create"]
            + df["nodes.modify"]
            + df["nodes.delete"]
            + df["ways.create"]
            + df["ways.modify"]
            + df["ways.delete"]
            + df["relations.create"]
            + df["relations.modify"]
            + df["relations.delete"]
        )
        df.insert(3, "map_changes", df["changes"], True)
        df = df.drop(columns=["changes"])
        df = df.sort_values("map_changes", ascending=False)
        df.insert(0, "rank", range(1, len(df) + 1), True)
        if args.rows:
            df = df.head(args.rows)
        print(df)
        if args.all_tags:
            # Get the column names of the DataFrame
            cols = df.columns.tolist()
            # Identify the column names that you want to move
            cols_to_move = ["tags_create", "tags_modify"]
            # Remove the columns to move from the list of column names
            cols = [col for col in cols if col not in cols_to_move]
            # Add the columns to move to the end of the list of column names
            cols = cols + cols_to_move
            # Reindex the DataFrame with the new order of column names
            df = df.reindex(columns=cols)

        if hashtags or changeset_meta:
            df["hashtags"] = df["hashtags"].apply(lambda x: ",".join(map(str, x)))
            column_to_move = "hashtags"
            df = df.assign(**{column_to_move: df.pop(column_to_move)})

        start_date = in_local_timezone(start_date_utc, args.timezone)
        end_date = in_local_timezone(end_date_utc, args.timezone)

        fname = f"{args.name}_{start_date}_{end_date}"
        if args.exclude_date_in_name:
            fname = args.name
        if "image" in args.format:  ### image used for twitter tweet
            # Convert the DataFrame to an image
            df_img = df.head(25)
            # Compute sums of specified columns for the top 20 rows
            created = df_img[["nodes.create", "ways.create", "relations.create"]].sum(
                axis=1
            )
            modified = df_img[["nodes.modify", "ways.modify", "relations.modify"]].sum(
                axis=1
            )
            deleted = df_img[["nodes.delete", "ways.delete", "relations.delete"]].sum(
                axis=1
            )
            # Concatenate original DataFrame and sums DataFrame
            result_df = pd.concat(
                [
                    df_img,
                    created.rename("Created"),
                    modified.rename("Modified"),
                    deleted.rename("Deleted"),
                ],
                axis=1,
            )

            cols_to_export = [
                "rank",
                "name",
                "changesets",
                "map_changes",
                "Created",
                "Modified",
                "Deleted",
            ]  # Specify columns to export
            result_df = result_df.reset_index(drop=True)

            dfi.export(
                result_df[cols_to_export],
                "top_users.png",
                max_cols=-1,
                max_rows=-1,
            )

        if "json" in args.format:
            # with open(f"{out_file_name}.json") as file:
            #     file.write(json.dumps(users))
            df.to_json(f"{fname}.json", orient="records")
        if "csv" in args.format:
            # Add the start_date and end_date columns to the DataFrame
            csv_df = df
            csv_df["start_date"] = start_date_utc
            csv_df["end_date"] = end_date_utc

            # Create profile link column
            csv_df.insert(2, "profile", csv_df["name"].apply(create_profile_link))

            csv_df.to_csv(f"{fname}.csv", index=False)
        if "excel" in args.format:
            df.to_excel(f"{fname}.xlsx", index=False)

        if "text" in args.format:
            text_output = df.to_markdown(tablefmt="grid", index=False)
            with open(f"{fname}.txt", "w", encoding="utf-8") as file:
                file.write(
                    f"User Contributions From {start_date} to {end_date} . Planet Source File : {args.url}\n "
                )
                file.write(text_output)

        if args.charts:
            if any("geofabrik" in url.lower() for url in args.url):
                df.drop("countries", axis="columns")
            create_charts(df)

        if args.summary:
            summary_df.to_csv(f"summary.csv", index=False)
            created_sum = (
                df["nodes.create"] + df["ways.create"] + df["relations.create"]
            )
            modified_sum = (
                df["nodes.modify"] + df["ways.modify"] + df["relations.modify"]
            )
            deleted_sum = (
                df["nodes.delete"] + df["ways.delete"] + df["relations.delete"]
            )

            # Get the attribute of first row
            summary_text = f"{humanize.intword(len(df))} Users made {humanize.intword(df['changesets'].sum())} changesets with {humanize.intword(df['map_changes'].sum())} map changes."
            thread_summary = f"{humanize.intword(created_sum.sum())} OSM Elements were Created, {humanize.intword(modified_sum.sum())} Modified & {humanize.intword(deleted_sum.sum())} Deleted."

            with open(f"summary.md", "w", encoding="utf-8") as file:
                file.write(
                    f"### Last Update : Stats from {start_date_utc} to {end_date_utc} (UTC Timezone)\n\n"
                )
                file.write(f"#### {summary_text}\n")
                file.write(f"#### {thread_summary}\n")
                top_users = "\nTop 5 Users are : \n"
                # set rank column as index
                df.set_index("rank", inplace=True)
                for i in range(1, 6 if len(df) > 6 else len(df)):
                    top_users += f"- {df.loc[i, 'name']} : {humanize.intword(df.loc[i, 'map_changes'])} Map Changes\n"
                file.write(top_users)

                user_tags_summary = "\nSummary of Supplied Tags\n"
                user_tag = "poi"
                user_tags_summary += f"- {user_tag} = Created: {humanize.intword(df[f'{user_tag}.create'].sum())}, Modified : {humanize.intword(df[f'{user_tag}.modify'].sum())}\n"

                if args.tags:
                    for user_tag in args.tags:
                        user_tags_summary += f"- {user_tag} = Created: {humanize.intword(df[f'{user_tag}.create'].sum())}, Modified : {humanize.intword(df[f'{user_tag}.modify'].sum())}\n"
                file.write(f"{user_tags_summary}\n")

                if args.all_tags:
                    # Apply the sum_tags function to the tags column
                    tag_counts = sum_tags(df["tags_create"].tolist())

                    # Sort the resulting dictionary by values and take the top three entries
                    top_tags = sorted(
                        tag_counts.items(), key=lambda x: x[1], reverse=True
                    )[:5]
                    created_tags_summary = "\nTop 5 Created tags are :\n"
                    # Print the top tags and their counts
                    for tag, count in top_tags:
                        created_tags_summary += f"- {tag}: {humanize.intword(count)}\n"
                    # Apply the sum_tags function to the tags column
                    tag_counts = sum_tags(df["tags_modify"].tolist())

                    # Sort the resulting dictionary by values and take the top three entries
                    top_tags = sorted(
                        tag_counts.items(), key=lambda x: x[1], reverse=True
                    )[:5]
                    modified_tags_summary = "\nTop 5 Modified tags are :\n"
                    # Print the top tags and their counts
                    for tag, count in top_tags:
                        modified_tags_summary += f"- {tag}: {humanize.intword(count)}\n"
                    file.write(f"{created_tags_summary}\n")
                    file.write(f"{modified_tags_summary}\n")

                if "hashtags" in df.columns[df.astype(bool).any()]:
                    top_five = (
                        df["hashtags"]
                        .str.split(",")
                        .explode()
                        .dropna()
                        .value_counts()
                        .head(5)
                    )
                    trending_hashtags = f"\nTop 5 trending hashtags are:\n"
                    for i in range(0, len(top_five)):
                        if top_five.index[i].strip() != "":
                            trending_hashtags += (
                                f"- {top_five.index[i]} : {top_five[i]} users\n"
                            )
                    file.write(f"{trending_hashtags}\n")

                if "countries" in df.columns[df.astype(bool).any()]:
                    top_five = (
                        df["countries"]
                        .str.split(",")
                        .explode()
                        .dropna()
                        .value_counts()
                        .head(5)
                    )
                    trending_countries = (
                        f"\nTop 5 trending Countries where user contributed are:\n"
                    )
                    for i in range(0, len(top_five)):
                        if top_five.index[i].strip() != "":
                            trending_countries += (
                                f"- {top_five.index[i]} : {top_five[i]} users\n"
                            )
                    file.write(f"{trending_countries}\n")
        # Loop through the arguments
        for i in range(len(sys.argv)):
            # If the argument is '--password'
            if sys.argv[i] == "--password":
                # Replace the value with '***'
                sys.argv[i + 1] = "***"
        command = " ".join(sys.argv)
        start_repl_ts = seq_to_timestamp(start_seq_url, args.timezone)
        end_repl_ts = seq_to_timestamp(end_seq_url, args.timezone)

        with open(f"{args.name}_metadata.json", "w", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "command": str(command),
                        "source": str(args.url),
                        "start_date": str(start_date),
                        "start_seq": f"{start_seq} = {start_repl_ts}",
                        "end_date": str(end_date),
                        "end_seq": f"{end_seq} = {end_repl_ts}",
                    }
                )
            )
        print("Metadata Created")

    else:
        print("No data Found")
        sys.exit()

    end_time = time.time()
    elapsed_time = end_time - start_time

    # convert elapsed time to hr:min:sec format
    hours, rem = divmod(elapsed_time, 3600)
    minutes, seconds = divmod(rem, 60)
    print(
        "Script Completed in hr:min:sec = {:0>2}:{:0>2}:{:05.2f}".format(
            int(hours), int(minutes), seconds
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        print(ex)
        sys.exit()
