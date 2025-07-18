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
from bs4 import BeautifulSoup

API_KEY = "your-api-key-here"
USER_ID = "your-user-id-here"
USERNAME = "your-username-here"
PASSWORD = "your-password-here"
POSTS_PER_PAGE = 50
MAX_CONSECUTIVE_EMPTY_PAGES = 10
CACHE_FILE = "tag_cache.json"
POSTS_CACHE_FILE = "posts_cache.json"
FAILED_POSTS_CACHE_FILE = "failed_posts_cache.json"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Threading and performance settings - Adjust these based on your system and rate limits
MAX_WORKERS = 3          # Reduced for better rate limit compliance
DOWNLOAD_WORKERS = 2     # Reduced concurrent downloads
TAG_BATCH_SIZE = 10      # Smaller batches to avoid rate limits
ENABLE_PERFORMANCE_MODE = True  # Set to False to use original sequential processing

file_lock = threading.Lock()

# Rate limiting settings
API_DELAY = 0.1  # Delay between API calls in seconds (10 requests per second limit)
last_api_call_time = 0
api_call_lock = threading.Lock()
adaptive_delay = API_DELAY

# Cache buffers for batch operations
pending_posts_cache = {}
pending_tag_cache = {}
cache_update_lock = threading.Lock()

# Logging functions
def log_message(message, log_file="log.txt"):
    print(message)
    if log_to_file:
        with open(log_file, "a") as file:
            file.write(message + "\\n")


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
        log_message(f"Post {post_id:<8} is already in the cache. Skipping API request.")
        return "SKIP"

    url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&id={post_id}&json=1&api_key={API_KEY}&user_id={USER_ID}"
    max_retries = 5
    base_delay = 5  # Increased base delay for rate limiting
    failed_posts_cache = load_failed_posts_cache()

    for i in range(max_retries):
        try:
            response = requests.get(url)
            response.raise_for_status()

            if response.status_code == 429:
                handle_rate_limit_response()
                raise requests.exceptions.RequestException("Too Many Requests")

            data = json.loads(response.text)
            if "post" in data:
                post = data["post"]
                reset_adaptive_delay()  # Success, so we can reduce delay if it was increased
                return post if isinstance(post, list) else [post]
            else:
                reset_adaptive_delay()  # Success, so we can reduce delay if it was increased
                return None

        except requests.exceptions.RequestException as e:
            if "Too Many Requests" in str(e):
                handle_rate_limit_response()

            if i < max_retries - 1:
                delay = base_delay * (2 ** i)  # Exponential backoff
                log_message(
                    f"Encountered error: {str(e)}. Retrying after {delay} seconds (attempt {i + 1}/{max_retries})"
                )
                time.sleep(delay)
            else:
                log_message(f"Error getting post details for post {post_id:<8}: {str(e)}")
                # Save the post ID to the cache when it exceeds max retries
                failed_posts_cache[post_id] = True
                save_failed_posts_cache(failed_posts_cache)
                return None


# Functions related to downloading and saving images
def create_directories():
    sensitivities = ["General", "Sensitive", "Questionable", "Explicit"]
    for sensitivity in sensitivities:
        os.makedirs(f"Multiple/{sensitivity}", exist_ok=True)


# Global session for connection pooling
download_session = requests.Session()
download_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
})

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
        return

    try:
        download_image(file_url, file_path)
    except Exception as e:
        log_message(
            f"Error downloading image {file_name} for post {post['id']:<8}: {str(e)}"
        )


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

    # First, fetch all post details in parallel
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all post detail fetching tasks with staggered delays
        future_to_post_id = {}
        for i, post_id in enumerate(post_ids):
            # Add small staggered delay to prevent simultaneous API hits
            if i > 0:
                time.sleep(0.05)  # 50ms delay between task submissions
            future_to_post_id[executor.submit(get_post_details, post_id)] = post_id

        posts_to_process = []

        for future in as_completed(future_to_post_id):
            post_id = future_to_post_id[future]
            try:
                post_details = future.result()
                if post_details and post_details != "SKIP" and post_details[0]:
                    posts_to_process.append(post_details[0])
            except Exception as e:
                log_message(f"Error fetching details for post {post_id}: {str(e)}")

    if not posts_to_process:
        return 0

    # Collect all unique tags from all posts for batch processing
    all_tags = set()
    for post in posts_to_process:
        all_tags.update(post["tags"].split())

    # Batch fetch tag details
    batch_fetch_tag_details(list(all_tags))

    # Process posts with image downloads in parallel
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
        futures = [executor.submit(process_post_optimized, post) for post in posts_to_process]

        for future in as_completed(futures):
            try:
                if future.result():  # If download occurred
                    downloaded_count += 1
            except Exception as e:
                log_message(f"Error processing post: {str(e)}")

    # Flush cache updates
    flush_cache_buffers()

    return downloaded_count


def batch_fetch_tag_details(tags):
    """Fetch tag details in parallel batches"""
    cache = load_cache()
    tags_to_fetch = [tag for tag in tags if tag not in cache and tag not in pending_tag_cache]

    if not tags_to_fetch:
        return

    # Process tags in batches to avoid overwhelming the API
    for i in range(0, len(tags_to_fetch), TAG_BATCH_SIZE):
        batch = tags_to_fetch[i:i + TAG_BATCH_SIZE]

        with ThreadPoolExecutor(max_workers=min(len(batch), MAX_WORKERS)) as executor:
            future_to_tag = {
                executor.submit(get_tag_details_single, tag): tag
                for tag in batch
            }

            for future in as_completed(future_to_tag):
                tag = future_to_tag[future]
                try:
                    tag_details = future.result()
                    if tag_details:
                        with cache_update_lock:
                            pending_tag_cache[tag] = tag_details
                except Exception as e:
                    log_message(f"Error fetching tag details for {tag}: {str(e)}")

        # Small delay between batches to respect rate limits
        time.sleep(0.5)  # Increased delay between batches


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
                delay = base_delay * (2 ** i)
                time.sleep(delay)
            else:
                return None
        else:
            break

    return None


def process_post_optimized(post):
    """Optimized post processing with buffered cache updates"""
    post_id = post["id"]

    # Check cache first (including pending cache)
    posts_cache = load_posts_cache()
    with cache_update_lock:
        if post_id in posts_cache or post_id in pending_posts_cache:
            return False

    # Check if file already exists
    file_url = post["file_url"]
    file_name = file_url.split("/")[-1]
    sensitivity = get_sensitivity(post)

    character_tags = get_character_tags_optimized(post["tags"])
    copyright_tag = get_copyright_tag_optimized(post["tags"])

    base_folder_name, specific_folder_name = get_folder_name(character_tags, copyright_tag)
    base_folder_name = sanitize_for_path(base_folder_name)

    if specific_folder_name:
        path = os.path.join(BASE_DIR, base_folder_name, specific_folder_name, sensitivity)
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
            log_message(f"Downloaded: {file_name} for post {post_id:<8}")
        except Exception as e:
            log_message(f"Error downloading {file_name} for post {post_id:<8}: {str(e)}")

    # Buffer cache update instead of immediate write
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


# Functions related to tag details
def get_tag_details(tag):
    # Load cache
    cache = load_cache()

    # Check if tag is in cache
    if tag in cache:
        return cache[tag]

    # Rate limit API calls
    rate_limit_api_call()

    tag_details = None  # assign a default value

    # Fetch tag details from API
    modified_tag = (
        tag.replace("&#039;", "'")
        .replace("&gt;", ">")
        .replace("&lt;", "<")
        .replace("&quot;", '"')
        .replace("&amp;", "&")
    )
    encoded_tag = quote(modified_tag)
    url = f"https://gelbooru.com/index.php?page=dapi&s=tag&q=index&json=1&name={encoded_tag}&api_key={API_KEY}&user_id={USER_ID}"
    max_retries = 5
    base_delay = 5  # Increased base delay for rate limiting

    for i in range(max_retries):
        try:
            response = requests.get(url)
            response.raise_for_status()

            if response.status_code == 429:
                handle_rate_limit_response()
                raise requests.exceptions.RequestException("Too Many Requests")

            data = json.loads(response.text)
            if data:
                try:
                    tag_details = data["tag"][0]
                    reset_adaptive_delay()  # Success, so we can reduce delay if it was increased
                except KeyError:
                    reset_adaptive_delay()  # Success, so we can reduce delay if it was increased
                    return None
            else:
                reset_adaptive_delay()  # Success, so we can reduce delay if it was increased
                return None

        except requests.exceptions.RequestException as e:
            if "Too Many Requests" in str(e):
                handle_rate_limit_response()

            if i < max_retries - 1:
                delay = base_delay * (2 ** i)  # Exponential backoff
                time.sleep(delay)
            else:
                return None
        else:
            break

    # Save tag details to cache
    if tag_details is not None:
        cache[tag] = tag_details
        save_cache(cache)

    return tag_details


def get_character_tags(tags):
    character_tags = []
    for tag in tags.split():
        tag_details = get_tag_details(tag)
        if tag_details and "type" in tag_details and int(tag_details["type"]) == 4:
            character_tags.append(tag_details["name"])
    return character_tags


def get_copyright_tag(tags):
    for tag in tags.split():
        tag_details = get_tag_details(tag)
        if tag_details and "type" in tag_details and int(tag_details["type"]) == 3:
            return tag_details["name"]
    return None


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


def process_post(post):
    post_id = post["id"]
    log_message(f"Processing post {post_id:<8}")

    # Load posts cache
    posts_cache = load_posts_cache()

    # Check if the post has been processed already
    if post_id in posts_cache:
        log_message(f"Skipping post {post_id:<8} as it has already been processed")
        return False  # Indicate no download occurred

    character_tags = get_character_tags(post["tags"])
    copyright_tag = get_copyright_tag(post["tags"])
    log_message(f"Character tags: {character_tags}")

    # Check if the image file exists on disk before calling download_and_save_image
    file_url = post["file_url"]
    file_name = file_url.split("/")[-1]
    sensitivity = get_sensitivity(post)

    base_folder_name, specific_folder_name = get_folder_name(
        character_tags, copyright_tag
    )
    base_folder_name = sanitize_for_path(
        base_folder_name
    )  # Sanitize the base folder name

    # Construct the path based on whether there is a specific folder name
    if specific_folder_name:
        path = os.path.join(
            BASE_DIR, base_folder_name, specific_folder_name, sensitivity
        )
    else:
        path = os.path.join(BASE_DIR, base_folder_name, sensitivity)

    file_path = os.path.join(path, file_name)

    if os.path.exists(file_path):
        log_message(
            f"Skipping download of image {file_name} for post {post['id']:<8} because it already exists"
        )
        posts_cache[post_id] = True
        save_posts_cache(posts_cache)
        return False  # Indicate no download occurred
    else:
        download_and_save_image(post, character_tags, sensitivity, copyright_tag)

    # Update posts cache (This should be done whether the file was newly downloaded or already existed)
    posts_cache[post_id] = True
    save_posts_cache(posts_cache)

    return not os.path.exists(
        file_path
    )  # Indicate a download occurred only if the file was not previously existing


def rate_limit_api_call():
    """Ensure we don't make API calls too frequently"""
    global last_api_call_time, adaptive_delay
    with api_call_lock:
        current_time = time.time()
        time_since_last_call = current_time - last_api_call_time
        if time_since_last_call < adaptive_delay:
            sleep_time = adaptive_delay - time_since_last_call
            if sleep_time > 0.2:  # Only log if we're waiting more than 200ms
                log_message(f"Rate limiting: waiting {sleep_time:.2f} seconds before next API call")
            time.sleep(sleep_time)
        last_api_call_time = time.time()


def handle_rate_limit_response():
    """Increase adaptive delay when we get rate limited"""
    global adaptive_delay
    with api_call_lock:
        adaptive_delay = min(adaptive_delay * 2, 5.0)  # Double delay, max 5 seconds
        log_message(f"Rate limited! Increasing adaptive delay to {adaptive_delay:.2f} seconds")


def reset_adaptive_delay():
    """Reset adaptive delay to normal when requests are successful"""
    global adaptive_delay
    with api_call_lock:
        if adaptive_delay > API_DELAY:
            adaptive_delay = max(adaptive_delay * 0.9, API_DELAY)  # Gradually reduce delay


def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully by saving caches before exiting"""
    log_message("\n\nReceived interrupt signal (Ctrl+C). Saving progress and exiting gracefully...")

    # Save any cached data
    try:
        log_message("Saving caches before exit...")
        # The caches are already saved after each operation, but we'll make sure
        # any pending operations are completed by acquiring the file lock briefly
        with file_lock:
            log_message("Cache save completed.")
    except Exception as e:
        log_message(f"Warning: Error while saving caches during exit: {str(e)}")

    log_message("Graceful exit completed. Goodbye!")
    sys.exit(0)


# Main function
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-logtofile", help="log output to file", action="store_true")
    args = parser.parse_args()

    global log_to_file
    log_to_file = args.logtofile

    # Register signal handler for graceful exit
    signal.signal(signal.SIGINT, signal_handler)
    log_message("Press Ctrl+C to gracefully exit the program.")

    session = login()
    if session is None:
        log_message("Failed to log in. Exiting.")
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
            print(f"No more favorite posts found at pid={pid}")
            break

        print(f"Page with pid={pid}: {len(post_ids)} favorite posts")

        if ENABLE_PERFORMANCE_MODE:
            # Use optimized batch processing
            start_time = time.time()
            downloaded_count = batch_process_posts(post_ids, session)
            end_time = time.time()

            print(f"Processed {len(post_ids)} posts in {end_time - start_time:.2f} seconds")
            print(f"Downloaded {downloaded_count} new images")
            downloaded_images = downloaded_count > 0
        else:
            # Original sequential processing (fallback)
            downloaded_images = False
            for post_id in post_ids:
                rate_limit_api_call()
                post_details = get_post_details(post_id)
                if (
                    post_details == "SKIP"
                    or post_details is None
                    or not post_details
                    or post_details[0] is None
                ):
                    continue

                if process_post(post_details[0]):
                    downloaded_images = True

        if not downloaded_images:
            consecutive_empty_pages += 1
        else:
            consecutive_empty_pages = 0

        if len(post_ids) < POSTS_PER_PAGE:
            print("Reached the last page of favorite posts.")
            break

        pid += POSTS_PER_PAGE

    if consecutive_empty_pages >= MAX_CONSECUTIVE_EMPTY_PAGES:
        print(
            f"No images downloaded for {MAX_CONSECUTIVE_EMPTY_PAGES} consecutive pages. Ending the script."
        )

    # Final cleanup - flush any remaining cache updates
    flush_cache_buffers()
    print("Script completed. All cache updates saved.")


if __name__ == "__main__":
    main()
