# Gelbooru Favorite Downloader

This script allows you to download your favorite posts from [Gelbooru](https://gelbooru.com/) using their [API](https://gelbooru.com/index.php?page=wiki&s=view&id=18780). The script logs in to your Gelbooru account and then retrieves your favorite posts. For each post, it downloads the associated image and saves it to a directory named after the character tags and sensitivity rating of the post. The script also creates directories for posts with multiple characters and posts with no character tags.

## Installation

1. Clone this repository: `git clone https://github.com/Wffv9FNa/Gelbooru-Favorite-Downloader/`
2. Install the required packages: `pip install -r requirements.txt`

## Usage

1. Set your Gelbooru credentials in the `USERNAME` and `PASSWORD` variables in `gelbooru_favorite_downloader.py`.
2. Run the script: `python gelbooru_favorite_downloader.py`

## Configuration

The following variables can be adjusted in the script to customize its behavior:

- `API_KEY`: The API key to use for accessing Gelbooru's API.
- `USER_ID`: The ID of the Gelbooru user whose favorite posts to download.
- `POSTS_PER_PAGE`: The number of favorite posts to retrieve per page.
- `MAX_PAGES`: The maximum number of pages of favorite posts to retrieve.

## Disclaimer

Please use this script responsibly and in accordance with Gelbooru's terms of service. The author of this script is not responsible for any misuse or violation of Gelbooru's policies.
