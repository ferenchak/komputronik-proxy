"""v8-based transport, bounded workers, durable outbox and separate Sheets output."""
import argparse
import csv
import gzip
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser

import gspread
from curl_cffi import requests
from parser_local_v8 import (get_sheets_client, IMPERSONATE_PROFILE, CONNECT_TIMEOUT_S,
                             READ_TIMEOUT_S, SPREADSHEET_NAME, WORKSHEET_NAME, URL_COLUMN_INDEX)
from product_discovery import (BASE, USER_AGENT, catalog_id, product_keys, parse_product,
                               existing_keys, normalize_url, crawl_sitemaps)

FIELDS = ['url', 'name', 'price_pln', 'currency', 'availability', 'ean', 'sku', 'mpn', 'image']


class Restricted(RuntimeError):
    pass


class Transport:
    def __init__(self, delay):
        self.delay = max(1, delay)
        self.lock = threading.Lock()
        self.last = 0
        self.stopped = threading.Event()

    def get(self, url, _redirects=0, **unused):
        if urlparse(url).hostname not in ('komputronik.pl', 'www.komputronik.pl') or urlparse(url).scheme != 'https':
            raise ValueError('Only HTTPS Komputronik URLs allowed')
        proxy = os.environ.get('WEBSHARE_PROXY_URL')
        for attempt in range(3):
            with self.lock:
                if self.stopped.is_set():
                    raise Restricted('Run stopped after access restriction')
                time.sleep(max(0, self.last + self.delay - time.monotonic()))
                self.last = time.monotonic()
            try:
                resp = requests.get(url, impersonate=IMPERSONATE_PROFILE,
                                    headers={'User-Agent': USER_AGENT, 'Accept-Language': 'pl-PL'},
                                    proxies={'http': proxy, 'https': proxy} if proxy else None,
                                    timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S), allow_redirects=False)
                if resp.status_code in (403, 429) or resp.headers.get('cf-mitigated') == 'challenge':
                    self.stopped.set()
                    raise Restricted(f'Access restricted: HTTP {resp.status_code}')
                resp.raise_for_status()
                if 300 <= resp.status_code < 400:
                    location = resp.headers.get('location')
                    if not location or _redirects >= 3:
                        raise RuntimeError('Invalid redirect chain')
                    # Reapply host/scheme allowlist and global rate limit at each hop.
                    return self.get(urljoin(url, location), _redirects=_redirects + 1)
                if resp.content.startswith(b'\x1f\x8b'):
                    resp._content = gzip.decompress(resp.content)
                return resp
            except Restricted:
                raise
            except Exception:
                if attempt == 2:
                    raise RuntimeError('Request failed after retries') from None
                time.sleep(2 ** attempt)


class Outbox:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS candidates (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def add(self, item):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO candidates VALUES (?,?)',
                            (catalog_id(item['url']) or normalize_url(item['url']), json.dumps(item, ensure_ascii=False)))

    def items(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM candidates ORDER BY id')]

    def export(self, path):
        path = Path(path)
        temporary = path.with_suffix(path.suffix + '.tmp')
        with temporary.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(self.items())
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(path)


def retry(operation):
    for attempt in range(3):
        try:
            return operation()
        except Exception:
            if attempt == 2:
                raise RuntimeError('Sheets operation failed; local outbox retained') from None
            time.sleep(2 ** attempt)


def source_keys(book):
    sheet = retry(lambda: book.worksheet(WORKSHEET_NAME))
    urls = retry(lambda: sheet.col_values(URL_COLUMN_INDEX))
    keys = set()
    for url in urls:
        keys.update(product_keys({'url': url}))
    return keys


def publish(book, items, title='product_discovery'):
    if title == WORKSHEET_NAME:
        raise ValueError('Cannot publish into source sheet')
    sheets = retry(book.worksheets)
    sheet = next((s for s in sheets if s.title == title), None)
    if sheet is None:
        # On uncertain create outcome a rerun discovers the created sheet.
        sheet = book.add_worksheet(title=title, rows=max(1000, len(items) + 1), cols=9)
    rows = retry(sheet.get_all_values)
    # gspread returns [[]] for a newly created empty worksheet.
    # Remove only empty trailing rows; preserve every populated existing row.
    while rows and not any(value != '' for value in rows[-1]):
        rows.pop()
    if rows and rows[0] != FIELDS:
        raise ValueError('Unexpected destination headers; refusing overwrite')
    known = source_keys(book)
    for row in rows[1:]:
        known.update(product_keys(dict(zip(FIELDS, row))))
    pending = []
    for item in items:
        keys = product_keys(item)
        if not keys & known:
            pending.append([item.get(f, '') for f in FIELDS])
            known.update(keys)
    if not rows:
        retry(lambda: sheet.update(range_name='A1:I1', values=[FIELDS], value_input_option='RAW'))
    start = max(2, len(rows) + 1)
    if start + len(pending) - 1 > sheet.row_count:
        retry(lambda: sheet.resize(rows=start + len(pending) - 1))
    for offset in range(0, len(pending), 50):
        chunk = pending[offset:offset + 50]
        first = start + offset
        # Fixed ranges are idempotent on success followed by a response timeout.
        retry(lambda: sheet.update(range_name=f'A{first}:I{first + len(chunk) - 1}',
                                   values=chunk, value_input_option='RAW'))
    return len(pending)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--existing', help='Existing CSV; otherwise read k_1 column B')
    p.add_argument('--output', default='product_candidates.csv')
    p.add_argument('--database', default='product_discovery.sqlite3')
    p.add_argument('--min-price', type=Decimal, default=Decimal('500'))
    p.add_argument('--max-pages', type=int, default=20, help='0 = all sitemap product URLs')
    p.add_argument('--max-sitemaps', type=int, default=1000)
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--delay', type=float, default=2.0)
    p.add_argument('--publish', action='store_true')
    p.add_argument('--publish-only', action='store_true')
    args = p.parse_args()
    if args.workers < 1 or args.max_pages < 0 or args.max_sitemaps < 1 or args.delay < 0 or not args.min_price.is_finite():
        p.error('Invalid limits')
    box = Outbox(args.database)
    client = get_sheets_client() if not args.existing or args.publish or args.publish_only else None
    book = retry(lambda: client.open_by_key(os.environ['SPREADSHEET_ID']) if os.environ.get('SPREADSHEET_ID')
                 else client.open(SPREADSHEET_NAME)) if client else None
    known = existing_keys(args.existing) if args.existing else source_keys(book)
    for item in box.items():
        known.update(product_keys(item))
    scanned = saved = failed = 0
    try:
        if not args.publish_only:
            transport = Transport(args.delay)
            r = transport.get(BASE + '/robots.txt')
            robots = RobotFileParser()
            robots.parse(r.text.splitlines())
            transport.delay = max(transport.delay, robots.crawl_delay(USER_AGENT) or 0)
            rate = robots.request_rate(USER_AGENT)
            if rate:
                transport.delay = max(transport.delay, rate.seconds / rate.requests)
            seen = set()

            def urls():
                for url in crawl_sitemaps(transport, r.text, args.max_sitemaps, robots):
                    identity = catalog_id(url)
                    if identity in seen or product_keys({'url': url}) & known or not robots.can_fetch(USER_AGENT, url):
                        continue
                    seen.add(identity)
                    yield url
                    if args.max_pages and len(seen) >= args.max_pages:
                        return

            def scan(url):
                try:
                    return parse_product(transport.get(url).text, url)
                except Restricted:
                    raise
                except Exception:
                    return None

            source = iter(urls())
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                while True:
                    window = []
                    for _ in range(args.workers):
                        url = next(source, None)
                        if url is None:
                            break
                        window.append(pool.submit(scan, url))
                    if not window:
                        break
                    for future in window:
                        item = future.result()
                        scanned += 1
                        if item is None:
                            failed += 1
                        elif item['name'] and item['currency'].upper() == 'PLN' and Decimal(item['price_pln']) > args.min_price and not product_keys(item) & known:
                            box.add(item)
                            known.update(product_keys(item))
                            saved += 1
    finally:
        box.export(args.output)
    count = publish(book, box.items()) if args.publish or args.publish_only else 0
    print(json.dumps(dict(scanned=scanned, new_candidates=saved, failed=failed, published=count,
                          scope='bounded_sample' if args.max_pages else 'sitemap_pass',
                          complete_catalog_verified=False)))
    if failed:
        raise SystemExit(2)


if __name__ == '__main__':
    main()

