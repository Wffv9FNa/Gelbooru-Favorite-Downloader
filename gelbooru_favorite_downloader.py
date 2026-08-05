"""Download a Gelbooru user's favourites to local folders organised by character,
copyright and content rating.

Reads tuning settings from config.yaml and credentials (API key, user id, username,
password) from a .env file alongside this script. Pages through the favourites list,
fetches post and tag details via the Gelbooru API, and downloads images in parallel
with adaptive rate limiting. Progress is cached so reruns skip already-downloaded posts.

Run with: python gelbooru_favorite_downloader.py

Flags:
  -logtofile        also append console output to log.txt (and debug_log.txt with --debug)
  -r/--retry-failed retry posts recorded in the failed-posts cache instead of paging favourites
  --list-failed     print failed and rate-limited posts, then exit without downloading
  --debug           emit verbose rate-limit telemetry
"""

import argparse
import html
import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from colorama import init, Fore, Style
from dotenv import load_dotenv

# Initialise colorama for Windows compatibility
init(autoreset=True)


# =============================================================================
# Colour Helpers
# =============================================================================
def c_success(text):
    """Green - for successful operations"""
    return f"{Fore.GREEN}{text}{Style.RESET_ALL}"


def c_warning(text):
    """Yellow - for warnings like rate limits"""
    return f"{Fore.YELLOW}{text}{Style.RESET_ALL}"


def c_error(text):
    """Red - for errors"""
    return f"{Fore.RED}{text}{Style.RESET_ALL}"


def c_info(text):
    """Cyan - for informational messages"""
    return f"{Fore.CYAN}{text}{Style.RESET_ALL}"


def c_header(text):
    """Magenta - for section headers"""
    return f"{Fore.MAGENTA}{Style.BRIGHT}{text}{Style.RESET_ALL}"


def c_dim(text):
    """Dim text for less important info"""
    return f"{Style.DIM}{text}{Style.RESET_ALL}"

# =============================================================================
# Configuration Loading
# =============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.yaml")
DOTENV_FILE = os.path.join(SCRIPT_DIR, ".env")

# override=False (default) so a real exported env var wins over the .env file.
load_dotenv(DOTENV_FILE)

# Placeholder values shipped in .env.example; treated as "not set".
CREDENTIAL_ENV_VARS = {
    "GELBOORU_API_KEY": "your-api-key-here",
    "GELBOORU_USER_ID": "your-user-id-here",
    "GELBOORU_USERNAME": "your-username-here",
    "GELBOORU_PASSWORD": "your-password-here",
}


def load_config():
    """Load configuration from config.yaml file."""
    if not os.path.exists(CONFIG_FILE):
        print(f"Error: Configuration file not found: {CONFIG_FILE}")
        print("Please create a config.yaml file with your tuning settings.")
        print("See config.yaml.example for reference. (Credentials go in .env - see .env.example.)")
        sys.exit(1)

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"Error: Invalid YAML in configuration file: {e}")
        sys.exit(1)

    # Check for empty config file
    if config is None:
        print("Error: Configuration file is empty.")
        print("Please copy config.yaml.example to config.yaml and fill in your tuning settings.")
        print("(Credentials go in .env - see .env.example.)")
        sys.exit(1)

    # Validate required sections
    required_sections = ["settings", "cache", "threading", "rate_limiting"]
    for section in required_sections:
        if section not in config:
            print(f"Error: Missing required section '{section}' in config.yaml")
            sys.exit(1)

    return config


def load_credentials():
    """Load and validate the four Gelbooru credentials from the environment."""
    missing = []
    values = {}
    for name, placeholder in CREDENTIAL_ENV_VARS.items():
        value = (os.getenv(name) or "").strip()
        if not value or value == placeholder:
            missing.append(name)
        values[name] = value

    if missing:
        print(f"Error: Missing Gelbooru credentials in {DOTENV_FILE}")
        print("Copy .env.example to .env and fill in the following:")
        for name in missing:
            print(f"  - {name} is not set in .env")
        sys.exit(1)

    return (
        values["GELBOORU_API_KEY"],
        values["GELBOORU_USER_ID"],
        values["GELBOORU_USERNAME"],
        values["GELBOORU_PASSWORD"],
    )


# Load configuration
config = load_config()
API_KEY, USER_ID, USERNAME, PASSWORD = load_credentials()

# General Settings
POSTS_PER_PAGE = config["settings"].get("posts_per_page", 50)
MAX_CONSECUTIVE_EMPTY_PAGES = config["settings"].get("max_consecutive_empty_pages", 10)
_base_dir = config["settings"].get("base_dir", "")
BASE_DIR = _base_dir if _base_dir else SCRIPT_DIR

# Cache Files
CACHE_FILE = config["cache"].get("tag_cache_file", "tag_cache.json")
POSTS_CACHE_FILE = config["cache"].get("posts_cache_file", "posts_cache.json")
FAILED_POSTS_CACHE_FILE = config["cache"].get(
    "failed_posts_cache_file", "failed_posts_cache.json"
)
RATE_LIMITED_POSTS_FILE = config["cache"].get(
    "rate_limited_posts_file", "rate_limited_posts.json"
)

# Threading and Performance Settings
MAX_WORKERS = config["threading"].get("max_workers", 4)
DOWNLOAD_WORKERS = config["threading"].get("download_workers", 3)
TAG_BATCH_SIZE = config["threading"].get("tag_batch_size", 20)

file_lock = threading.Lock()
failed_cache_lock = threading.Lock()

# Rate Limiting Settings
MIN_DELAY = config["rate_limiting"].get("min_delay", 0.25)
MAX_DELAY = config["rate_limiting"].get("max_delay", 5.0)
DELAY_INCREASE_FACTOR = config["rate_limiting"].get("delay_increase_factor", 1.5)
DELAY_DECREASE_FACTOR = config["rate_limiting"].get("delay_decrease_factor", 0.95)
SUCCESS_THRESHOLD = config["rate_limiting"].get("success_threshold", 15)

# Dynamic concurrency control
current_max_workers = MAX_WORKERS  # This will be reduced when we hit rate limits
workers_lock = threading.Lock()

last_api_call_time = 0
api_call_lock = threading.Lock()
adaptive_delay = MIN_DELAY  # Start with minimum delay
successful_requests = 0  # Counter for successful requests
rate_limited_posts = set()  # Track currently rate-limited posts
rate_limited_lock = threading.Lock()

# Cache buffers for batch operations
pending_posts_cache = {}
pending_tag_cache = {}
cache_update_lock = threading.Lock()

rate_stats = {
    "throttle_waits": 0,
    "throttle_wait_seconds": 0.0,
    "rate_limit_429s": 0,
    "cooldown_seconds": 0.0,
    "retries": 0,
    "peak_delay_seconds": MIN_DELAY,
    "min_workers": MAX_WORKERS,
    "waits_by_endpoint": {"detail": 0, "tag": 0, "download": 0},
    "wait_seconds_by_endpoint": {"detail": 0.0, "tag": 0.0, "download": 0.0},
}
stats_lock = threading.Lock()

# Logging settings
log_to_file = False  # Will be set to True if -logtofile flag is used
debug_enabled = False  # Will be set to True if --debug flag is used


# Logging functions
def log_message(message, log_file="log.txt"):
    print(message)
    if log_to_file:
        with open(log_file, "a") as file:
            file.write(message + "\n")


def debug_log(message):
    """Emit verbose rate-limit telemetry, prefixed with time and thread. Gated on --debug."""
    if not debug_enabled:
        return
    now = time.time()
    timestamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int((now % 1) * 1000):03d}"
    line = f"[DEBUG {timestamp} {threading.current_thread().name}] {message}"
    print(c_dim(line), flush=True)
    if log_to_file:
        with open("debug_log.txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")


def countdown_sleep(seconds, reason="Waiting", show_done=True):
    """Sleep with a visible countdown timer so users know the script is still working."""
    total = int(seconds)
    if total >= 1:
        for remaining in range(total, 0, -1):
            print(f"\r{reason}: {remaining}s remaining...  ", end="", flush=True)
            time.sleep(1)
        # Clear the countdown line
        if show_done:
            print(f"\r{reason}: Done.{' ' * 20}")
        else:
            print(f"\r{' ' * 60}\r", end="", flush=True)
    # Sleep any fractional remainder (or full time if < 1 second)
    remainder = seconds - total if total >= 1 else seconds
    if remainder > 0:
        time.sleep(remainder)


# Login function
def login():
    LOGIN_SUCCESS_MARKER = ">Logout</a>"
    session = requests.Session()
    login_url = "https://gelbooru.com/index.php?page=account&s=login&code=00"
    login_data = {"user": USERNAME, "pass": PASSWORD, "submit": "Log in"}

    try:
        response = session.post(login_url, data=login_data, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(c_error(f"Could not reach Gelbooru to log in: {e}"))
        sys.exit(1)

    if LOGIN_SUCCESS_MARKER not in response.text:
        log_message(f"Login response (first 200 chars): {response.text[:200]}")
        print(c_error(
            "Login failed: check GELBOORU_USERNAME / GELBOORU_PASSWORD in .env"
        ))
        sys.exit(1)

    return session


# Functions related to fetching post data

# Distinct from an empty list (end of favourites) so a failed fetch is not treated as the end.
FETCH_FAILED = object()

# Distinct from a fetch failure so a deleted post is not reported as an error.
POST_MISSING = object()


def get_favorite_post_ids(session, pid):
    """Scrape one page of favourite post ids starting at offset pid.

    Returns a list of post id strings (empty when the page is past the last
    favourite), or the FETCH_FAILED sentinel if the page could not be retrieved
    after all retries. The empty-list and FETCH_FAILED cases are kept distinct so
    a transient failure is not mistaken for the end of the favourites.
    """
    url = f"https://gelbooru.com/index.php?page=favorites&s=view&id={USER_ID}&pid={pid}"
    max_retries = 5
    base_delay = 5

    for i in range(max_retries):
        try:
            response = session.get(url, timeout=30)
            if response.status_code == 429:
                handle_rate_limit_response()
                raise requests.exceptions.RequestException("Too Many Requests")

            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")
            post_spans = soup.find_all("span", class_="thumb")
            post_ids = [span.find("a")["href"].split("=")[-1] for span in post_spans]
            reset_adaptive_delay()
            debug_log(f"[favourites pid={pid}] fetched {len(post_ids)} post ids on attempt {i + 1}")
            if i > 0:
                log_message(
                    f"Successfully retrieved favourite page pid={pid} after {i + 1} attempts"
                )
            return post_ids

        except requests.exceptions.RequestException as e:
            if "Too Many Requests" in str(e):
                handle_rate_limit_response()

            if i < max_retries - 1:
                delay = base_delay * (2**i)
                with stats_lock:
                    rate_stats["retries"] += 1
                debug_log(f"[favourites pid={pid}] retry {i + 1}/{max_retries} in {delay}s: {str(e)[:60]}")
                log_message(
                    f"Favourite page pid={pid}: {e!s}. Retrying after {delay}s (attempt {i + 1}/{max_retries})"
                )
                countdown_sleep(delay, f"Retry backoff for favourite page pid={pid}")
            else:
                debug_log(f"[favourites pid={pid}] gave up after {max_retries} attempts: {str(e)[:60]}")
                log_message(
                    f"Failed to get favourite page pid={pid} after {max_retries} attempts: {e!s}"
                )
                return FETCH_FAILED

    # Defensive: only reachable if max_retries ever becomes 0 or negative.
    return FETCH_FAILED


def get_post_details(post_id):
    """Fetch a post's details from the API.

    Returns the string "SKIP" if the post is already in the posts cache, a
    single-element list holding the post dict on success, the POST_MISSING
    sentinel if the API no longer returns the post (deleted or hidden), or None
    if it could not be fetched after all retries. POST_MISSING and None are kept
    distinct so a deleted favourite is not counted as a fetch failure.
    """
    posts_cache = load_posts_cache()

    if post_id in posts_cache:
        return "SKIP"

    rate_limit_api_call("detail")
    url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&id={post_id}&json=1&api_key={API_KEY}&user_id={USER_ID}"
    max_retries = 5
    base_delay = 5  # Increased base delay for rate limiting

    for i in range(max_retries):
        try:
            response = requests.get(url, timeout=30)
            if response.status_code == 429:
                handle_rate_limit_response()
                add_rate_limited_post(post_id)  # Track rate-limited post
                raise requests.exceptions.RequestException("Too Many Requests")

            response.raise_for_status()

            data = json.loads(response.text)
            if "post" in data:
                post = data["post"]
                reset_adaptive_delay()  # Success, so we can reduce delay if it was increased
                if i > 0:  # If this was a retry attempt
                    log_message(
                        f"Successfully retrieved post {post_id} after {i+1} attempts"
                    )
                remove_rate_limited_post(post_id)  # Remove from tracking if successful
                return post if isinstance(post, list) else [post]
            else:
                reset_adaptive_delay()  # Success, so we can reduce delay if it was increased
                remove_rate_limited_post(
                    post_id
                )  # Remove from tracking if request completed
                debug_log(f"post {post_id} is no longer returned by the API (deleted or hidden)")
                return POST_MISSING

        except requests.exceptions.RequestException as e:
            if "Too Many Requests" in str(e):
                handle_rate_limit_response()
                add_rate_limited_post(post_id)  # Track rate-limited post
                log_message(
                    f"Rate limit hit for post {post_id:<8} - Attempt {i + 1}/{max_retries}"
                )

            if i < max_retries - 1:
                delay = base_delay * (2**i)  # Exponential backoff
                with stats_lock:
                    rate_stats["retries"] += 1
                debug_log(f"[post {post_id}] retry {i + 1}/{max_retries} in {delay}s: {str(e)[:60]}")
                log_message(
                    f"Post {post_id:<8}: {e!s}. Retrying after {delay}s (attempt {i + 1}/{max_retries})"
                )
                countdown_sleep(delay, f"Retry backoff for post {post_id}")
            else:
                log_message(
                    f"Failed to get post {post_id:<8} after {max_retries} attempts: {e!s}"
                )
                # Save the post ID to the cache when it exceeds max retries
                with failed_cache_lock:
                    failed_posts_cache = load_failed_posts_cache()
                    failed_posts_cache[str(post_id)] = {"error": str(e)[:100], "type": "api"}
                    save_failed_posts_cache(failed_posts_cache)
                remove_rate_limited_post(
                    post_id
                )  # Remove from tracking after max retries
                return None


# Functions related to downloading and saving images
# Global session for connection pooling
download_session = requests.Session()
download_session.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        # The image host's hotlink protection serves the HTML post page, not the bytes, without this.
        "Referer": "https://gelbooru.com/",
    }
)


def resolve_download_url(post) -> tuple[str, str]:
    """Return (download_url, file_name) for a post. file_url is authoritative
    unless it is served from a different host than preview_url (videos use a
    video-cdn host that returns an HTML error page), in which case the real
    file is reconstructed on the preview host from directory/image."""
    file_url = post["file_url"]
    preview_url = post.get("preview_url")
    directory = post.get("directory")
    image = post.get("image")
    if preview_url and directory and image:
        preview_host = urlparse(preview_url).netloc
        if preview_host and urlparse(file_url).netloc != preview_host:
            return f"https://{preview_host}/images/{directory}/{image}", image
    return file_url, file_url.split("/")[-1]


def is_retryable_download_error(error: Exception) -> bool:
    """Transport faults and 5xx are worth retrying. A 4xx such as a 404 video-cdn URL is terminal."""
    if isinstance(error, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    response = getattr(error, "response", None)
    return response is not None and 500 <= response.status_code < 600


def download_image(url, file_path):
    max_retries = 3
    base_delay = 2

    for attempt in range(max_retries):
        rate_limit_api_call("download")
        try:
            response = download_session.get(url, timeout=30)

            # Check 429 before raise_for_status so it routes to backoff, not a generic HTTPError.
            if response.status_code == 429:
                handle_rate_limit_response()
                if attempt < max_retries - 1:
                    with stats_lock:
                        rate_stats["retries"] += 1
                    continue

            response.raise_for_status()
        except Exception as e:
            if attempt < max_retries - 1 and is_retryable_download_error(e):
                delay = base_delay * (2**attempt)
                with stats_lock:
                    rate_stats["retries"] += 1
                debug_log(
                    f"download retry {attempt + 1}/{max_retries} in {delay}s: {str(e)[:60]}"
                )
                # Silent sleep, not countdown_sleep: this runs on a worker thread under the progress bar.
                time.sleep(delay)
                continue
            raise Exception(f"Error downloading image: {e!s}") from e

        with open(file_path, "wb") as f:
            f.write(response.content)
        reset_adaptive_delay()
        return


def sanitize_for_path(name):
    """Sanitise a string for use as a Windows file or directory name.

    Replaces each character that is invalid on Windows (<>:"/\\|?*) with an underscore.
    """
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        name = name.replace(char, "_")
    return name


def build_destination_dir(character_tags, copyright_tag, sensitivity) -> str:
    """Return the destination directory for a post. Callers create it themselves."""
    base_folder_name, specific_folder_name = get_folder_name(
        character_tags, copyright_tag
    )
    base_folder_name = sanitize_for_path(base_folder_name)

    if specific_folder_name:
        specific_folder_name = sanitize_for_path(specific_folder_name)
        return os.path.join(
            BASE_DIR, base_folder_name, specific_folder_name, sensitivity
        )
    return os.path.join(BASE_DIR, base_folder_name, sensitivity)


def download_and_save_image(post, character_tags, sensitivity, copyright_tag):
    file_url, file_name = resolve_download_url(post)

    path = build_destination_dir(character_tags, copyright_tag, sensitivity)

    if not os.path.exists(path):
        os.makedirs(path)

    file_path = os.path.join(path, file_name)

    if os.path.exists(file_path):
        log_message(
            f"Skipping download of image {file_name} for post {post['id']:<8} because it already exists"
        )
        return True  # Indicate success since file exists

    try:
        download_image(file_url, file_path)
        return True  # Indicate successful download
    except Exception as e:
        log_message(
            f"Error downloading image {file_name} for post {post['id']:<8}: {e!s}"
        )
        return False  # Indicate failed download


# Optimized batch operations
def flush_cache_buffers():
    """Flush pending cache updates to disk"""
    global pending_posts_cache, pending_tag_cache

    with cache_update_lock:
        if pending_posts_cache:
            posts_cache = load_posts_cache()
            posts_cache.update(pending_posts_cache)
            save_posts_cache(posts_cache)
            pending_posts_cache.clear()

        if pending_tag_cache:
            tag_cache = load_cache()
            tag_cache.update(pending_tag_cache)
            save_cache(tag_cache)
            pending_tag_cache.clear()


# Named so every post is accounted for in the per-page line, not just downloads.
POST_DOWNLOADED = "downloaded"
POST_ON_DISK = "on_disk"
POST_ALREADY_CACHED = "already_cached"
POST_DOWNLOAD_FAILED = "download_failed"

POST_OUTCOMES = (
    POST_DOWNLOADED,
    POST_ON_DISK,
    POST_ALREADY_CACHED,
    POST_DOWNLOAD_FAILED,
)


def batch_process_posts(post_ids):
    """Process multiple posts in parallel, returning a count per POST_OUTCOMES key"""
    download_results = dict.fromkeys(POST_OUTCOMES, 0)
    failed_count = 0

    # First, fetch all post details in parallel with dynamic worker count
    total_posts = len(post_ids)
    print(c_info("Fetching post details..."))
    with workers_lock:
        worker_count = current_max_workers
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        # Submit all post detail fetching tasks with staggered delays
        future_to_post_id = {}
        for i, post_id in enumerate(post_ids):
            # Add small staggered delay to prevent simultaneous API hits
            if i > 0:
                time.sleep(0.1)  # 100ms delay between task submissions
            future_to_post_id[executor.submit(get_post_details, post_id)] = post_id

        posts_to_process = []
        completed_count = 0
        cached_count = 0
        missing_count = 0

        for future in as_completed(future_to_post_id):
            post_id = future_to_post_id[future]
            completed_count += 1
            try:
                post_details = future.result()
                # POST_MISSING is checked first: it is truthy and not subscriptable.
                if post_details is POST_MISSING:
                    missing_count += 1
                elif post_details == "SKIP":
                    cached_count += 1
                elif post_details and post_details[0]:
                    posts_to_process.append(post_details[0])
                else:
                    failed_count += 1
            except Exception as e:
                if "Too Many Requests" not in str(e):
                    failed_count += 1

            # Update progress bar with colours
            progress = int((completed_count / total_posts) * 20)
            bar_done = Fore.GREEN + "=" * progress
            bar_remaining = Fore.WHITE + "-" * (20 - progress)
            bar = bar_done + bar_remaining + Style.RESET_ALL

            new_count = c_success(f"new: {len(posts_to_process)}")
            cached_str = c_dim(f"cached: {cached_count}") if cached_count > 0 else ""
            missing_str = c_warning(f"missing: {missing_count}") if missing_count > 0 else ""
            failed_str = c_error(f"failed: {failed_count}") if failed_count > 0 else ""
            status_parts = [s for s in [new_count, cached_str, missing_str, failed_str] if s]
            status = ", ".join(status_parts)

            print(f"\r  [{bar}] {completed_count}/{total_posts} ({status})  ", end="", flush=True)

        print()  # New line after progress

    if not posts_to_process:
        return download_results

    # Collect all unique tags from all posts for batch processing
    print(c_info("Processing tags..."))
    all_tags = set()
    for post in posts_to_process:
        all_tags.update(post["tags"].split())
    print(f"Found {len(all_tags)} unique tags to process...")

    # Batch fetch tag details
    batch_fetch_tag_details(list(all_tags))

    # Process posts with image downloads in parallel
    with workers_lock:
        worker_count = current_max_workers
    with ThreadPoolExecutor(max_workers=min(worker_count, DOWNLOAD_WORKERS)) as executor:
        futures = [
            executor.submit(process_post, post) for post in posts_to_process
        ]

        for future in as_completed(futures):
            try:
                download_results[future.result()] += 1
            except Exception as e:
                download_results[POST_DOWNLOAD_FAILED] += 1
                log_message(f"Error processing post: {e!s}")

    # Flush cache updates
    flush_cache_buffers()
    return download_results


def format_page_summary(download_results, elapsed):
    """Build the per-page result line, naming every outcome rather than downloads alone"""
    downloaded_count = download_results[POST_DOWNLOADED]

    extras = []
    if download_results[POST_ON_DISK] > 0:
        extras.append(c_dim(f"{download_results[POST_ON_DISK]} already on disk"))
    if download_results[POST_ALREADY_CACHED] > 0:
        extras.append(c_dim(f"{download_results[POST_ALREADY_CACHED]} cached mid-run"))
    if download_results[POST_DOWNLOAD_FAILED] > 0:
        extras.append(c_error(f"{download_results[POST_DOWNLOAD_FAILED]} failed"))
    extra_str = (", " + ", ".join(extras)) if extras else ""

    if downloaded_count > 0:
        return c_success(f"Downloaded {downloaded_count} new images") + extra_str + c_dim(f" in {elapsed:.1f}s")
    if extras:
        return c_dim("No new downloads") + extra_str + c_dim(f" - {elapsed:.1f}s")
    return c_dim(f"No new images (all cached) - {elapsed:.1f}s")


def batch_fetch_tag_details(tags):
    """Fetch tag details in parallel batches"""
    cache = load_cache()
    tags_to_fetch = [
        tag for tag in tags if tag not in cache and tag not in pending_tag_cache
    ]

    if not tags_to_fetch:
        return

    # Process tags in batches to avoid overwhelming the API
    total_tags = len(tags_to_fetch)
    print(c_info(f"Fetching {total_tags} new tag details..."))
    tags_completed = 0

    for i in range(0, total_tags, TAG_BATCH_SIZE):
        batch = tags_to_fetch[i : i + TAG_BATCH_SIZE]

        with workers_lock:
            worker_count = current_max_workers
        with ThreadPoolExecutor(max_workers=min(len(batch), worker_count)) as executor:
            future_to_tag = {
                executor.submit(get_tag_details_single, tag): tag for tag in batch
            }

            for future in as_completed(future_to_tag):
                tag = future_to_tag[future]
                tags_completed += 1
                try:
                    tag_details = future.result()
                    if tag_details:
                        with cache_update_lock:
                            pending_tag_cache[tag] = tag_details
                except Exception:
                    pass  # Silently skip tag errors

                # Update progress bar with colours
                progress = int((tags_completed / total_tags) * 20)
                bar_done = Fore.CYAN + "=" * progress
                bar_remaining = Fore.WHITE + "-" * (20 - progress)
                bar = bar_done + bar_remaining + Style.RESET_ALL
                print(f"\r  [{bar}] {tags_completed}/{total_tags} tags  ", end="", flush=True)

        # Small delay between batches to respect rate limits
        time.sleep(0.5)

    print()  # New line after progress


def get_tag_details_single(tag):
    """Fetch single tag details without caching logic"""
    rate_limit_api_call("tag")

    modified_tag = (
        tag.replace("&#039;", "'")
        .replace("&gt;", ">")
        .replace("&lt;", "<")
        .replace("&quot;", '"')
        .replace("&amp;", "&")
    )
    encoded_tag = quote(modified_tag)
    url = f"https://gelbooru.com/index.php?page=dapi&s=tag&q=index&json=1&name={encoded_tag}&api_key={API_KEY}&user_id={USER_ID}"

    max_retries = 3  # Reduced retries for batch operations
    base_delay = 2

    for i in range(max_retries):
        try:
            response = requests.get(url, timeout=10)

            # Check 429 before raise_for_status so it routes to backoff, not a generic HTTPError.
            if response.status_code == 429:
                handle_rate_limit_response()
                raise requests.exceptions.RequestException("HTTP 429 rate limited")

            response.raise_for_status()

            data = json.loads(response.text)
            if data and "tag" in data and data["tag"]:
                reset_adaptive_delay()
                tag_data = data["tag"][0]
                tag_data["name"] = html.unescape(tag_data["name"])
                return tag_data
            else:
                reset_adaptive_delay()
                return None

        except requests.exceptions.RequestException as e:
            if "Too Many Requests" in str(e):
                handle_rate_limit_response()

            if i < max_retries - 1:
                delay = base_delay * (2**i)
                with stats_lock:
                    rate_stats["retries"] += 1
                debug_log(f"[tag {tag}] retry {i + 1}/{max_retries} in {delay}s: {str(e)[:60]}")
                time.sleep(delay)
            else:
                debug_log(f"[tag {tag}] gave up after {max_retries} attempts: {str(e)[:60]}")
                return None

    # Defensive: only reachable if max_retries ever becomes 0 or negative.
    return None


def process_post(post):
    """Process post with buffered cache updates, returning one of POST_OUTCOMES"""
    post_id = post["id"]

    # Safety check in case this function is called directly
    # Normally cached posts are filtered out earlier in batch_process_posts
    posts_cache = load_posts_cache()
    with cache_update_lock:
        if post_id in posts_cache or post_id in pending_posts_cache:
            log_message(f"Post {post_id:<8} found in cache during processing, skipping")
            return POST_ALREADY_CACHED

    file_url, file_name = resolve_download_url(post)
    sensitivity = get_sensitivity(post)

    character_tags = get_character_tags(post["tags"])
    copyright_tag = get_copyright_tag(post["tags"])

    path = build_destination_dir(character_tags, copyright_tag, sensitivity)

    file_path = os.path.join(path, file_name)

    if not os.path.exists(file_path):
        try:
            if not os.path.exists(path):
                os.makedirs(path)
            download_image(file_url, file_path)
            outcome = POST_DOWNLOADED
            # Format download message with colour
            print(f"  {c_success('+')} {c_dim(file_name[:45])} {c_dim('post')} {post_id}")
            # Only add to cache if download succeeded
            with cache_update_lock:
                pending_posts_cache[post_id] = True
        except Exception as e:
            print(f"  {c_error('x')} {c_error('Failed:')} {file_name[:30]} - {str(e)[:30]}")
            outcome = POST_DOWNLOAD_FAILED
            # Track download failures so they can be retried later
            with failed_cache_lock:
                failed_cache = load_failed_posts_cache()
                failed_cache[str(post_id)] = {"error": str(e)[:100], "type": "download"}
                save_failed_posts_cache(failed_cache)
    else:
        # File already exists, safe to cache
        outcome = POST_ON_DISK
        with cache_update_lock:
            pending_posts_cache[post_id] = True

    return outcome


def get_character_tags(tags):
    """Retrieve character tags using cached data"""
    character_tags = []
    cache = load_cache()

    for tag in tags.split():
        # Check both main cache and pending cache
        tag_details = cache.get(tag)
        if not tag_details:
            with cache_update_lock:
                tag_details = pending_tag_cache.get(tag)

        if tag_details and "type" in tag_details and int(tag_details["type"]) == 4:
            character_tags.append(html.unescape(tag_details["name"]))

    return character_tags


def get_copyright_tag(tags):
    """Retrieve copyright tag using cached data"""
    cache = load_cache()

    for tag in tags.split():
        # Check both main cache and pending cache
        tag_details = cache.get(tag)
        if not tag_details:
            with cache_update_lock:
                tag_details = pending_tag_cache.get(tag)

        if tag_details and "type" in tag_details and int(tag_details["type"]) == 3:
            return html.unescape(tag_details["name"])

    return None


# Functions for managing rate-limited posts
def load_rate_limited_posts():
    """Load the set of rate-limited posts from disk"""
    try:
        with open(RATE_LIMITED_POSTS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def _save_rate_limited_posts_unlocked():
    """Save rate-limited posts to disk. Must be called while holding rate_limited_lock."""
    with open(RATE_LIMITED_POSTS_FILE, "w") as f:
        json.dump(list(rate_limited_posts), f)


def add_rate_limited_post(post_id):
    """Add a post to the rate-limited tracking set"""
    with rate_limited_lock:
        rate_limited_posts.add(post_id)
        _save_rate_limited_posts_unlocked()
        tracked = len(rate_limited_posts)
    debug_log(f"now tracking rate-limited post {post_id} ({tracked} tracked)")


def remove_rate_limited_post(post_id):
    """Remove a post from the rate-limited tracking set"""
    removed = False
    with rate_limited_lock:
        if post_id in rate_limited_posts:
            rate_limited_posts.remove(post_id)
            _save_rate_limited_posts_unlocked()
            removed = True
        tracked = len(rate_limited_posts)
    if removed:
        debug_log(f"cleared rate-limited post {post_id} ({tracked} still tracked)")


# Functions related to cache handling
def load_cache():
    try:
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)


def load_posts_cache():
    try:
        with open(POSTS_CACHE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_posts_cache(cache):
    with open(POSTS_CACHE_FILE, "w") as f:
        json.dump(cache, f)


def load_failed_posts_cache():
    file_lock.acquire()
    try:
        with open(FAILED_POSTS_CACHE_FILE, "r") as f:
            if os.stat(FAILED_POSTS_CACHE_FILE).st_size == 0:
                return {}
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.decoder.JSONDecodeError:
        with open(FAILED_POSTS_CACHE_FILE, "r") as f:
            print(f"Error decoding JSON, file contents: {f.read()}")
        return {}
    finally:
        file_lock.release()


def save_failed_posts_cache(cache):
    with open(FAILED_POSTS_CACHE_FILE, "w") as f:
        json.dump(cache, f)


# Functions related to post processing
def get_sensitivity(post):
    rating = post.get("rating")
    if rating == "sensitive":
        return "Sensitive"
    elif rating == "questionable":
        return "Questionable"
    elif rating == "explicit":
        return "Explicit"
    else:
        return "General"


def get_folder_name(character_tags, copyright_tag):
    if not character_tags:
        return ("No Character", None)
    elif len(character_tags) == 1:
        return (character_tags[0].replace(":", "-"), None)
    else:
        if copyright_tag:
            return ("Multiple", copyright_tag.replace(":", "-"))
        else:
            return ("Multiple", None)


def rate_limit_api_call(endpoint="api"):
    """Ensure we don't make API calls too frequently"""
    global last_api_call_time, adaptive_delay

    with api_call_lock:
        current_time = time.time()
        earliest_allowed = last_api_call_time + adaptive_delay

        if current_time >= earliest_allowed:
            sleep_time = 0
            last_api_call_time = current_time
        else:
            sleep_time = earliest_allowed - current_time
            last_api_call_time = earliest_allowed
        delay_snapshot = adaptive_delay

    # Sleep OUTSIDE the lock so other threads aren't blocked
    if sleep_time > 0:
        with stats_lock:
            rate_stats["throttle_waits"] += 1
            rate_stats["throttle_wait_seconds"] += sleep_time
            if endpoint in rate_stats["waits_by_endpoint"]:
                rate_stats["waits_by_endpoint"][endpoint] += 1
                rate_stats["wait_seconds_by_endpoint"][endpoint] += sleep_time
        debug_log(f"throttle wait {sleep_time:.2f}s (adaptive_delay={delay_snapshot:.2f}s)")
        if sleep_time >= 2:
            countdown_sleep(sleep_time, "Rate limiting", show_done=False)
        else:
            time.sleep(sleep_time)
    else:
        debug_log(f"no throttle wait (adaptive_delay={delay_snapshot:.2f}s)")


def handle_rate_limit_response():
    """Adjust rate limiting parameters when we hit a rate limit"""
    global adaptive_delay, successful_requests, current_max_workers

    with api_call_lock:
        old_delay = adaptive_delay
        adaptive_delay = min(adaptive_delay * DELAY_INCREASE_FACTOR, MAX_DELAY)
        successful_requests = 0  # Reset success counter on rate limit

        # Reduce concurrent workers when we hit rate limits
        with workers_lock:
            old_workers = current_max_workers
            current_max_workers = max(
                1, current_max_workers - 1
            )  # Reduce workers but keep at least 1
        new_workers = current_max_workers
        new_delay = adaptive_delay

        # Force a longer pause after rate limit
        sleep_time = adaptive_delay * 2

    with stats_lock:
        rate_stats["rate_limit_429s"] += 1
        rate_stats["cooldown_seconds"] += sleep_time
        rate_stats["peak_delay_seconds"] = max(rate_stats["peak_delay_seconds"], new_delay)
        rate_stats["min_workers"] = min(rate_stats["min_workers"], new_workers)
        total_429s = rate_stats["rate_limit_429s"]

    print(c_warning(f"\n! Rate limited - backing off ({new_delay:.1f}s delay)"), flush=True)
    debug_log(
        f"429 #{total_429s}: adaptive_delay {old_delay:.2f}s -> {new_delay:.2f}s, "
        f"workers {old_workers} -> {new_workers}, cooldown {sleep_time:.2f}s"
    )

    # Countdown outside the lock so other threads aren't blocked
    countdown_sleep(sleep_time, c_warning("Rate limit cooldown"))


def reset_adaptive_delay():
    """Reset adaptive delay to normal when requests are successful"""
    global adaptive_delay, successful_requests, current_max_workers
    decreased = False
    ramped = False
    with api_call_lock:
        successful_requests += 1

        if successful_requests >= SUCCESS_THRESHOLD:
            if adaptive_delay > MIN_DELAY:
                old_delay = adaptive_delay
                adaptive_delay = max(adaptive_delay * DELAY_DECREASE_FACTOR, MIN_DELAY)
                new_delay = adaptive_delay
                decreased = True

            # ramp independently of the delay, else a 429 burst pins workers for the session
            with workers_lock:
                if current_max_workers < MAX_WORKERS:
                    old_workers = current_max_workers
                    current_max_workers += 1
                    new_workers = current_max_workers
                    ramped = True

            successful_requests = 0  # Reset counter after adjustment

    if decreased:
        debug_log(
            f"{SUCCESS_THRESHOLD} clean requests: adaptive_delay {old_delay:.2f}s -> {new_delay:.2f}s"
        )
    if ramped:
        debug_log(
            f"{SUCCESS_THRESHOLD} clean requests: workers {old_workers} -> {new_workers}"
        )


def print_rate_limit_summary():
    """Print accumulated rate-limit telemetry; helps evaluate and tune config.yaml."""
    with stats_lock:
        s = dict(rate_stats)
    print(c_header("\n" + "=" * 60))
    print(c_header("  Rate-limit summary"))
    print(c_header("=" * 60))
    print(f"  429 responses hit:      {s['rate_limit_429s']}")
    print(f"  Retry attempts:         {s['retries']}")
    print(f"  Throttle spacing waits: {s['throttle_waits']} ({s['throttle_wait_seconds']:.1f}s total)")
    for ep in ("detail", "tag", "download"):
        print(
            f"    - {ep:<8s} {s['waits_by_endpoint'][ep]} "
            f"({s['wait_seconds_by_endpoint'][ep]:.1f}s)"
        )
    print(f"  429 cooldown time:      {s['cooldown_seconds']:.1f}s total")
    print(f"  Peak adaptive delay:    {s['peak_delay_seconds']:.2f}s (config max {MAX_DELAY:.2f}s)")
    print(f"  Min concurrent workers: {s['min_workers']} (config start {MAX_WORKERS})")
    print(f"  Final adaptive delay:   {adaptive_delay:.2f}s")


def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully by saving caches before exiting"""
    # Print immediately to confirm signal received
    print(c_warning("\n\nInterrupted! Saving progress..."))
    sys.stdout.flush()

    # Save any pending cached data
    try:
        flush_cache_buffers()
        print(c_success("Progress saved."))
    except Exception as e:
        print(c_error(f"Warning: Error saving caches: {e!s}"))

    try:
        print_rate_limit_summary()
    except Exception:
        pass

    print(c_info("Goodbye!"))
    sys.stdout.flush()
    # Use os._exit() to forcefully terminate all threads immediately
    os._exit(0)


def retry_failed_posts():
    """Retry downloading posts that previously failed."""
    failed_cache = load_failed_posts_cache()

    if not failed_cache:
        print(c_info("No failed posts to retry."))
        return

    failed_post_ids = list(failed_cache.keys())
    print(c_header(f"\n{'='*60}"))
    print(c_header(f"  Retrying {len(failed_post_ids)} previously failed posts"))
    print(c_header(f"{'='*60}"))

    # First, fetch all post details to gather tags
    print(c_info("Fetching post details..."))
    posts_to_retry = []
    stale_post_ids = []  # "SKIP" => already in posts_cache, i.e. recovered in a prior run.
    missing_post_ids = []
    for post_id in failed_post_ids:
        rate_limit_api_call("detail")
        post_details = get_post_details(post_id)
        if post_details is POST_MISSING:
            missing_post_ids.append(post_id)
        elif post_details == "SKIP":
            stale_post_ids.append(post_id)
        elif post_details and post_details[0]:
            posts_to_retry.append((post_id, post_details[0]))

    if stale_post_ids:
        for post_id in stale_post_ids:
            failed_cache.pop(post_id, None)
        save_failed_posts_cache(failed_cache)
        print(c_info(f"Cleared {len(stale_post_ids)} stale entries already downloaded"))

    # Batch fetch all tags
    if posts_to_retry:
        all_tags = set()
        for _, post in posts_to_retry:
            all_tags.update(post["tags"].split())
        print(c_info(f"Fetching {len(all_tags)} unique tags..."))
        batch_fetch_tag_details(list(all_tags))

    success_count = 0
    still_failed = 0

    for i, (post_id, post) in enumerate(posts_to_retry):
        progress = int(((i + 1) / len(posts_to_retry)) * 20)
        bar_done = Fore.CYAN + "=" * progress
        bar_remaining = Fore.WHITE + "-" * (20 - progress)
        bar = bar_done + bar_remaining + Style.RESET_ALL
        print(f"\r  [{bar}] {i + 1}/{len(posts_to_retry)} - Post {post_id}  ", end="", flush=True)

        # Get tags for this post
        character_tags = get_character_tags(post["tags"])
        copyright_tag = get_copyright_tag(post["tags"])
        sensitivity = get_sensitivity(post)

        # Try to download
        if download_and_save_image(post, character_tags, sensitivity, copyright_tag):
            # Success! Remove from failed cache
            del failed_cache[post_id]
            save_failed_posts_cache(failed_cache)

            # Add to posts cache
            posts_cache = load_posts_cache()
            posts_cache[post_id] = True
            save_posts_cache(posts_cache)

            success_count += 1
            print(f"\r  {c_success('+')} Post {post_id} - recovered successfully{' '*20}")
        else:
            still_failed += 1

    # Handle posts that couldn't be fetched (stale and missing entries are not failures).
    for post_id in failed_post_ids:
        if post_id in stale_post_ids or post_id in missing_post_ids:
            continue
        if not any(pid == post_id for pid, _ in posts_to_retry):
            still_failed += 1

    print()  # New line after progress

    if success_count > 0:
        print(c_success(f"\nRecovered {success_count} posts"))
    if missing_post_ids:
        print(c_warning(f"{len(missing_post_ids)} posts no longer exist on Gelbooru (deleted or hidden)"))
        print(c_dim(f"  {', '.join(missing_post_ids)}"))
    if still_failed > 0:
        print(c_warning(f"{still_failed} posts still failing"))

    print(c_success("\n" + "="*60))
    print(c_success("  Retry complete!"))
    print(c_success("="*60))


# Main function
def main():
    parser = argparse.ArgumentParser(
        description="Download favourite images from Gelbooru"
    )
    parser.add_argument("-logtofile", help="log output to file", action="store_true")
    parser.add_argument(
        "-r", "--retry-failed",
        help="retry previously failed posts instead of normal operation",
        action="store_true"
    )
    parser.add_argument(
        "--list-failed",
        help="list all failed posts without retrying",
        action="store_true"
    )
    parser.add_argument(
        "--debug",
        help="emit verbose rate-limit telemetry (per-event timing, backoff, retries)",
        action="store_true"
    )
    args = parser.parse_args()

    global log_to_file, rate_limited_posts, debug_enabled
    log_to_file = args.logtofile
    debug_enabled = args.debug

    # Handle --list-failed
    if args.list_failed:
        failed_cache = load_failed_posts_cache()
        rate_limited = load_rate_limited_posts()

        print(c_header("\nFailed Posts Status"))
        print(c_header("="*40))

        if failed_cache:
            print(c_error(f"\nFailed posts ({len(failed_cache)}):"))
            for post_id in sorted(failed_cache.keys()):
                error_info = failed_cache[post_id]
                if isinstance(error_info, dict):
                    error_type = error_info.get("type", "unknown")
                    error_msg = error_info.get("error", "")[:50]
                    print(f"  - {post_id} [{error_type}] {c_dim(error_msg)}")
                else:
                    print(f"  - {post_id}")
        else:
            print(c_dim("\nNo failed posts."))

        if rate_limited:
            print(c_warning(f"\nRate-limited ({len(rate_limited)} posts):"))
            for post_id in sorted(rate_limited):
                print(f"  - {post_id}")
        else:
            print(c_dim("\nNo rate-limited posts."))

        print()
        return

    # Load any previously rate-limited posts
    rate_limited_posts = load_rate_limited_posts()
    if rate_limited_posts:
        print(c_warning(f"Found {len(rate_limited_posts)} previously rate-limited posts to retry"))

    # Register signal handler for graceful exit
    signal.signal(signal.SIGINT, signal_handler)
    print(c_dim("Press Ctrl+C to gracefully exit the program."))

    # Handle --retry-failed mode
    if args.retry_failed:
        retry_failed_posts()
        print_rate_limit_summary()
        return

    session = login()

    pid = 0
    consecutive_empty_pages = (
        0  # Counter for consecutive pages without downloaded images
    )

    while consecutive_empty_pages < MAX_CONSECUTIVE_EMPTY_PAGES:
        post_ids = get_favorite_post_ids(session, pid)
        if post_ids is FETCH_FAILED:
            print(c_error(f"Could not fetch favourite page (pid={pid}) after retries; stopping to avoid missing posts."))
            break
        if not post_ids:
            print(c_info("No more favourite posts found."))
            break

        page_num = (pid // POSTS_PER_PAGE) + 1
        print(c_header(f"\n{'='*60}"))
        print(c_header(f"  Page {page_num} - {len(post_ids)} favourite posts"))
        print(c_header(f"{'='*60}"))

        # Process posts in batches
        start_time = time.time()
        download_results = batch_process_posts(post_ids)
        end_time = time.time()

        elapsed = end_time - start_time
        print(format_page_summary(download_results, elapsed))
        downloaded_images = download_results[POST_DOWNLOADED] > 0

        if not downloaded_images:
            consecutive_empty_pages += 1
        else:
            consecutive_empty_pages = 0

        if len(post_ids) < POSTS_PER_PAGE:
            print(c_info("\nReached the last page of favourite posts."))
            break

        pid += POSTS_PER_PAGE

    if consecutive_empty_pages >= MAX_CONSECUTIVE_EMPTY_PAGES:
        print(c_info(f"\nNo new images for {MAX_CONSECUTIVE_EMPTY_PAGES} consecutive pages."))

    # Final cleanup - flush any remaining cache updates
    flush_cache_buffers()

    # Report on any remaining rate-limited posts
    remaining_rate_limited = len(rate_limited_posts)
    if remaining_rate_limited > 0:
        print(c_warning(f"\n{remaining_rate_limited} posts still rate-limited (will retry next run)"))

    print_rate_limit_summary()

    print(c_success("\n" + "="*60))
    print(c_success("  Complete! All progress saved."))
    print(c_success("="*60))


if __name__ == "__main__":
    main()
