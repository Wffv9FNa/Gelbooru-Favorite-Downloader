import os
import requests
from bs4 import BeautifulSoup
import json
import time
import json
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor

API_KEY = 'your-api-key-here'
USER_ID = 'your-user-id-here'
USERNAME = "your-username-here"
PASSWORD = "your-password-here"
POSTS_PER_PAGE = 50
CACHE_FILE = "tag_cache.json"
POSTS_CACHE_FILE = "posts_cache.json"

def log_message(message, log_file="log.txt"):
    print(message)
    with open(log_file, 'a') as file:
        file.write(message + "\n")

def login():
    session = requests.Session()
    login_url = 'https://gelbooru.com/index.php?page=account&s=login&code=00'
    login_data = {'user': USERNAME, 'pass': PASSWORD, 'submit': 'Log in'}

    try:
        response = session.post(login_url, data=login_data)
        response.raise_for_status()
    except Exception as e:
        log_message(f"Error logging in: {str(e)}")
        return None

    return session

def get_favorite_post_ids(session, pid):
    url = f"https://gelbooru.com/index.php?page=favorites&s=view&id={USER_ID}&pid={pid}"
    try:
        response = session.get(url)
        response.raise_for_status()
    except Exception as e:
        log_message(f"Error getting favorite posts: {str(e)}")
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    post_spans = soup.find_all('span', class_='thumb')
    post_ids = [span.find('a')['href'].split('=')[-1] for span in post_spans]

    return post_ids

def get_post_details(post_id):
    url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&id={post_id}&json=1&api_key={API_KEY}"

    max_retries = 5
    base_delay = 2

    for i in range(max_retries):
        try:
            response = requests.get(url)
            response.raise_for_status()

            if response.status_code == 429:
                raise requests.exceptions.RequestException("Too Many Requests")

            data = json.loads(response.text)
            if 'post' in data:
                post = data['post']
                return post if isinstance(post, list) else [post]
            else:
                return None

        except requests.exceptions.RequestException as e:
            if i < max_retries - 1:
                delay = base_delay * (i + 1)
                log_message(f"Encountered error: {str(e)}. Retrying after {delay} seconds (attempt {i + 1}/{max_retries})")
                time.sleep(delay)
            else:
                log_message(f"Error getting post details for post {post_id}: {str(e)}")
                return None


def download_and_save_image(post, character_tags, sensitivity):
    file_url = post['file_url']
    file_name = file_url.split('/')[-1]

    if not character_tags:
        folder_name = 'No Character'
    elif len(character_tags) == 1:
        folder_name = character_tags[0]
    else:
        folder_name = 'Multiple'

    path = f"{folder_name}/{sensitivity}"
    if not os.path.exists(path):
        os.makedirs(path)

    if folder_name != 'No Character' and folder_name != 'Multiple':
        for character in character_tags:
            char_path = f"{character}/{sensitivity}"
            if not os.path.exists(char_path):
                os.makedirs(char_path)

    file_path = os.path.join(path, file_name)

    if os.path.exists(file_path):
        print(f"Skipping download of image {file_name} for post {post['id']} because it already exists")
        return

    try:
        download_image(file_url, file_path)
    except Exception as e:
        log_message(f"Error downloading image {file_name} for post {post['id']}: {str(e)}")


def download_image(url, file_path):
    try:
        response = requests.get(url)
        response.raise_for_status()
    except Exception as e:
        raise Exception(f"Error downloading image: {str(e)}")

    with open(file_path, 'wb') as f:
        f.write(response.content)


def create_directories():
    sensitivities = ['General', 'Sensitive', 'Questionable', 'Explicit']
    for sensitivity in sensitivities:
        os.makedirs(f"Multiple/{sensitivity}", exist_ok=True)


def load_cache():
    try:
        with open(CACHE_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f)


def get_tag_details(tag):
    # Load cache
    cache = load_cache()

    # Check if tag is in cache
    if tag in cache:
        return cache[tag]

    tag_details = None  # assign a default value

    # Fetch tag details from API
    encoded_tag = quote(tag)
    url = f"https://gelbooru.com/index.php?page=dapi&s=tag&q=index&json=1&name={encoded_tag}"
    max_retries = 5
    base_delay = 2

    for i in range(max_retries):
        try:
            response = requests.get(url)
            response.raise_for_status()

            if response.status_code == 429:
                raise requests.exceptions.RequestException("Too Many Requests")

            data = json.loads(response.text)
            if data:
                try:
                    tag_details = data['tag'][0]
                except KeyError:
                    log_message(f"Error: Could not find tag details for '{tag}'. Skipping this tag.")
                    return None
            else:
                return None

        except requests.exceptions.RequestException as e:
            if i < max_retries - 1:
                delay = base_delay * (i + 1)
                log_message(f"Encountered error: {str(e)}. Retrying after {delay} seconds (attempt {i + 1}/{max_retries})")
                time.sleep(delay)
            else:
                log_message(f"Error getting tag details for {tag}: {str(e)}")
                return None
        else:
            print(f"Successfully processed tag '{tag}' after {i} retries.")
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
        if tag_details and 'type' in tag_details and int(tag_details['type']) == 4:
            character_tags.append(tag_details['name'])

    return character_tags


def get_sensitivity(post):
    rating = post.get('rating')
    if rating == 'sensitive':
        return 'Sensitive'
    elif rating == 'questionable':
        return 'Questionable'
    elif rating == 'explicit':
        return 'Explicit'
    else:
        return 'General'


def load_posts_cache():
    try:
        with open(POSTS_CACHE_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_posts_cache(cache):
    with open(POSTS_CACHE_FILE, 'w') as f:
        json.dump(cache, f)


def process_post(post):
    post_id = post['id']
    print(f'Processing post {post_id}')

    # Load posts cache
    posts_cache = load_posts_cache()

    # Check if the post has been processed already
    if post_id in posts_cache:
        print(f'Skipping post {post_id} as it has already been processed')
        return

    character_tags = get_character_tags(post['tags'])
    print(f'Character tags: {character_tags}')

    # Check if the image file exists on disk before calling download_and_save_image
    file_url = post['file_url']
    file_name = file_url.split('/')[-1]
    sensitivity = get_sensitivity(post)
    folder_name = get_folder_name(character_tags)
    file_path = os.path.join(folder_name, sensitivity, file_name)

    if os.path.exists(file_path):
        print(f"Skipping download of image {file_name} for post {post['id']} because it already exists")
    else:
        download_and_save_image(post, character_tags, sensitivity)

    # Update posts cache
    posts_cache[post_id] = True
    save_posts_cache(posts_cache)


# Helper function to get the folder name based on character_tags
def get_folder_name(character_tags):
    if not character_tags:
        return 'No Character'
    elif len(character_tags) == 1:
        return character_tags[0]
    else:
        return 'Multiple'


def main():
    session = login()
    if session is None:
        log_message("Failed to log in. Exiting.")
        return

    # Load posts cache
    posts_cache = load_posts_cache()

    pid = 0
    while True:
        post_ids = get_favorite_post_ids(session, pid)
        if post_ids is None or not post_ids:
            print(f"No more favorite posts found at pid={pid}")
            break

        print(f"Page with pid={pid}: {len(post_ids)} favorite posts")

        with ThreadPoolExecutor() as executor:
            post_details_list = list(executor.map(get_post_details, post_ids))

        for post_details in post_details_list:
            if post_details is None or not post_details or post_details[0] is None:
                log_message("Post details not found")
                continue

            post_id = post_details[0]['id']
            if str(post_id) in posts_cache:
                print(f"Skipping post {post_id} as it has already been processed")
            else:
                process_post(post_details[0])
                # Update posts cache
                posts_cache[str(post_id)] = True

        save_posts_cache(posts_cache)

        if len(post_ids) < POSTS_PER_PAGE:
            break

        pid += POSTS_PER_PAGE


if __name__ == '__main__':
    main()