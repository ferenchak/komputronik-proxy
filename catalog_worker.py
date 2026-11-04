"""One resumable catalog pass. Persistent queue/outbox live under DISCOVERY_DATA_DIR."""
import json
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from discovery_runner import Transport, Restricted, Outbox, publish, source_keys, retry
from parser_local_v8 import get_sheets_client, SPREADSHEET_NAME
from product_discovery import BASE, USER_AGENT, catalog_id, parse_product, product_keys, normalize_url


class Queue:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (kind TEXT, url TEXT, state TEXT DEFAULT "pending", attempts INTEGER DEFAULT 0, error TEXT DEFAULT "", PRIMARY KEY(kind,url))')
        self.db.execute('CREATE TABLE IF NOT EXISTS status (key TEXT PRIMARY KEY, value TEXT)')
        with self.db:
            self.db.execute('UPDATE jobs SET state="pending" WHERE state="running"')

    def add(self, kind, url):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO jobs(kind,url) VALUES (?,?)', (kind,url))

    def take(self, kind, limit=1):
        with self.db:
            urls = [r[0] for r in self.db.execute('SELECT url FROM jobs WHERE kind=? AND state="pending" ORDER BY rowid LIMIT ?', (kind,limit))]
            self.db.executemany('UPDATE jobs SET state="running", attempts=attempts+1 WHERE kind=? AND url=?', [(kind,u) for u in urls])
        return urls

    def finish(self, kind, url, error=''):
        with self.db:
            self.db.execute('UPDATE jobs SET state=CASE WHEN ?="" THEN "done" WHEN attempts<3 THEN "pending" ELSE "failed" END,error=? WHERE kind=? AND url=?', (error,error,kind,url))

    def stats(self):
        return {kind + '_' + state: count for kind,state,count in self.db.execute('SELECT kind,state,count(*) FROM jobs GROUP BY kind,state')}

    def status(self, state):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO status VALUES ("run_state",?)', (state,))


def index_links(content, url):
    indexes, products = set(), set()
    if content.lstrip().lower().startswith((b'<!doctype html', b'<html')):
        soup = BeautifulSoup(content, 'html.parser')
        for a in soup.select('a[href],link[rel="next"][href]'):
            target = urljoin(url, a['href'])
            p = urlsplit(target)
            if p.hostname not in ('komputronik.pl','www.komputronik.pl') or p.scheme != 'https':
                continue
            if catalog_id(target):
                products.add(normalize_url(target))
            elif p.path.endswith(('.xml','.xml.gz')):
                indexes.add(target)
            elif p.path.startswith('/category/') and ',' not in p.path:
                query = parse_qs(p.query)
                if set(query) <= {'p','page','pageNumber'}:
                    indexes.add(urlunsplit(('https','www.komputronik.pl',p.path,urlencode(sorted((k,v[0]) for k,v in query.items())),'')))
    else:
        root = ET.fromstring(content)
        locs = [n.text.strip() for n in root.iter() if n.tag.rsplit('}',1)[-1]=='loc' and n.text]
        if root.tag.rsplit('}',1)[-1]=='sitemapindex':
            indexes.update(locs)
        elif root.tag.rsplit('}',1)[-1]=='urlset':
            products.update(normalize_url(u) for u in locs if catalog_id(u))
        else:
            raise ValueError('Unrecognized sitemap document')
    if not indexes and not products:
        raise ValueError('No crawlable catalog links')
    return indexes, products


def run():
    directory = Path(os.environ.get('DISCOVERY_DATA_DIR','/data'))
    directory.mkdir(parents=True, exist_ok=True)
    queue = Queue(directory/'catalog.sqlite3')
    box = Outbox(directory/'candidates.sqlite3')
    book = None
    if os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON') or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
        client = get_sheets_client()
        key = os.environ.get('SPREADSHEET_ID')
        book = retry(lambda: client.open_by_key(key) if key else client.open(SPREADSHEET_NAME))
        known = source_keys(book)
    elif os.environ.get('DISCOVERY_PUBLIC_CATALOG_ONLY') == '1':
        known = set()
        print(json.dumps({'sheets_sync':'disabled_no_credentials','source_filtering':'deferred_until_google_sync','scan_scope':'public_catalog_only'}),flush=True)
    else:
        raise RuntimeError('Provide Google credentials or enable public-catalog-only mode')
    candidate_keys = set()
    for item in box.items():
        candidate_keys.update(product_keys(item))
    transport = Transport(float(os.environ.get('DISCOVERY_DELAY','2')))
    robots_response = transport.get(BASE+'/robots.txt')
    robots = RobotFileParser()
    robots.parse(robots_response.text.splitlines())
    transport.delay = max(transport.delay, robots.crawl_delay(USER_AGENT) or 0)
    rate = robots.request_rate(USER_AGENT)
    if rate:
        transport.delay=max(transport.delay,rate.seconds/rate.requests)
    seeds=[line.split(':',1)[1].strip() for line in robots_response.text.splitlines() if line.lower().startswith('sitemap:')]
    for seed in seeds or [BASE+'/sitemap.xml']:
        queue.add('index',seed)
    queue.status('running')
    workers = int(os.environ.get('DISCOVERY_WORKERS','4'))
    last_publish, last_log = 0, 0
    pending_publish = True

    def log(state):
        print(json.dumps({'state':state,**queue.stats(),'candidates':len(box.items())},ensure_ascii=False),flush=True)

    def scan(url):
        try:
            item=parse_product(transport.get(url).text,url)
            return url,item,'' if item else 'product_json_missing'
        except Restricted:
            raise
        except Exception:
            return url,None,'request_or_parse_failed'

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while True:
                if book is not None and pending_publish and time.monotonic()-last_publish>=90:
                    try:
                        count=publish(book,box.items())
                        print(json.dumps({'published_now':count}),flush=True)
                        pending_publish=False
                    except Exception as error:
                        print(json.dumps({'publish_error':type(error).__name__,'outbox_retained':True}),flush=True)
                    last_publish=time.monotonic()
                urls=queue.take('product',workers)
                futures=[pool.submit(scan,url) for url in urls]
                restriction=None
                for future in futures:
                    try:
                        url,item,error=future.result()
                        if item and item.get('name') and str(item.get('currency','')).upper()=='PLN' and Decimal(item['price_pln'])>Decimal('500') and not product_keys(item)&known:
                            box.add(item)
                            candidate_keys.update(product_keys(item))
                            pending_publish=True
                        queue.finish('product',url,error)
                    except Restricted as error:
                        restriction=error
                if restriction:
                    raise restriction
                indexes=queue.take('index') if not urls else []
                for url in indexes:
                    if not robots.can_fetch(USER_AGENT,url):
                        queue.finish('index',url,'robots_disallowed')
                        continue
                    try:
                        response=transport.get(url)
                        next_indexes, products=index_links(response.content,url)
                        for target in next_indexes:
                            if robots.can_fetch(USER_AGENT,target):
                                queue.add('index',target)
                        for target in products:
                            if robots.can_fetch(USER_AGENT,target) and not product_keys({'url':target})&(known|candidate_keys):
                                queue.add('product',target)
                        queue.finish('index',url)
                    except Restricted:
                        raise
                    except Exception:
                        queue.finish('index',url,'request_or_index_parse_failed')
                if time.monotonic()-last_log>=30:
                    log('running'); last_log=time.monotonic()
                if not urls and not indexes:
                    break
        failures=sum(v for k,v in queue.stats().items() if k.endswith('_failed'))
        state='finished_with_failures' if failures else 'finished'
        queue.status(state)
        log(state)
    except Restricted:
        queue.status('blocked_by_site')
        log('blocked_by_site')
        # Deliberately no automatic retry of an access restriction.
        raise SystemExit(3)
    finally:
        box.export(directory/'product_candidates.csv')
        if book is not None and box.items():
            try:
                print(json.dumps({'final_published':publish(book,box.items())}),flush=True)
            except Exception as error:
                print(json.dumps({'final_publish_error':type(error).__name__,'outbox_retained':True}),flush=True)


if __name__=='__main__':
    run()
