"""
Article scraper for Citadel Securities News & Insights.

Fetches article listings and individual article content using
requests + BeautifulSoup with browser-like headers to handle
sites that block basic automated requests.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class Article:
    url: str
    title: str
    summary: str = ""
    body: str = ""
    date: str = ""
    author: str = ""
    tags: list = field(default_factory=list)


class CitadelScraper:
    """Scrapes articles from Citadel Securities News & Insights."""

    BASE_URL = "https://www.citadelsecurities.com"

    def __init__(self, urls: list[str], article_url_pattern: str = "/news-and-insights/"):
        self.urls = urls
        self.article_url_pattern = article_url_pattern
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _fetch_page(self, url: str, retries: int = 3, backoff: float = 2.0) -> Optional[str]:
        """Fetch a page with retry logic and exponential backoff."""
        for attempt in range(retries):
            try:
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as e:
                wait = backoff * (2 ** attempt)
                logger.warning(
                    "Attempt %d/%d failed for %s: %s. Retrying in %.1fs...",
                    attempt + 1, retries, url, e, wait,
                )
                time.sleep(wait)
        logger.error("All %d attempts failed for %s", retries, url)
        return None

    def discover_article_links(self) -> list[str]:
        """Discover article links from the listing pages."""
        links = set()
        for listing_url in self.urls:
            html = self._fetch_page(listing_url)
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser")
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                full_url = urljoin(listing_url, href)
                parsed = urlparse(full_url)
                # Match article URLs: must be under the pattern and have a slug
                if (
                    self.article_url_pattern in parsed.path
                    and parsed.path.rstrip("/") != self.article_url_pattern.rstrip("/")
                    and self.BASE_URL in full_url
                ):
                    links.add(full_url.rstrip("/"))
        logger.info("Discovered %d article links", len(links))
        return sorted(links)

    def scrape_article(self, url: str) -> Optional[Article]:
        """Scrape a single article page for its content."""
        html = self._fetch_page(url)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")
        article = Article(url=url, title=self._extract_title(soup))
        article.date = self._extract_date(soup)
        article.author = self._extract_author(soup)
        article.body = self._extract_body(soup)
        article.summary = self._extract_summary(soup, article.body)
        article.tags = self._extract_tags(soup)

        logger.info("Scraped article: %s", article.title)
        return article

    @staticmethod
    def _extract_title(soup: BeautifulSoup) -> str:
        # Try og:title first, then <h1>, then <title>
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            return og["content"].strip()
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)
        title = soup.find("title")
        return title.get_text(strip=True) if title else "Untitled"

    @staticmethod
    def _extract_date(soup: BeautifulSoup) -> str:
        # Try common date meta tags and elements
        for attr in ["article:published_time", "datePublished", "date"]:
            meta = soup.find("meta", property=attr) or soup.find("meta", attrs={"name": attr})
            if meta and meta.get("content"):
                return meta["content"].strip()
        time_el = soup.find("time")
        if time_el:
            return time_el.get("datetime", time_el.get_text(strip=True))
        return ""

    @staticmethod
    def _extract_author(soup: BeautifulSoup) -> str:
        meta = soup.find("meta", attrs={"name": "author"})
        if meta and meta.get("content"):
            return meta["content"].strip()
        author_el = soup.find(class_=re.compile(r"author", re.I))
        if author_el:
            return author_el.get_text(strip=True)
        return ""

    @staticmethod
    def _extract_body(soup: BeautifulSoup) -> str:
        # Look for article or main content containers
        content = (
            soup.find("article")
            or soup.find(class_=re.compile(r"article[-_]?(content|body|text)", re.I))
            or soup.find(class_=re.compile(r"(post|entry)[-_]?(content|body)", re.I))
            or soup.find("main")
        )
        if not content:
            content = soup.find("body")
        if not content:
            return ""

        # Remove script/style/nav/footer elements
        for tag in content.find_all(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        paragraphs = content.find_all(["p", "h2", "h3", "h4", "li", "blockquote"])
        if paragraphs:
            return "\n\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))
        return content.get_text(separator="\n", strip=True)

    @staticmethod
    def _extract_summary(soup: BeautifulSoup, body: str) -> str:
        og = soup.find("meta", property="og:description")
        if og and og.get("content"):
            return og["content"].strip()
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            return meta["content"].strip()
        # Fall back to first 300 chars of body
        if body:
            return body[:300].rsplit(" ", 1)[0] + "..."
        return ""

    @staticmethod
    def _extract_tags(soup: BeautifulSoup) -> list[str]:
        tags = []
        for meta in soup.find_all("meta", property="article:tag"):
            if meta.get("content"):
                tags.append(meta["content"].strip())
        return tags

    def scrape_all_new(self, seen_urls: set[str]) -> list[Article]:
        """Discover articles and scrape any not yet seen."""
        all_links = self.discover_article_links()
        new_links = [link for link in all_links if link not in seen_urls]
        logger.info("Found %d new articles out of %d total", len(new_links), len(all_links))

        articles = []
        for link in new_links:
            article = self.scrape_article(link)
            if article:
                articles.append(article)
            time.sleep(1)  # polite delay between requests
        return articles
