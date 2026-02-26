# Citadel Securities Article Scraper & Email Digest

Scrapes new articles from [Citadel Securities News & Insights](https://www.citadelsecurities.com/news-and-insights/) and sends a daily email digest.

## Setup

### 1. Install dependencies

```sh
pip install -r requirements.txt
```

### 2. Configure

Copy the example config and fill in your settings:

```sh
cp config.example.json config.json
```

Edit `config.json`:
- **`scraper.urls`** — listing page(s) to scrape for article links
- **`email.smtp_server`** / **`smtp_port`** — your SMTP server (defaults to Gmail)
- **`email.sender_email`** — the "from" email address
- **`email.sender_password`** — app password (or set `SMTP_PASSWORD` env var)
- **`email.recipients`** — list of email addresses to receive the digest

> **Gmail users:** Use an [App Password](https://support.google.com/accounts/answer/185833) instead of your account password.

### 3. Run manually

```sh
# Scrape and send digest
python main.py

# Dry run — scrape and preview without sending email
python main.py --dry-run

# Verbose logging
python main.py --dry-run -v
```

### 4. Schedule daily with cron

Run `crontab -e` and add a line like:

```cron
# Every day at 8:00 AM
0 8 * * * cd /path/to/002-article-scraper-email && /usr/bin/python3 main.py >> digest.log 2>&1
```

Or using the `SMTP_PASSWORD` environment variable:

```cron
0 8 * * * cd /path/to/002-article-scraper-email && SMTP_PASSWORD="your-app-password" /usr/bin/python3 main.py >> digest.log 2>&1
```

## How it works

1. **Discover** — fetches the listing page(s) and extracts article links
2. **Filter** — compares against previously seen URLs (stored in `seen_articles.json`)
3. **Scrape** — fetches each new article page, extracts title, date, author, body, summary
4. **Email** — composes an HTML + plain text digest and sends via SMTP
5. **Track** — saves newly seen URLs so they aren't sent again

## Project structure

```
002-article-scraper-email/
├── config.example.json   # Example configuration (copy to config.json)
├── emailer.py            # Email digest composition and sending
├── main.py               # CLI entry point and orchestration
├── requirements.txt      # Python dependencies
├── scraper.py            # Web scraper for article discovery and extraction
└── README.md
```
