# Gelbooru Favorites Downloader

A Python script to download your Gelbooru favorite images and organize them into character and sensitivity rating folders with parallel processing and intelligent caching.

## Features

- Downloads favorite images from Gelbooru using the API
- Organizes images into folders based on character tags and sensitivity ratings (General, Sensitive, Questionable, Explicit)
- **Parallel batch processing** for fast downloads
- **Adaptive rate limiting** to avoid API limits
- **Color-coded terminal output** for better visibility
- **Smart caching** to avoid reprocessing posts and re-downloading images
- **Failed post tracking** with retry capability
- **Configuration file** for easy customization
- **Graceful shutdown** (Ctrl+C) with progress saving
- Optional file logging

## Requirements

- Python 3.6 or later
- Required packages:
  - beautifulsoup4
  - requests
  - pyyaml
  - colorama

## Installation

1. Clone this repository or download the script files:
```bash
git clone <repository-url>
cd Gelbooru-Favorite-Downloader
```

2. Install the required packages:
```bash
pip install -r requirements.txt
```

Or manually:
```bash
pip install beautifulsoup4 requests pyyaml colorama
```

3. Configure your credentials:
```bash
cp config.yaml.example config.yaml
```

4. Edit `config.yaml` and add your Gelbooru credentials:
   - **API Key** and **User ID**: Get these from Gelbooru → My Account → Options → API Access Credentials
   - **Username** and **Password**: Your Gelbooru login credentials

## Configuration

The `config.yaml` file contains all settings:

### API Credentials
```yaml
api:
  api_key: "your-api-key-here"
  user_id: "your-user-id-here"
  username: "your-username-here"
  password: "your-password-here"
```

### General Settings
- `posts_per_page`: Number of posts to fetch per page (default: 50)
- `max_consecutive_empty_pages`: Stop after this many pages with no new downloads (default: 10)
- `base_dir`: Base directory for downloads (leave empty to use script directory)

### Threading & Performance
- `max_workers`: Parallel API request threads (default: 4)
- `download_workers`: Parallel download threads (default: 3)
- `tag_batch_size`: Tags to process per batch (default: 20)

### Rate Limiting
- `min_delay`: Minimum delay between API calls in seconds (default: 0.25)
- `max_delay`: Maximum delay between API calls in seconds (default: 5.0)
- `delay_increase_factor`: Multiply delay by this when rate limited (default: 1.5)
- `delay_decrease_factor`: Multiply delay by this after successes (default: 0.95)
- `success_threshold`: Successful requests before reducing delay (default: 15)

See `config.yaml.example` for the complete configuration template.

## Usage

### Normal Operation
Download all favorite images:
```bash
python gelbooru_favorite_downloader.py
```

### With File Logging
Save output to a log file:
```bash
python gelbooru_favorite_downloader.py -logtofile
```

### Retry Failed Downloads
Retry posts that previously failed:
```bash
python gelbooru_favorite_downloader.py --retry-failed
```

### List Failed Posts
Display all failed posts without retrying:
```bash
python gelbooru_favorite_downloader.py --list-failed
```

## How It Works

1. **Login** to Gelbooru with your credentials
2. **Fetch favorites** page by page from your account
3. **Batch process** posts in parallel:
   - Fetch post details via API
   - Batch fetch all tag details
   - Download images in parallel
4. **Organize files** into folders:
   - Single character: `{character_name}/{sensitivity}/`
   - Multiple characters: `Multiple/{copyright}/{sensitivity}/`
   - No character tags: `No Character/{sensitivity}/`
5. **Cache everything** to avoid reprocessing on future runs

### Progress Tracking

The script maintains several cache files:
- `posts_cache.json` - Successfully processed posts
- `tag_cache.json` - Tag details to avoid API calls
- `failed_posts_cache.json` - Posts that failed (for --retry-failed)
- `rate_limited_posts.json` - Currently rate-limited posts

You can safely interrupt the script with **Ctrl+C** - it will save all progress before exiting.

## Folder Structure

Downloaded images are organized as follows:

```
base_dir/
├── character_name_1/
│ ├── General/
│ ├── Sensitive/
│ ├── Questionable/
│ └── Explicit/
├── Multiple/
│ └── copyright_name/
│ ├── General/
│ ├── Sensitive/
│ ├── Questionable/
│ └── Explicit/
└── No Character/
├── General/
├── Sensitive/
├── Questionable/
└── Explicit/
```

## Troubleshooting

### Rate Limiting
If you see "Rate limited" messages, the script will automatically:
- Increase delays between requests
- Reduce concurrent workers
- Save progress and retry on next run

### Failed Downloads
Use `--list-failed` to see what failed, then `--retry-failed` to attempt recovery.

### Configuration Errors
Make sure `config.yaml` exists and has valid credentials. See `config.yaml.example` for the template.

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

## License

[MIT](https://choosealicense.com/licenses/mit/)
