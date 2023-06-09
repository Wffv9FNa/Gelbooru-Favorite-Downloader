# Gelbooru Favorites Downloader

A Python script to download your Gelbooru favorite images and organize them into character and sensitivity rating folders.

## Features

- Downloads favorite images from Gelbooru
- Organizes images into folders based on character tags and sensitivity ratings (General, Sensitive, Questionable, Explicit)
- Handles rate limits and retries
- Resumes downloading from the last downloaded image
- Utilizes cache to avoid reprocessing posts and re-downloading images
- Option to log output to a file

## Requirements

- Python 3.6 or later
- Beautiful Soup 4
- Requests
- argparse

## Installation

1. Clone this repository or download the script file.
2. Install the required packages using pip:

```bash
pip install beautifulsoup4 requests argparse
```

3. Replace the placeholder values in the script with your Gelbooru API key, user ID, username, and password:

```python
API_KEY = 'your-api-key-here'
USER_ID = 'your-user-id-here'
USERNAME = "your-username-here"
PASSWORD = "your-password-here"
```

## Usage

Run the script in your terminal:

```bash
python gelbooru_favorites_downloader.py
```

To log the output messages to a file, add the `-logtofile` flag:

```bash
python gelbooru_favorites_downloader.py -logtofile
```

The script will download your favorite images and organize them into folders based on the character tags and sensitivity ratings. The script first checks a local cache of processed posts, and if a post has been processed before, it won't request the post details from the API, saving on API calls.

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

## License

[MIT](https://choosealicense.com/licenses/mit/)
