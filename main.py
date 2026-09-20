"""
YouTube Playlist to Text Transcriber
Usage: python main.py --playlist-url <URL> [options]
"""

import argparse
import os
import sys
import tempfile
import traceback
import time
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import urllib.request
import urllib.error
import urllib.parse
import json
import re

import yt_dlp
import whisper
from yt_dlp.utils import DownloadError


def load_env(path=".env"):
    """Load key=value pairs from a .env file into os.environ (does not overwrite)."""
    env_path = Path(path)
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download a YouTube playlist and transcribe each video to a text file."
    )
    parser.add_argument(
        "--playlist-url",
        required=True,
        help="Public YouTube playlist URL",
    )
    parser.add_argument(
        "--output",
        default="output/transcripts.txt",
        help="Output text file path (default: output/transcripts.txt)",
    )
    parser.add_argument(
        "--model",
        default="base",
        choices=["tiny", "base", "small", "medium", "large"],
        help="Whisper model size (default: base). Larger = more accurate but slower.",
    )
    parser.add_argument(
        "--max-videos",
        type=int,
        default=None,
        help="Limit number of videos to process after filtering (useful for testing)",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=1,
        help="1-based playlist index to start from (default: 1)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Only process videos that failed in the previous output file",
    )
    parser.add_argument(
        "--retry-failed-from",
        default=None,
        help="Path to a previous output report to reprocess only failed video IDs",
    )
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="Keep downloaded audio files after transcription",
    )
    parser.add_argument(
        "--cookies-file",
        default=None,
        help="Path to cookies.txt for authenticated YouTube access",
    )
    parser.add_argument(
        "--cookies-from-browser",
        default=None,
        choices=["chrome", "edge", "firefox", "brave", "opera", "vivaldi"],
        help="Read cookies directly from browser (fallback when DPAPI works)",
    )
    parser.add_argument(
        "--js-runtime",
        default="node",
        help="JS runtime used by yt-dlp challenge solver (default: node)",
    )
    parser.add_argument(
        "--remote-components",
        default="ejs:github",
        help="yt-dlp remote components source (default: ejs:github)",
    )
    parser.add_argument(
        "--extractor-args",
        default="youtube:player_client=web_creator,tv",
        help="yt-dlp extractor args string",
    )
    parser.add_argument(
        "--sleep-requests",
        type=float,
        default=1.0,
        help="Sleep between yt-dlp requests (default: 1.0s)",
    )
    parser.add_argument(
        "--min-sleep-interval",
        type=float,
        default=1.0,
        help="Minimum randomized sleep interval per download (default: 1.0s)",
    )
    parser.add_argument(
        "--max-sleep-interval",
        type=float,
        default=3.0,
        help="Maximum randomized sleep interval per download (default: 3.0s)",
    )
    parser.add_argument(
        "--retry-count",
        type=int,
        default=3,
        help="Retry attempts for transient yt-dlp failures (default: 3)",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=2.0,
        help="Base backoff seconds used for retries (default: 2.0)",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop the run on first failed video (default: continue)",
    )
    parser.add_argument(
        "--playlist-enumerator",
        default="auto",
        choices=["auto", "yt-dlp", "playwright", "api"],
        help="How to enumerate playlist videos (default: auto)",
    )
    parser.add_argument(
        "--playwright-headful",
        action="store_true",
        help="Run Playwright with visible browser window",
    )
    parser.add_argument(
        "--playwright-max-scroll-rounds",
        type=int,
        default=250,
        help="Max scroll rounds for Playwright playlist scraping (default: 250)",
    )
    parser.add_argument(
        "--playwright-stable-rounds",
        type=int,
        default=8,
        help="Stop Playwright scraping after this many no-growth rounds (default: 8)",
    )
    parser.add_argument(
        "--playwright-scroll-pause-seconds",
        type=float,
        default=1.0,
        help="Pause between Playwright scroll rounds (default: 1.0)",
    )
    parser.add_argument(
        "--expected-videos",
        type=int,
        default=None,
        help="Expected playlist size for preflight validation",
    )
    parser.add_argument(
        "--strict-enumeration",
        action="store_true",
        help="Fail before transcription if discovered URLs are fewer than --expected-videos",
    )
    parser.add_argument(
        "--enumerate-only",
        action="store_true",
        help="Only discover and save playlist URLs, then exit",
    )
    parser.add_argument(
        "--url-manifest",
        default="output/playlist_urls.txt",
        help="Where to write discovered playlist URLs (default: output/playlist_urls.txt)",
    )
    parser.add_argument(
        "--youtube-api-key",
        default=None,
        help="YouTube Data API v3 key (or set YOUTUBE_API_KEY in .env)",
    )
    return parser.parse_args()


def parse_extractor_args(raw_value):
    """
    Convert a CLI string like:
    youtube:player_client=web_creator,tv;youtube:lang=en
    into yt-dlp extractor_args dict format.
    """
    extractor_args = {}
    if not raw_value:
        return extractor_args

    for section in raw_value.split(";"):
        section = section.strip()
        if not section or ":" not in section:
            continue
        extractor, pairs = section.split(":", 1)
        extractor = extractor.strip()
        if not extractor:
            continue
        extractor_args.setdefault(extractor, {})
        current_key = None
        current_values = []
        for token in pairs.split(","):
            token = token.strip()
            if not token:
                continue
            if "=" in token:
                if current_key and current_values:
                    values = []
                    for item in current_values:
                        values.extend([part for part in item.split("|") if part])
                    extractor_args[extractor][current_key] = values
                current_key, first_value = token.split("=", 1)
                current_key = current_key.strip()
                current_values = [first_value.strip()]
            elif current_key:
                current_values.append(token)
        if current_key and current_values:
            values = []
            for item in current_values:
                values.extend([part for part in item.split("|") if part])
            extractor_args[extractor][current_key] = values
    return extractor_args


def validate_auth_inputs(args):
    if args.start_index < 1:
        raise ValueError("--start-index must be at least 1")
    if args.retry_failed and args.retry_failed_from:
        raise ValueError("Use either --retry-failed or --retry-failed-from, not both")
    if args.expected_videos is not None and args.expected_videos < 1:
        raise ValueError("--expected-videos must be at least 1")
    if args.playwright_stable_rounds < 1:
        raise ValueError("--playwright-stable-rounds must be at least 1")
    if args.playwright_max_scroll_rounds < 1:
        raise ValueError("--playwright-max-scroll-rounds must be at least 1")
    if args.playwright_scroll_pause_seconds <= 0:
        raise ValueError("--playwright-scroll-pause-seconds must be greater than 0")
    if args.min_sleep_interval > args.max_sleep_interval:
        raise ValueError("--min-sleep-interval cannot be greater than --max-sleep-interval")
    if args.cookies_file:
        cookie_path = Path(args.cookies_file)
        if not cookie_path.exists():
            raise FileNotFoundError(f"Cookie file not found: {cookie_path}")
        if cookie_path.stat().st_size == 0:
            raise ValueError(f"Cookie file is empty: {cookie_path}")


def build_common_ydl_opts(args):
    remote_components = {item.strip() for item in args.remote_components.split(",") if item.strip()}
    opts = {
        "quiet": True,
        "js_runtimes": {args.js_runtime: {}},
        "remote_components": remote_components,
        "extractor_args": parse_extractor_args(args.extractor_args),
        "sleep_interval_requests": args.sleep_requests,
        "sleep_interval": args.min_sleep_interval,
        "max_sleep_interval": args.max_sleep_interval,
    }
    if args.cookies_file:
        opts["cookiefile"] = str(Path(args.cookies_file))
    elif args.cookies_from_browser:
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    return opts


def extract_playlist_id_from_url(url):
    parsed = urlparse(url)
    playlist_id = parse_qs(parsed.query).get("list", [None])[0]
    return playlist_id


def fetch_playlist_entries_with_api(playlist_url, api_key):
    """Fetch ALL playlist entries using YouTube Data API v3 (guaranteed full list)."""
    playlist_id = extract_playlist_id_from_url(playlist_url)
    if not playlist_id:
        raise ValueError(f"Could not extract playlist ID from: {playlist_url}")

    print(f"Fetching full playlist via YouTube Data API (playlist ID: {playlist_id})...")

    entries = []
    next_page_token = None
    page_num = 0

    while True:
        page_num += 1
        params = {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": "50",
            "key": api_key,
        }
        if next_page_token:
            params["pageToken"] = next_page_token

        query_string = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        api_url = f"https://www.googleapis.com/youtube/v3/playlistItems?{query_string}"

        try:
            with urllib.request.urlopen(api_url, timeout=30) as response:
                data = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"YouTube API error {exc.code}: {body}"
            ) from exc

        items = data.get("items", [])
        for item in items:
            snippet = item.get("snippet", {})
            resource = snippet.get("resourceId", {})
            video_id = resource.get("videoId")
            title = snippet.get("title") or f"Video {video_id}"
            if not video_id:
                continue
            if title in ("Deleted video", "Private video"):
                continue
            entries.append(
                {
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "id": video_id,
                }
            )

        print(f"  API page {page_num}: {len(items)} items (total so far: {len(entries)})")
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    print(f"YouTube Data API returned {len(entries)} video(s) total.")
    return entries


def fetch_playlist_entries_with_ytdlp(playlist_url, ydl_common_opts):
    """Return (entries, playlist_count) extracted by yt-dlp."""
    ydl_opts = {
        **ydl_common_opts,
        "extract_flat": True,
        "skip_download": True,
    }
    print(f"Fetching playlist metadata from: {playlist_url}")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(playlist_url, download=False)

    entries = info.get("entries", [])
    if not entries:
        print("No entries found in playlist. Make sure the playlist is public.")
        sys.exit(1)

    results = []
    for entry in entries:
        if entry is None:
            continue
        video_id = entry.get("id") or entry.get("url", "").split("?v=")[-1]
        title = entry.get("title") or f"Video {video_id}"
        url = f"https://www.youtube.com/watch?v={video_id}"
        results.append({"title": title, "url": url, "id": video_id})

    playlist_count = info.get("playlist_count")
    return results, playlist_count


def extract_video_id_from_url(url):
    if not url:
        return None
    normalized_url = url.strip()
    if normalized_url.startswith("//"):
        normalized_url = f"https:{normalized_url}"
    elif normalized_url.startswith("/"):
        normalized_url = f"https://www.youtube.com{normalized_url}"

    parsed = urlparse(normalized_url)
    netloc = parsed.netloc.lower()
    if "youtube.com" not in netloc and "youtu.be" not in netloc:
        return None
    if netloc == "youtu.be":
        return parsed.path.strip("/") or None
    if parsed.path.startswith("/watch"):
        return parse_qs(parsed.query).get("v", [None])[0]
    if parsed.path.startswith("/shorts/"):
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2:
            return parts[1]
    return None


def load_cookies_for_playwright(cookie_file):
    jar = MozillaCookieJar()
    jar.load(cookie_file, ignore_discard=True, ignore_expires=True)
    cookies = []
    for cookie in jar:
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
                "httpOnly": False,
            }
        )
    return cookies


def write_playwright_debug_artifacts(page, args, reason):
    debug_dir = Path(args.url_manifest).parent
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time())

    screenshot_path = debug_dir / f"playwright_debug_{timestamp}.png"
    html_path = debug_dir / f"playwright_debug_{timestamp}.html"
    links_path = debug_dir / f"playwright_debug_links_{timestamp}.txt"

    page.screenshot(path=str(screenshot_path), full_page=True)
    with open(html_path, "w", encoding="utf-8") as handle:
        handle.write(page.content())
    sampled_hrefs = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a'))
            .map(a => a.getAttribute('href') || a.href || '')
            .filter(Boolean)
            .slice(0, 300)
        """
    )
    with open(links_path, "w", encoding="utf-8") as handle:
        handle.write(f"Reason: {reason}\n")
        handle.write(f"Page URL: {page.url}\n\n")
        for href in sampled_hrefs:
            handle.write(f"{href}\n")

    return screenshot_path, html_path, links_path


def fetch_playlist_entries_with_playwright(playlist_url, args):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium"
        ) from exc

    print("Using Playwright fallback to enumerate full playlist...")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.playwright_headful)
        context = browser.new_context()
        if args.cookies_file:
            context.add_cookies(load_cookies_for_playwright(args.cookies_file))
        page = context.new_page()
        try:
            page.goto(playlist_url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(3000)
            page.wait_for_selector("ytd-app", timeout=20000)

            stable_rounds = 0
            best_count = 0
            for _ in range(args.playwright_max_scroll_rounds):
                page.evaluate(
                    """
                    () => {
                        const targets = [
                            document.querySelector('ytd-rich-grid-renderer #contents'),
                            document.querySelector('ytd-item-section-renderer #contents'),
                            document.querySelector('ytd-playlist-video-list-renderer #contents'),
                            document.querySelector('ytd-section-list-renderer #contents'),
                            document.scrollingElement
                        ].filter(Boolean);

                        for (const target of targets) {
                            target.scrollTop = target.scrollHeight;
                        }
                        window.scrollTo(0, document.body.scrollHeight);
                    }
                    """
                )
                page.wait_for_timeout(int(args.playwright_scroll_pause_seconds * 1000))
                current_count = page.evaluate(
                    """
                    () => {
                        const itemSelector = 'ytd-rich-item-renderer, ytd-playlist-video-renderer, ytd-grid-video-renderer, ytm-shorts-lockup-view-model, ytm-shorts-lockup-view-model-v2';
                        const candidates = Array.from(document.querySelectorAll(
                            'a[href*="/shorts/"], a[href*="/watch?v="]'
                        ));
                        const ids = new Set();
                        for (const anchor of candidates) {
                            if (!anchor.closest(itemSelector)) {
                                continue;
                            }
                            const rawHref = anchor.getAttribute('href') || anchor.href || '';
                            if (!rawHref) {
                                continue;
                            }
                            let videoId = null;
                            try {
                                const url = new URL(rawHref, window.location.origin);
                                if (url.pathname.startsWith('/shorts/')) {
                                    videoId = url.pathname.split('/').filter(Boolean)[1] || null;
                                } else if (url.pathname.startsWith('/watch')) {
                                    videoId = url.searchParams.get('v');
                                }
                            } catch (_err) {}
                            if (videoId) {
                                ids.add(videoId);
                            }
                        }
                        return ids.size;
                    }
                    """
                )
                if current_count > best_count:
                    best_count = current_count
                    stable_rounds = 0
                else:
                    stable_rounds += 1
                    if stable_rounds >= args.playwright_stable_rounds:
                        break

            raw_entries = page.evaluate(
                """
                () => {
                    const itemSelector = 'ytd-rich-item-renderer, ytd-playlist-video-renderer, ytd-grid-video-renderer, ytm-shorts-lockup-view-model, ytm-shorts-lockup-view-model-v2';
                    const anchors = Array.from(document.querySelectorAll(
                        'a[href*="/shorts/"], a[href*="/watch?v="]'
                    ));
                    const extracted = [];

                    for (const anchor of anchors) {
                        const item = anchor.closest(itemSelector);
                        if (!item) {
                            continue;
                        }
                        const href = anchor.getAttribute('href') || anchor.href || '';
                        if (!href) {
                            continue;
                        }

                        let title =
                            (anchor.getAttribute('title')
                            || anchor.getAttribute('aria-label')
                            || anchor.textContent
                            || '').trim();
                        if (!title) {
                            const titleNode = item.querySelector(
                                '#video-title, #video-title-link, yt-formatted-string#video-title, h3, .shortsLockupViewModelHostMetadataSubhead'
                            );
                            title = (
                                titleNode?.getAttribute('title')
                                || titleNode?.getAttribute('aria-label')
                                || titleNode?.textContent
                                || ''
                            ).trim();
                        }

                        extracted.push({ href, title });
                    }
                    return extracted;
                }
                """
            )
        finally:
            browser.close()

    entries = []
    seen_ids = set()
    for raw in raw_entries:
        href = raw.get("href", "")
        title = raw.get("title") or ""
        video_id = extract_video_id_from_url(href)
        if not video_id or video_id in seen_ids:
            continue
        seen_ids.add(video_id)
        entries.append(
            {
                "title": title or f"Video {video_id}",
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "id": video_id,
            }
        )

    if not entries:
        # Re-open quickly to capture debug artifacts for diagnosis.
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            if args.cookies_file:
                context.add_cookies(load_cookies_for_playwright(args.cookies_file))
            page = context.new_page()
            page.goto(playlist_url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(2000)
            screenshot_path, html_path, links_path = write_playwright_debug_artifacts(
                page, args, "zero_entries_after_playwright_scrape"
            )
            browser.close()
        raise RuntimeError(
            "Playwright fallback found zero playlist videos. "
            f"Debug artifacts written to: {screenshot_path}, {html_path}, {links_path}"
        )

    print(f"Playwright extracted {len(entries)} video(s).")
    return entries


def fetch_playlist_entries(playlist_url, ydl_common_opts, args):
    api_key = getattr(args, "youtube_api_key", None) or os.environ.get("YOUTUBE_API_KEY", "").strip()
    if api_key in ("", "your_api_key_here"):
        api_key = None

    # If user explicitly requested API mode
    if args.playlist_enumerator == "api":
        if not api_key:
            raise ValueError(
                "--playlist-enumerator api requires a YouTube API key. "
                "Set YOUTUBE_API_KEY in .env or pass --youtube-api-key."
            )
        entries = fetch_playlist_entries_with_api(playlist_url, api_key)
        print(f"Found {len(entries)} video(s) to process (api).")
        return entries, {"source": "api", "yt_dlp_count": None, "playlist_count": None}

    # Always start with yt-dlp for metadata/count
    entries, playlist_count = fetch_playlist_entries_with_ytdlp(playlist_url, ydl_common_opts)
    source = "yt-dlp"

    if args.playlist_enumerator == "yt-dlp":
        print(f"Found {len(entries)} video(s) to process (yt-dlp).")
        return entries, {
            "source": source,
            "yt_dlp_count": len(entries),
            "playlist_count": playlist_count,
        }

    if isinstance(playlist_count, int) and len(entries) < playlist_count:
        print(
            "WARNING: Playlist appears to have "
            f"{playlist_count} videos but yt-dlp extracted {len(entries)}."
        )

    needs_more = (
        args.playlist_enumerator in ("playwright",)
        or (args.playlist_enumerator == "auto" and isinstance(playlist_count, int) and len(entries) < playlist_count)
        or (args.playlist_enumerator == "auto" and args.expected_videos is not None and len(entries) < args.expected_videos)
        or (args.playlist_enumerator == "auto" and len(entries) == 100)
    )

    if needs_more and api_key:
        try:
            api_entries = fetch_playlist_entries_with_api(playlist_url, api_key)
            if len(api_entries) >= len(entries):
                print(
                    f"Using API entry list ({len(api_entries)} items) "
                    f"instead of yt-dlp list ({len(entries)} items)."
                )
                entries = api_entries
                source = "api"
                needs_more = False
        except Exception as exc:
            print(f"YouTube API enumeration failed: {exc}")
            print("Falling through to Playwright/yt-dlp.")

    if needs_more:
        try:
            fallback_entries = fetch_playlist_entries_with_playwright(playlist_url, args)
            if len(fallback_entries) >= len(entries):
                print(
                    f"Using Playwright entry list ({len(fallback_entries)} items) "
                    f"instead of yt-dlp list ({len(entries)} items)."
                )
                entries = fallback_entries
                source = "playwright"
        except Exception as exc:
            print(f"Playwright fallback failed: {exc}")
            print("Continuing with yt-dlp playlist results.")

    print(f"Found {len(entries)} video(s) to process.")
    return entries, {
        "source": source,
        "yt_dlp_count": len(entries) if source == "yt-dlp" else None,
        "playlist_count": playlist_count,
    }


def write_url_manifest(entries, path):
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        for idx, entry in enumerate(entries, start=1):
            handle.write(f"{idx}. {entry['title']}\n")
            handle.write(f"{entry['url']}\n\n")
    return output_path


def load_failed_video_ids(report_path):
    path = Path(report_path)
    if not path.exists():
        raise FileNotFoundError(f"Failed-report file not found: {path}")

    failed_ids = set()
    in_failed_section = False
    with open(path, "r", encoding="utf-8") as file_handle:
        for raw_line in file_handle:
            line = raw_line.strip()
            if line == "Failed videos:":
                in_failed_section = True
                continue
            if not in_failed_section:
                continue
            if not line.startswith("- "):
                continue
            parts = line[2:].split("|")
            if not parts:
                continue
            video_id = parts[0].strip()
            if video_id:
                failed_ids.add(video_id)
    return failed_ids


def is_retryable_error(message):
    msg = message.lower()
    retry_markers = [
        "http error 429",
        "too many requests",
        "timed out",
        "connection reset",
        "getaddrinfo failed",
        "failed to resolve",
        "temporarily unavailable",
        "remote end closed connection",
    ]
    return any(marker in msg for marker in retry_markers)


def classify_error(message):
    msg = message.lower()
    if "sign in to confirm you" in msg or "use --cookies" in msg:
        return "AUTH_ERROR"
    if "http error 429" in msg or "too many requests" in msg:
        return "RATE_LIMIT"
    if "requested format is not available" in msg or "no formats" in msg:
        return "FORMAT_ERROR"
    return "DOWNLOAD_ERROR"


def download_audio(entry, audio_dir, ydl_common_opts, retry_count, retry_backoff_seconds):
    """Download audio for a single video to audio_dir. Returns path to downloaded file."""
    video_id = entry["id"]
    out_template = str(Path(audio_dir) / f"{video_id}.%(ext)s")

    ydl_opts = {
        **ydl_common_opts,
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
    }
    last_error = None
    for attempt in range(1, retry_count + 2):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([entry["url"]])
            last_error = None
            break
        except DownloadError as err:
            last_error = err
            if attempt <= retry_count and is_retryable_error(str(err)):
                delay = retry_backoff_seconds * attempt
                print(
                    f"  Retry {attempt}/{retry_count} in {delay:.1f}s "
                    f"due to transient error..."
                )
                time.sleep(delay)
                continue
            raise
    if last_error is not None:
        raise last_error

    audio_path = Path(audio_dir) / f"{video_id}.mp3"
    if not audio_path.exists():
        # Fallback: find any file with the video_id prefix
        matches = list(Path(audio_dir).glob(f"{video_id}.*"))
        if matches:
            audio_path = matches[0]
        else:
            raise FileNotFoundError(f"Audio file not found for video {video_id}")

    return str(audio_path)


def transcribe_audio(model, audio_path):
    """Transcribe an audio file with Whisper and return the full text."""
    result = model.transcribe(audio_path, fp16=False)
    return result["text"].strip()


def format_entry_block(index, entry, transcript):
    lines = [
        f"=== Video {index} ===",
        f"Title: {entry['title']}",
        f"URL:   {entry['url']}",
        "",
        "Transcript:",
        transcript,
        "",
        "-" * 60,
        "",
    ]
    return "\n".join(lines)


def main():
    load_env()
    args = parse_args()
    validate_auth_inputs(args)
    ydl_common_opts = build_common_ydl_opts(args)

    entries, enumeration_meta = fetch_playlist_entries(
        args.playlist_url, ydl_common_opts=ydl_common_opts, args=args
    )

    manifest_path = write_url_manifest(entries, args.url_manifest)
    print(f"Preflight complete: discovered {len(entries)} playlist URL(s).")
    print(f"Enumeration source: {enumeration_meta['source']}")
    print(f"URL manifest saved to: {manifest_path.resolve()}")

    if args.expected_videos is not None and len(entries) < args.expected_videos:
        message = (
            f"Discovered {len(entries)} video URLs, which is below "
            f"--expected-videos {args.expected_videos}."
        )
        if args.strict_enumeration:
            raise ValueError(message + " Aborting due to --strict-enumeration.")
        print(f"WARNING: {message}")
    elif args.expected_videos is not None:
        print(f"Expected videos target met: {len(entries)} >= {args.expected_videos}.")

    if args.enumerate_only:
        print("Exiting due to --enumerate-only (no downloads or transcription were run).")
        return

    if args.start_index > len(entries):
        raise ValueError(
            f"--start-index ({args.start_index}) is larger than extracted videos ({len(entries)})"
        )
    if args.start_index > 1:
        entries = entries[args.start_index - 1 :]
        print(f"Start index applied: processing from playlist position {args.start_index}.")

    failed_source_path = None
    if args.retry_failed:
        failed_source_path = args.output
    elif args.retry_failed_from:
        failed_source_path = args.retry_failed_from

    if failed_source_path:
        failed_ids = load_failed_video_ids(failed_source_path)
        if not failed_ids:
            print(f"No failed video IDs found in: {failed_source_path}")
            print("Nothing to process.")
            return
        before_count = len(entries)
        entries = [entry for entry in entries if entry["id"] in failed_ids]
        print(
            f"Retry-failed filter applied: {len(entries)} of {before_count} entries "
            f"matched failures from {failed_source_path}."
        )
        if not entries:
            print("No matching failed IDs found in current playlist slice.")
            return

    if args.max_videos is not None:
        entries = entries[: args.max_videos]
        print(f"Max-videos applied: processing first {len(entries)} filtered entries.")

    print(f"\nLoading Whisper model '{args.model}'... (first run downloads the model)")
    model = whisper.load_model(args.model)
    print("Model ready.\n")

    audio_dir = tempfile.mkdtemp(prefix="yt_audio_")
    if args.keep_audio:
        audio_dir = str(Path(args.output).parent / "audio")
        Path(audio_dir).mkdir(parents=True, exist_ok=True)
        print(f"Audio files will be kept at: {audio_dir}")

    success_count = 0
    fail_count = 0
    blocks = []
    failed_items = []

    for i, entry in enumerate(entries, start=1):
        print(f"[{i}/{len(entries)}] Processing: {entry['title']}")
        try:
            print(f"  Downloading audio...")
            audio_path = download_audio(
                entry,
                audio_dir,
                ydl_common_opts=ydl_common_opts,
                retry_count=args.retry_count,
                retry_backoff_seconds=args.retry_backoff_seconds,
            )
            print(f"  Transcribing...")
            transcript = transcribe_audio(model, audio_path)
            blocks.append(format_entry_block(i, entry, transcript))
            print(f"  Done. ({len(transcript)} chars)\n")
            success_count += 1

            if not args.keep_audio:
                try:
                    os.remove(audio_path)
                except OSError:
                    pass

        except Exception as e:
            fail_count += 1
            error_type = classify_error(str(e))
            error_msg = f"[{error_type}: {e}]"
            blocks.append(format_entry_block(i, entry, error_msg))
            failed_items.append({"title": entry["title"], "id": entry["id"], "error": error_type})
            print(f"  FAILED: {e}")
            traceback.print_exc()
            print()
            if args.stop_on_failure:
                print("Stopping early due to --stop-on-failure.")
                break

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    header = (
        f"YouTube Playlist Transcripts\n"
        f"Playlist: {args.playlist_url}\n"
        f"Videos: {success_count} transcribed, {fail_count} failed\n"
        f"{'=' * 60}\n\n"
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(blocks))
        if failed_items:
            f.write("\nFailed videos:\n")
            for item in failed_items:
                f.write(f"- {item['id']} | {item['error']} | {item['title']}\n")

    print(f"\nDone! {success_count} transcribed, {fail_count} failed.")
    if failed_items:
        print("Failed items summary:")
        for item in failed_items:
            print(f"  - {item['id']} [{item['error']}] {item['title']}")
    print(f"Output saved to: {output_path.resolve()}")


if __name__ == "__main__":
    main()
