"""
Email digest sender.

Composes and sends HTML email digests containing
scraped article summaries and links.
"""

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from scraper import Article

logger = logging.getLogger(__name__)


def _build_article_html(article: Article) -> str:
    """Build an HTML block for a single article."""
    meta_parts = []
    if article.date:
        meta_parts.append(f"<strong>Date:</strong> {article.date}")
    if article.author:
        meta_parts.append(f"<strong>Author:</strong> {article.author}")
    if article.tags:
        meta_parts.append(f"<strong>Tags:</strong> {', '.join(article.tags)}")
    meta_line = " &middot; ".join(meta_parts)

    # Truncate body for digest (first 500 chars)
    preview = article.summary or article.body[:500]
    if len(preview) > 500:
        preview = preview[:500].rsplit(" ", 1)[0] + "..."

    return f"""
    <div style="margin-bottom: 28px; padding: 20px; border: 1px solid #e0e0e0;
                border-radius: 8px; background: #fafafa;">
      <h2 style="margin: 0 0 8px 0; color: #1a1a1a;">
        <a href="{article.url}" style="color: #0066cc; text-decoration: none;">
          {article.title}
        </a>
      </h2>
      {f'<p style="margin: 0 0 10px 0; font-size: 13px; color: #666;">{meta_line}</p>' if meta_line else ''}
      <p style="margin: 0; color: #333; line-height: 1.6;">{preview}</p>
      <p style="margin: 10px 0 0 0;">
        <a href="{article.url}" style="color: #0066cc; font-size: 14px;">Read full article &rarr;</a>
      </p>
    </div>
    """


def build_digest_html(articles: list[Article]) -> str:
    """Build the full HTML email body for the digest."""
    today = datetime.now().strftime("%B %d, %Y")
    article_blocks = "\n".join(_build_article_html(a) for a in articles)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
                 Roboto, Helvetica, Arial, sans-serif; max-width: 680px;
                 margin: 0 auto; padding: 20px; color: #333;">
      <div style="border-bottom: 3px solid #0066cc; padding-bottom: 15px;
                  margin-bottom: 25px;">
        <h1 style="margin: 0; color: #1a1a1a;">Citadel Securities &mdash; News &amp; Insights</h1>
        <p style="margin: 5px 0 0 0; color: #666;">Daily Digest &middot; {today}</p>
      </div>

      <p style="color: #555; margin-bottom: 20px;">
        {len(articles)} new article{"s" if len(articles) != 1 else ""} found today.
      </p>

      {article_blocks}

      <div style="border-top: 1px solid #e0e0e0; padding-top: 15px; margin-top: 25px;
                  font-size: 12px; color: #999;">
        <p>This digest was generated automatically by the article scraper.</p>
        <p>Source: <a href="https://www.citadelsecurities.com/news-and-insights/"
                      style="color: #999;">citadelsecurities.com/news-and-insights</a></p>
      </div>
    </body>
    </html>
    """


def build_digest_text(articles: list[Article]) -> str:
    """Build a plain text version of the digest."""
    today = datetime.now().strftime("%B %d, %Y")
    lines = [
        "Citadel Securities - News & Insights",
        f"Daily Digest - {today}",
        "=" * 50,
        f"\n{len(articles)} new article{'s' if len(articles) != 1 else ''} found today.\n",
    ]
    for article in articles:
        lines.append(f"  {article.title}")
        if article.date:
            lines.append(f"  Date: {article.date}")
        if article.summary:
            preview = article.summary[:300]
            lines.append(f"  {preview}")
        lines.append(f"  Link: {article.url}")
        lines.append("")
    lines.append("-" * 50)
    lines.append("Source: https://www.citadelsecurities.com/news-and-insights/")
    return "\n".join(lines)


def send_digest(
    articles: list[Article],
    smtp_server: str,
    smtp_port: int,
    sender_email: str,
    sender_password: str,
    recipients: list[str],
    subject_prefix: str = "[Citadel Insights]",
) -> bool:
    """Send the email digest to all recipients."""
    if not articles:
        logger.info("No new articles to send.")
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    subject = f"{subject_prefix} Daily Digest - {today} ({len(articles)} new)"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = ", ".join(recipients)

    text_body = build_digest_text(articles)
    html_body = build_digest_html(articles)
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, recipients, msg.as_string())
        logger.info("Digest sent to %s", ", ".join(recipients))
        return True
    except smtplib.SMTPException:
        logger.exception("Failed to send digest email")
        return False
