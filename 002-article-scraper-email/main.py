#!/usr/bin/env python3
"""
Citadel Securities Article Scraper & Email Digest

Scrapes new articles from Citadel Securities' News & Insights page
and sends a daily email digest with summaries and links.

Usage:
    python main.py                  # Run once (for cron)
    python main.py --dry-run        # Scrape but don't send email
    python main.py --config path    # Use custom config file
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from emailer import send_digest
from scraper import CitadelScraper

DEFAULT_CONFIG = "config.json"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

logger = logging.getLogger("digest")


def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        logger.error("Config file not found: %s", config_path)
        logger.error("Copy config.example.json to config.json and fill in your settings.")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def load_seen_articles(filepath: str) -> set[str]:
    path = Path(filepath)
    if path.exists():
        with open(path) as f:
            return set(json.load(f))
    return set()


def save_seen_articles(filepath: str, seen: set[str]) -> None:
    with open(filepath, "w") as f:
        json.dump(sorted(seen), f, indent=2)


def run(config_path: str, dry_run: bool = False) -> None:
    config = load_config(config_path)
    scraper_cfg = config["scraper"]
    email_cfg = config["email"]
    storage_cfg = config["storage"]

    # Resolve storage path relative to config file
    config_dir = str(Path(config_path).parent)
    seen_file = os.path.join(config_dir, storage_cfg["seen_articles_file"])

    # Load previously seen article URLs
    seen_urls = load_seen_articles(seen_file)
    logger.info("Loaded %d previously seen articles", len(seen_urls))

    # Scrape for new articles
    scraper = CitadelScraper(
        urls=scraper_cfg["urls"],
        article_url_pattern=scraper_cfg.get("article_url_pattern", "/news-and-insights/"),
    )
    new_articles = scraper.scrape_all_new(seen_urls)

    if not new_articles:
        logger.info("No new articles found. Nothing to send.")
        return

    logger.info("Found %d new articles:", len(new_articles))
    for article in new_articles:
        logger.info("  - %s (%s)", article.title, article.url)

    if dry_run:
        logger.info("Dry run mode — skipping email send.")
        # Still print a preview
        for article in new_articles:
            print(f"\n{'='*60}")
            print(f"Title:   {article.title}")
            print(f"URL:     {article.url}")
            print(f"Date:    {article.date}")
            print(f"Author:  {article.author}")
            print(f"Summary: {article.summary[:200]}")
    else:
        success = send_digest(
            articles=new_articles,
            smtp_server=email_cfg["smtp_server"],
            smtp_port=email_cfg["smtp_port"],
            sender_email=email_cfg["sender_email"],
            sender_password=email_cfg.get("sender_password") or os.environ.get("SMTP_PASSWORD", ""),
            recipients=email_cfg["recipients"],
            subject_prefix=email_cfg.get("subject_prefix", "[Citadel Insights]"),
        )
        if not success:
            logger.error("Failed to send digest email")
            sys.exit(1)

    # Mark articles as seen
    new_urls = {a.url for a in new_articles}
    seen_urls.update(new_urls)
    save_seen_articles(seen_file, seen_urls)
    logger.info("Updated seen articles list (%d total)", len(seen_urls))


def main():
    parser = argparse.ArgumentParser(
        description="Citadel Securities Article Scraper & Email Digest"
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help="Path to config JSON file (default: config.json)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scrape articles but don't send email"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FORMAT,
    )

    run(config_path=args.config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
