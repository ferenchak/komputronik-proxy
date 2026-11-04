#!/usr/bin/env python3
"""Komputronik Product Discovery: sitemap -> product JSON-LD -> CSV shortlist.

Install: pip install -r requirements-discovery.txt
Example:
 python product_discovery.py --existing existing.csv --output candidates.csv --max-pages 100

Default: read-only Sheets access; --publish writes only the discovery tab.
The CSV may contain URL, EAN/GTIN, SKU, or product model columns.
"""
import argparse
import csv
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse, urldefrag, urljoin, parse_qs
from urllib.robotparser import RobotFileParser

from curl_cffi import requests
from bs4 import BeautifulSoup

BASE = 'https://www.komputronik.pl'
USER_AGENT = 'ProductDiscovery/0.1 (catalog research; contact site owner for bulk access)'


def normalize_url(url):
    value = urldefrag((url or '').strip())[0].split('?')[0].rstrip('/')
    parsed = urlparse(value)
    if parsed.hostname in ('komputronik.pl', 'www.komputronik.pl'):
        return 'https://www.komputronik.pl' + parsed.path.rstrip('/')
    return value


def catalog_id(url):
    parsed = urlparse(url)
    if parsed.hostname not in ('komputronik.pl', 'www.komputronik.pl'):
        return ''
    match = re.fullmatch(r'/product/(\d+)(?:/[^/]*)?/?', parsed.path)
    return match.group(1) if match else ''


def normalize_id(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').lower())


def existing_keys(path):
    keys = set()
    if not path:
        return keys
    with open(path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            for col, val in row.items():
                key = (col or '').strip().lower()
                if key in ('url', 'link', 'product_url', 'посилання') and val:
                    keys.update(product_keys({'url': val}))
                elif key in ('ean', 'gtin', 'gtin13', 'sku', 'mpn', 'model', 'модель', 'артикул') and val:
                    namespace = {'gtin': 'ean', 'gtin13': 'ean', 'артикул': 'sku'}.get(key, key)
                    if namespace in ('ean', 'sku', 'mpn'):
                        keys.add(namespace + ':' + str(val).strip().casefold())
    return keys


def json_nodes(data):
    if isinstance(data, list):
        for item in data:
            yield from json_nodes(item)
    elif isinstance(data, dict):
        yield data
        if '@graph' in data:
            yield from json_nodes(data['@graph'])


def parse_product(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for node in json_nodes(data):
            typ = node.get('@type', '')
            if 'Product' not in (typ if isinstance(typ, list) else [typ]):
                continue
            offers = node.get('offers') or {}
            if isinstance(offers, list):
                offers = next((o for o in offers if isinstance(o, dict)), {})
            if not isinstance(offers, dict):
                continue
            raw_price = offers.get('price')
            if raw_price is None and isinstance(offers.get('priceSpecification'), dict):
                raw_price = offers['priceSpecification'].get('price')
            try:
                price = Decimal(str(raw_price).replace(' ', '').replace(',', '.'))
            except (InvalidOperation, TypeError):
                continue
            if not price.is_finite() or price < 0:
                continue
            image = node.get('image') or ''
            if isinstance(image, list):
                image = image[0] if image else ''
            if isinstance(image, dict):
                image = image.get('url', '')
            return {
                'url': normalize_url(url), 'name': node.get('name', ''),
                'price_pln': str(price), 'currency': offers.get('priceCurrency', ''),
                'availability': offers.get('availability', ''),
                'ean': node.get('gtin13') or node.get('gtin') or '',
                'sku': node.get('sku', ''), 'mpn': node.get('mpn', ''),
                'image': image,
            }
    return None


def product_keys(item):
    keys = {'url:' + normalize_url(item['url'])}
    if catalog_id(item['url']):
        keys.add('catalog:' + catalog_id(item['url']))
    for field in ('ean', 'sku', 'mpn'):
        if item.get(field):
            keys.add(field + ':' + str(item[field]).strip().casefold())
    return keys


def crawl_sitemaps(session, robots, max_sitemaps=100, policy=None):
    seeds = []
    for line in robots.splitlines():
        if line.lower().startswith('sitemap:'):
            seeds.append(line.split(':', 1)[1].strip())
    if not seeds:
        seeds = [BASE + '/sitemap.xml']
    queue = deque(dict.fromkeys(seeds))
    seen = set()
    while queue and len(seen) < max_sitemaps:
        url = queue.popleft()
        if url in seen or urlparse(url).hostname not in ('komputronik.pl', 'www.komputronik.pl'):
            continue
        seen.add(url)
        if policy is not None and not policy.can_fetch(USER_AGENT, url):
            raise RuntimeError('Sitemap disallowed by robots.txt')
        try:
            resp = session.get(url, timeout=25)
            resp.raise_for_status()
            if resp.content.lstrip().lower().startswith((b'<!doctype html', b'<html')):
                soup = BeautifulSoup(resp.content, 'html.parser')
                products = []
                indexes = []
                for anchor in soup.select('a[href], link[rel="next"][href]'):
                    target = urljoin(url, anchor['href'])
                    parsed = urlparse(target)
                    if parsed.hostname not in ('komputronik.pl', 'www.komputronik.pl'):
                        continue
                    if catalog_id(target):
                        products.append(normalize_url(target))
                    elif parsed.path.endswith(('.xml', '.xml.gz')):
                        indexes.append(target)
                    elif parsed.path.startswith('/category/'):
                        # Follow unfiltered categories and pagination; avoid faceted
                        # price/brand/sort combinations that explode crawl size.
                        if set(parse_qs(parsed.query)) <= {'page', 'p', 'pageNumber'}:
                            indexes.append(target.split('#', 1)[0])
                if not products and not indexes:
                    raise RuntimeError('HTML catalog has no product/category links')
                yield from dict.fromkeys(products)
                queue.extend(dict.fromkeys(indexes))
                continue
            root = ET.fromstring(resp.content)
        except (requests.exceptions.RequestException, ET.ParseError) as e:
            raise RuntimeError('Sitemap unavailable; incomplete scan') from None
        tag = root.tag.rsplit('}', 1)[-1]
        locs = [el.text.strip() for el in root.iter() if el.tag.rsplit('}', 1)[-1] == 'loc' and el.text]
        if tag == 'sitemapindex':
            queue.extend(locs)
        elif tag == 'urlset':
            for loc in locs:
                if urlparse(loc).hostname in ('komputronik.pl', 'www.komputronik.pl'):
                    if catalog_id(loc):
                        yield loc
    if queue:
        raise RuntimeError('Sitemap limit reached; incomplete scan')


if __name__ == '__main__':
    # Keep parsing helpers independently testable; use the v8-based runner.
    from discovery_runner import main as run
    run()
