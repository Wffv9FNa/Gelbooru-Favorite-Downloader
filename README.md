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
- **Rate-limit summary** printed at the end of every run (and on Ctrl+C) to help tune `config.yaml`, including a per-endpoint breakdown of throttle-gate waits (post-detail / tag / download)
- Optional file logging, plus a verbose `--debug` mode for rate-limit telemetry

## Requirements

- Python 3.9 or later (the code uses built-in generic type hints such as `tuple[str, str]`)
- Required packages:
  - beautifulsoup4
  - requests
  - pyyaml
  - colorama
  - python-dotenv

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
pip install beautifulsoup4 requests pyyaml colorama python-dotenv
```

3. Configure your credentials and settings:
```bash
cp .env.example .env
cp config.yaml.example config.yaml
```

4. Edit `.env` and add your Gelbooru credentials:
   - **GELBOORU_API_KEY** and **GELBOORU_USER_ID**: Get these from Gelbooru → My Account → Options → API Access Credentials
   - **GELBOORU_USERNAME** and **GELBOORU_PASSWORD**: Your Gelbooru login credentials

   Tuning options (download folders, threading, rate limiting) live in `config.yaml`.

## Configuration

Credentials live in `.env`; all tuning options live in `config.yaml`.

### API Credentials

Credentials are read from a `.env` file (never committed). Copy `.env.example` to `.env` and fill in:
```
GELBOORU_API_KEY=your-api-key-here
GELBOORU_USER_ID=your-user-id-here
GELBOORU_USERNAME=your-username-here
GELBOORU_PASSWORD=your-password-here
```

`config.yaml` must contain four sections - `settings`, `cache`, `threading`, and `rate_limiting`. The script exits with an error if any section is missing.

### General Settings (`settings`)
- `posts_per_page`: Number of posts to fetch per page (default: 50)
- `max_consecutive_empty_pages`: Stop after this many pages with no new downloads (default: 10)
- `base_dir`: Base directory for downloads (leave empty to use script directory)

### Cache Files (`cache`)
- `tag_cache_file`: Tag detail cache (default: `tag_cache.json`)
- `posts_cache_file`: Successfully processed posts (default: `posts_cache.json`)
- `failed_posts_cache_file`: Failed posts for `--retry-failed` (default: `failed_posts_cache.json`)
- `rate_limited_posts_file`: Currently rate-limited posts (default: `rate_limited_posts.json`)

### Threading & Performance (`threading`)
- `max_workers`: Parallel API request threads (default: 4)
- `download_workers`: Parallel download threads (default: 3)
- `tag_batch_size`: Tags to process per batch (default: 20)

### Rate Limiting (`rate_limiting`)
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
Retry posts that previously failed (`-r` is a short alias):
```bash
python gelbooru_favorite_downloader.py --retry-failed
```

### List Failed Posts
Display all failed posts (and any currently rate-limited posts) without retrying:
```bash
python gelbooru_favorite_downloader.py --list-failed
```

### Debug Mode
Emit verbose rate-limit telemetry (per-event timing, backoff, retries). Combine with `-logtofile` to also write a `debug_log.txt`:
```bash
python gelbooru_favorite_downloader.py --debug
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
   - Multiple characters with a copyright tag: `Multiple/{copyright}/{sensitivity}/`
   - Multiple characters with no copyright tag: `Multiple/{sensitivity}/`
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
Make sure `.env` exists with valid credentials (copy `.env.example` to `.env`), and that `config.yaml` exists with your tuning settings (copy `config.yaml.example` to `config.yaml`).

### Folder Names With HTML Entities
Folders created before the entity-decoding fix may contain literal HTML entities in their names (e.g. `agent_(girls&#039;_frontline)/`), whereas folders created afterwards use the decoded form (e.g. `agent_(girls'_frontline)/`). The two are **not** merged or renamed automatically: already-downloaded posts are deduplicated by post id in `posts_cache`, so no images are re-downloaded - affected characters simply have a one-time split between the old and new folder. If you want a single folder, move the old contents across manually. Tag names are assumed to be single-encoded; a rare double-encoded name would need more than one decode pass and is intentionally not handled.

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

## License

[MIT](https://choosealicense.com/licenses/mit/)
