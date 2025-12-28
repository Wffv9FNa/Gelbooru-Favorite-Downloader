import argparse
import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
import yaml
from bs4 import BeautifulSoup
from colorama import init, Fore, Style

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


def load_config():
    """Load configuration from config.yaml file."""
    if not os.path.exists(CONFIG_FILE):
        print(f"Error: Configuration file not found: {CONFIG_FILE}")
        print("Please create a config.yaml file with your settings.")
        print("See config.yaml.example for reference.")
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
        print("Please copy config.yaml.example to config.yaml and fill in your settings.")
        sys.exit(1)

    # Validate required sections
    required_sections = ["api", "settings", "cache", "threading", "rate_limiting"]
    for section in required_sections:
        if section not in config:
            print(f"Error: Missing required section '{section}' in config.yaml")
            sys.exit(1)

    return config


def validate_config(config):
    """Validate configuration values and return processed config."""
    errors = []

    # Validate API credentials
    api = config.get("api", {})
    if not api.get("api_key") or api.get("api_key") == "your-api-key-here":
        errors.append("API key not configured in config.yaml")
    if not api.get("user_id") or api.get("user_id") == "your-user-id-here":
        errors.append("User ID not configured in config.yaml")
    if not api.get("username") or api.get("username") == "your-username-here":
        errors.append("Username not configured in config.yaml")
    if not api.get("password") or api.get("password") == "your-password-here":
        errors.append("Password not configured in config.yaml")

    if errors:
        print("Configuration errors:")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)

    return config


# Load configuration
config = load_config()
config = validate_config(config)

# API Credentials
API_KEY = config["api"]["api_key"]
USER_ID = config["api"]["user_id"]
USERNAME = config["api"]["username"]
PASSWORD = config["api"]["password"]

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

# Logging settings
log_to_file = False  # Will be set to True if -logtofile flag is used


# Logging functions
def log_message(message, log_file="log.txt"):
    print(message)
    if log_to_file:
        with open(log_file, "a") as file:
            file.write(message + "\n")


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
    session = requests.Session()
    login_url = "https://gelbooru.com/index.php?page=account&s=login&code=00"
    login_data = {"user": USERNAME, "pass": PASSWORD, "submit": "Log in"}

    try:
        response = session.post(login_url, data=login_data)
        response.raise_for_status()
    except Exception as e:
        log_message(f"Error logging in: {str(e)}")
        return None

    return session


# Functions related to fetching post data
def get_favorite_post_ids(session, pid):
    url = f"https://gelbooru.com/index.php?page=favorites&s=view&id={USER_ID}&pid={pid}"
    try:
        response = session.get(url)
        response.raise_for_status()
    except Exception as e:
        log_message(f"Error getting favorite posts: {str(e)}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    post_spans = soup.find_all("span", class_="thumb")
    post_ids = [span.find("a")["href"].split("=")[-1] for span in post_spans]

    return post_ids


def get_post_details(post_id):
    # Load posts cache
    posts_cache = load_posts_cache()

    # Check if the post is in the cache
    if post_id in posts_cache:
        return "SKIP"

    url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&id={post_id}&json=1&api_key={API_KEY}&user_id={USER_ID}"
    max_retries = 5
    base_delay = 5  # Increased base delay for rate limiting
    failed_posts_cache = load_failed_posts_cache()

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
                return None

        except requests.exceptions.RequestException as e:
            if "Too Many Requests" in str(e):
                handle_rate_limit_response()
                add_rate_limited_post(post_id)  # Track rate-limited post
                log_message(
                    f"Rate limit hit for post {post_id:<8} - Attempt {i + 1}/{max_retries}"
                )

            if i < max_retries - 1:
                delay = base_delay * (2**i)  # Exponential backoff
                log_message(
                    f"Post {post_id:<8}: {str(e)}. Retrying after {delay}s (attempt {i + 1}/{max_retries})"
                )
                countdown_sleep(delay, f"Retry backoff for post {post_id}")
            else:
                log_message(
                    f"Failed to get post {post_id:<8} after {max_retries} attempts: {str(e)}"
                )
                # Save the post ID to the cache when it exceeds max retries
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
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
)


def download_image(url, file_path):
    try:
        response = download_session.get(url, timeout=30)
        response.raise_for_status()
    except Exception as e:
        raise Exception(f"Error downloading image: {str(e)}")

    with open(file_path, "wb") as f:
        f.write(response.content)


def sanitize_for_path(name):
    """
    Sanitizes a string so it can be used as a valid file name or directory name in Windows.
    Replaces invalid characters with an underscore.
    """
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        name = name.replace(char, "_")
    return name


def download_and_save_image(post, character_tags, sensitivity, copyright_tag):
    file_url = post["file_url"]
    file_name = file_url.split("/")[-1]

    base_folder_name, specific_folder_name = get_folder_name(
        character_tags, copyright_tag
    )
    base_folder_name = sanitize_for_path(
        base_folder_name
    )  # Sanitize the base folder name

    if specific_folder_name:
        path = os.path.join(
            BASE_DIR, base_folder_name, specific_folder_name, sensitivity
        )
    else:
        path = os.path.join(BASE_DIR, base_folder_name, sensitivity)

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
            f"Error downloading image {file_name} for post {post['id']:<8}: {str(e)}"
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


def batch_process_posts(post_ids, session):
    """Process multiple posts in parallel"""
    downloaded_count = 0
    retry_count = 0
    rate_limited_count = 0
    failed_count = 0

    # First, fetch all post details in parallel with dynamic worker count
    total_posts = len(post_ids)
    print(c_info("Fetching post details..."))
    with ThreadPoolExecutor(max_workers=current_max_workers) as executor:
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

        for future in as_completed(future_to_post_id):
            post_id = future_to_post_id[future]
            completed_count += 1
            try:
                post_details = future.result()
                if post_details and post_details != "SKIP" and post_details[0]:
                    posts_to_process.append(post_details[0])
                elif post_details == "SKIP":
                    cached_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                if "Too Many Requests" in str(e):
                    rate_limited_count += 1
                else:
                    failed_count += 1

            # Update progress bar with colours
            progress = int((completed_count / total_posts) * 20)
            bar_done = Fore.GREEN + "=" * progress
            bar_remaining = Fore.WHITE + "-" * (20 - progress)
            bar = bar_done + bar_remaining + Style.RESET_ALL

            new_count = c_success(f"new: {len(posts_to_process)}")
            cached_str = c_dim(f"cached: {cached_count}") if cached_count > 0 else ""
            failed_str = c_error(f"failed: {failed_count}") if failed_count > 0 else ""
            status_parts = [s for s in [new_count, cached_str, failed_str] if s]
            status = ", ".join(status_parts)

            print(f"\r  [{bar}] {completed_count}/{total_posts} ({status})  ", end="", flush=True)

        print()  # New line after progress

    if not posts_to_process:
        return 0

    # Collect all unique tags from all posts for batch processing
    print(c_info("Processing tags..."))
    all_tags = set()
    for post in posts_to_process:
        all_tags.update(post["tags"].split())
    print(f"Found {len(all_tags)} unique tags to process...")

    # Batch fetch tag details
    batch_fetch_tag_details(list(all_tags))

    # Process posts with image downloads in parallel
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
        futures = [
            executor.submit(process_post_optimized, post) for post in posts_to_process
        ]

        for future in as_completed(futures):
            try:
                if future.result():  # If download occurred
                    downloaded_count += 1
            except Exception as e:
                failed_count += 1
                log_message(f"Error processing post: {str(e)}")

    # Flush cache updates
    flush_cache_buffers()
    return downloaded_count


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
    total_batches = (total_tags + TAG_BATCH_SIZE - 1) // TAG_BATCH_SIZE
    print(c_info(f"Fetching {total_tags} new tag details..."))
    tags_completed = 0

    for i in range(0, total_tags, TAG_BATCH_SIZE):
        batch = tags_to_fetch[i : i + TAG_BATCH_SIZE]

        with ThreadPoolExecutor(max_workers=min(len(batch), MAX_WORKERS)) as executor:
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
                except Exception as e:
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
    rate_limit_api_call()

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
            response.raise_for_status()

            if response.status_code == 429:
                handle_rate_limit_response()
                raise requests.exceptions.RequestException("Too Many Requests")

            data = json.loads(response.text)
            if data and "tag" in data and data["tag"]:
                reset_adaptive_delay()
                return data["tag"][0]
            else:
                reset_adaptive_delay()
                return None

        except requests.exceptions.RequestException as e:
            if "Too Many Requests" in str(e):
                handle_rate_limit_response()

            if i < max_retries - 1:
                delay = base_delay * (2**i)
                time.sleep(delay)
            else:
                return None

    return None


def process_post_optimized(post):
    """Optimized post processing with buffered cache updates"""
    post_id = post["id"]

    # Safety check in case this function is called directly
    # Normally cached posts are filtered out earlier in batch_process_posts
    posts_cache = load_posts_cache()
    with cache_update_lock:
        if post_id in posts_cache or post_id in pending_posts_cache:
            log_message(f"Post {post_id:<8} found in cache during processing, skipping")
            return False

    # Check if file already exists
    file_url = post["file_url"]
    file_name = file_url.split("/")[-1]
    sensitivity = get_sensitivity(post)

    character_tags = get_character_tags_optimized(post["tags"])
    copyright_tag = get_copyright_tag_optimized(post["tags"])

    base_folder_name, specific_folder_name = get_folder_name(
        character_tags, copyright_tag
    )
    base_folder_name = sanitize_for_path(base_folder_name)

    if specific_folder_name:
        path = os.path.join(
            BASE_DIR, base_folder_name, specific_folder_name, sensitivity
        )
    else:
        path = os.path.join(BASE_DIR, base_folder_name, sensitivity)

    file_path = os.path.join(path, file_name)

    download_occurred = False

    if not os.path.exists(file_path):
        try:
            if not os.path.exists(path):
                os.makedirs(path)
            download_image(file_url, file_path)
            download_occurred = True
            # Format download message with colour
            print(f"  {c_success('+')} {c_dim(file_name[:45])} {c_dim('post')} {post_id}")
            # Only add to cache if download succeeded
            with cache_update_lock:
                pending_posts_cache[post_id] = True
        except Exception as e:
            print(f"  {c_error('x')} {c_error('Failed:')} {file_name[:30]} - {str(e)[:30]}")
            # Track download failures so they can be retried later
            failed_cache = load_failed_posts_cache()
            failed_cache[str(post_id)] = {"error": str(e)[:100], "type": "download"}
            save_failed_posts_cache(failed_cache)
    else:
        # File already exists, safe to cache
        with cache_update_lock:
            pending_posts_cache[post_id] = True

    return download_occurred


def get_character_tags_optimized(tags):
    """Optimized character tag retrieval using cached data"""
    character_tags = []
    cache = load_cache()

    for tag in tags.split():
        # Check both main cache and pending cache
        tag_details = cache.get(tag)
        if not tag_details:
            with cache_update_lock:
                tag_details = pending_tag_cache.get(tag)

        if tag_details and "type" in tag_details and int(tag_details["type"]) == 4:
            character_tags.append(tag_details["name"])

    return character_tags


def get_copyright_tag_optimized(tags):
    """Optimized copyright tag retrieval using cached data"""
    cache = load_cache()

    for tag in tags.split():
        # Check both main cache and pending cache
        tag_details = cache.get(tag)
        if not tag_details:
            with cache_update_lock:
                tag_details = pending_tag_cache.get(tag)

        if tag_details and "type" in tag_details and int(tag_details["type"]) == 3:
            return tag_details["name"]

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


def save_rate_limited_posts():
    """Save the current set of rate-limited posts to disk"""
    with rate_limited_lock:
        _save_rate_limited_posts_unlocked()


def add_rate_limited_post(post_id):
    """Add a post to the rate-limited tracking set"""
    with rate_limited_lock:
        rate_limited_posts.add(post_id)
        _save_rate_limited_posts_unlocked()


def remove_rate_limited_post(post_id):
    """Remove a post from the rate-limited tracking set"""
    with rate_limited_lock:
        if post_id in rate_limited_posts:
            rate_limited_posts.remove(post_id)
            _save_rate_limited_posts_unlocked()


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


def rate_limit_api_call():
    """Ensure we don't make API calls too frequently"""
    global last_api_call_time, adaptive_delay

    with api_call_lock:
        current_time = time.time()
        # Calculate the earliest time we're allowed to make a call
        earliest_allowed = last_api_call_time + adaptive_delay

        if current_time >= earliest_allowed:
            # We can call immediately, no waiting needed
            sleep_time = 0
            last_api_call_time = current_time
        else:
            # We need to wait until our reserved slot
            sleep_time = earliest_allowed - current_time
            # Reserve this slot for ourselves
            last_api_call_time = earliest_allowed

    # Sleep OUTSIDE the lock so other threads aren't blocked
    if sleep_time > 0:
        if sleep_time >= 2:
            # Only show countdown for longer waits (2+ seconds)
            countdown_sleep(sleep_time, "Rate limiting", show_done=False)
        else:
            time.sleep(sleep_time)


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

        print(c_warning(f"\n! Rate limited - backing off ({adaptive_delay:.1f}s delay)"), flush=True)

        # Force a longer pause after rate limit
        sleep_time = adaptive_delay * 2

    # Countdown outside the lock so other threads aren't blocked
    countdown_sleep(sleep_time, c_warning("Rate limit cooldown"))


def reset_adaptive_delay():
    """Reset adaptive delay to normal when requests are successful"""
    global adaptive_delay, successful_requests
    with api_call_lock:
        successful_requests += 1

        if successful_requests >= SUCCESS_THRESHOLD and adaptive_delay > MIN_DELAY:
            adaptive_delay = max(adaptive_delay * DELAY_DECREASE_FACTOR, MIN_DELAY)
            successful_requests = 0  # Reset counter after adjustment


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
        print(c_error(f"Warning: Error saving caches: {str(e)}"))

    print(c_info("Goodbye!"))
    sys.stdout.flush()
    # Use os._exit() to forcefully terminate all threads immediately
    os._exit(0)


def retry_failed_posts(session):
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
    for post_id in failed_post_ids:
        rate_limit_api_call()
        post_details = get_post_details(post_id)
        if post_details and post_details != "SKIP" and post_details[0]:
            posts_to_retry.append((post_id, post_details[0]))

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

        # Get tags for this post using optimized functions
        character_tags = get_character_tags_optimized(post["tags"])
        copyright_tag = get_copyright_tag_optimized(post["tags"])
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

    # Handle posts that were skipped or couldn't be fetched
    for post_id in failed_post_ids:
        if not any(pid == post_id for pid, _ in posts_to_retry):
            still_failed += 1

    print()  # New line after progress

    if success_count > 0:
        print(c_success(f"\nRecovered {success_count} posts"))
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
    args = parser.parse_args()

    global log_to_file, rate_limited_posts
    log_to_file = args.logtofile

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

    session = login()
    if session is None:
        print(c_error("Failed to log in. Exiting."))
        return

    # Handle --retry-failed mode
    if args.retry_failed:
        retry_failed_posts(session)
        return

    # Load posts cache
    posts_cache = load_posts_cache()

    pid = 0
    consecutive_empty_pages = (
        0  # Counter for consecutive pages without downloaded images
    )

    while consecutive_empty_pages < MAX_CONSECUTIVE_EMPTY_PAGES:
        post_ids = get_favorite_post_ids(session, pid)
        if post_ids is None or not post_ids:
            print(c_info("No more favourite posts found."))
            break

        page_num = (pid // POSTS_PER_PAGE) + 1
        print(c_header(f"\n{'='*60}"))
        print(c_header(f"  Page {page_num} - {len(post_ids)} favourite posts"))
        print(c_header(f"{'='*60}"))

        # Use optimised batch processing
        start_time = time.time()
        downloaded_count = batch_process_posts(post_ids, session)
        end_time = time.time()

        elapsed = end_time - start_time
        if downloaded_count > 0:
            print(c_success(f"Downloaded {downloaded_count} new images") + c_dim(f" in {elapsed:.1f}s"))
        else:
            print(c_dim(f"No new images (all cached) - {elapsed:.1f}s"))
        downloaded_images = downloaded_count > 0

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

    print(c_success("\n" + "="*60))
    print(c_success("  Complete! All progress saved."))
    print(c_success("="*60))


if __name__ == "__main__":
    main()
