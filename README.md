# Gelbooru Favorites Downloader

A Python script to download your Gelbooru favorite images and organize them into character and sensitivity rating folders.

## Features

- Downloads favorite images from Gelbooru
- Organizes images into folders based on character tags and sensitivity ratings (General, Sensitive, Questionable, Explicit)
- Handles rate limits and retries
- Resumes downloading from the last downloaded image

## Requirements

- Python 3.6 or later
- Beautiful Soup 4
- Requests

## Installation

1. Clone this repository or download the script file.
2. Install the required packages using pip:

```bash
pip install beautifulsoup4 requests
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

The script will download your favorite images and organize them into folders based on the character tags and sensitivity ratings.

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

## License

[MIT](https://choosealicense.com/licenses/mit/)
