# Gelbooru Favorite Downloader

This Python script downloads your favorite images from Gelbooru to your local machine. It logs into your Gelbooru account, fetches your favorite posts, and downloads the images. Each image is saved in a folder named after the character(s) associated with that image and the sensitivity level defined on Gelbooru.

## Prerequisites

You need the following Python packages to run this script:
* requests
* beautifulsoup4

You can install these packages using pip:
```sh
pip install requests beautifulsoup4
```

## Usage

1. Download the script and save it to your local machine.
2. Open the script in a text editor.
3. Replace the following placeholders with your own information:
   * `API_KEY`: Your Gelbooru API key. You can find your API key in your Gelbooru account settings.
   * `USER_ID`: Your Gelbooru user ID. You can find your user ID in your Gelbooru account settings.
   * `USERNAME`: Your Gelbooru username.
   * `PASSWORD`: Your Gelbooru password.
4. Save the changes to the script.
5. Open a command prompt or terminal window and navigate to the directory where you saved the script.
6. Run the script using the following command:
   ```sh
   python gelbooru_favorite_downloader.py
   ```
7. The script will log in to your Gelbooru account, fetch your favorite posts, and download the images. The images will be saved in folders named after the character(s) associated with each image and the sensitivity level defined on Gelbooru.

## Customization

You can customize the script by changing the following variables:

* `POSTS_PER_PAGE`: The number of favorite posts to fetch per page. The default value is 50.
* `MAX_PAGES`: The maximum number of pages of favorite posts to fetch. The default value is 5.
