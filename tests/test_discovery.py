import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import product_discovery as pd
import discovery_runner as runner
import parser_local_v8 as v8

URL = 'https://www.komputronik.pl/product/123/example.html'


def item(**changes):
    value = dict(zip(runner.FIELDS, [URL, 'Example', '599.99', 'PLN', 'InStock', '0012345678901', 'SKU1', 'MPN1', '']))
    value.update(changes)
    return value


def html(node):
    return '<script type="application/ld+json">' + json.dumps(node) + '</script>'


def test_identity_ignores_slug_host_and_tracking():
    a = pd.product_keys(item())
    b = pd.product_keys({'url': 'http://komputronik.pl/product/123/new-name.html?ref=123'})
    assert a & b == {'catalog:123'}


def test_identifier_namespaces_do_not_collide():
    assert not (pd.product_keys({'url': URL, 'ean': '123'}) &
                pd.product_keys({'url': URL.replace('/123/', '/124/'), 'sku': '123'}))


@pytest.mark.parametrize('price', ['NaN', 'Infinity', '-1', None, 'broken'])
def test_invalid_prices_rejected(price):
    assert pd.parse_product(html({'@type': 'Product', 'offers': {'price': price}}), URL) is None


def test_graph_and_decimal_price():
    parsed = pd.parse_product(html({'@graph': [{'@type': ['Thing', 'Product'], 'name': 'Tool',
                       'offers': {'price': '1 234,50', 'priceCurrency': 'PLN'}}]}), URL)
    assert parsed['price_pln'] == '1234.50'


def test_outbox_survives_restart_and_deduplicates(tmp_path):
    db = tmp_path / 'outbox.sqlite3'
    box = runner.Outbox(db)
    box.add(item())
    box.add(item(name='Updated'))
    other = runner.Outbox(db)
    assert len(other.items()) == 1
    assert other.items()[0]['name'] == 'Updated'
    other.export(tmp_path / 'out.csv')
    assert 'Updated' in (tmp_path / 'out.csv').read_text(encoding='utf-8-sig')


class Source:
    def col_values(self, column):
        assert column == 2
        return [URL]  # Live sheet starts with product data, no header.


class Destination:
    title = 'product_discovery'
    row_count = 1000
    col_count = 12

    def __init__(self):
        self.rows = []
        self.timed_out = False
        self.calls = []

    def get_all_values(self):
        return self.rows

    def update(self, *, range_name, values, value_input_option='RAW'):
        assert value_input_option == 'RAW'
        self.calls.append(range_name)
        start = int(range_name.split(':')[0][1:]) - 1
        self.rows[start:start + len(values)] = values
        if start > 0 and not self.timed_out:
            self.timed_out = True
            raise TimeoutError('Server succeeded; client timed out')


class Book:
    def __init__(self, destination):
        self.destination = destination

    def worksheet(self, title):
        assert title in ('k_1', 'old')
        return Source()

    def worksheets(self):
        return [self.destination]


def test_first_source_row_is_included():
    assert 'catalog:123' in runner.source_keys(Book(Destination()))


@pytest.mark.parametrize('empty_rows', [[], [[]], [['']], [[''] * 9, []]])
def test_publish_retry_and_rerun_do_not_duplicate(empty_rows):
    dest = Destination()
    dest.rows = empty_rows
    book = Book(dest)
    new = item(url=URL.replace('/123/', '/456/'), name='=IMPORTXML("bad")')
    with patch.object(runner.time, 'sleep'):
        assert runner.publish(book, [item(), new, new]) == 1
        assert len(dest.rows) == 2
        assert dest.calls.count('A2:L2') == 2
        assert runner.publish(book, [new]) == 0
    assert dest.rows[1][1] == new['name']


def test_unexpected_headers_are_preserved():
    dest = Destination()
    dest.rows = [['manual data']]
    with pytest.raises(ValueError):
        runner.publish(Book(dest), [item()])
    assert dest.rows == [['manual data']]


def test_publish_refuses_source_tab():
    with pytest.raises(ValueError):
        runner.publish(None, [], title='k_1')


def test_price_parser_failed_batch_is_durable(tmp_path, monkeypatch):
    monkeypatch.setenv('PARSER_PENDING_FILE', str(tmp_path / 'pending.json'))
    class Failing:
        def update(self, **kwargs):
            raise TimeoutError()
    writer = v8.BatchWriter(Failing())
    writer.add(2, {'name': 'Device', 'price': 999})
    with pytest.raises(RuntimeError):
        writer.flush()
    restored = v8.BatchWriter(Failing())
    assert restored._pending[2][0] == 'Device'
    class Good:
        def update(self, **kwargs):
            assert kwargs['range_name'] == 'F2:H2'
    restored._sheet = Good()
    restored.flush()
    assert not restored._pending
    assert json.loads((tmp_path / 'pending.json').read_text()) == {}


def test_restriction_stops_without_retry(monkeypatch):
    class Response:
        status_code = 403
        headers = {}
    calls = []
    def request(*args, **kwargs):
        calls.append(args)
        return Response()
    monkeypatch.setattr(runner.requests, 'get', request)
    transport = runner.Transport(1)
    with pytest.raises(runner.Restricted):
        transport.get(URL)
    with pytest.raises(runner.Restricted):
        transport.get(URL)
    assert len(calls) == 1


def test_sitemap_only_product_urls_and_no_hidden_limit():
    class Response:
        content = b'<urlset><url><loc>https://www.komputronik.pl/category/test</loc></url><url><loc>https://www.komputronik.pl/product/123/test.html</loc></url></urlset>'
        def raise_for_status(self):
            pass
    class Session:
        def get(self, *args, **kwargs):
            return Response()
    assert list(pd.crawl_sitemaps(Session(), '')) == [URL.replace('example', 'test')]


def test_sitemap_failure_is_visible():
    class Session:
        def get(self, *args, **kwargs):
            raise pd.requests.exceptions.RequestException('temporary failure')
    with pytest.raises(RuntimeError, match='incomplete scan'):
        list(pd.crawl_sitemaps(Session(), ''))


def test_full_bounded_pipeline_filters_and_persists(tmp_path, monkeypatch, capsys):
    class Response:
        def __init__(self, content):
            self.content = content.encode()
            self.text = content
        def raise_for_status(self):
            pass
    class Transport:
        def __init__(self, delay):
            self.delay = delay
        def get(self, url, **kwargs):
            if url.endswith('/robots.txt'):
                return Response('User-agent: *\nAllow: /\n')
            if url.endswith('/sitemap.xml'):
                return Response('<urlset>' + ''.join('<url><loc>' + URL.replace('/123/', '/' + str(i) + '/') + '</loc></url>' for i in (123, 124, 125, 126)) + '</urlset>')
            price = {'124': '500', '125': '500.01', '126': '900'}[pd.catalog_id(url)]
            currency = 'EUR' if pd.catalog_id(url) == '126' else 'PLN'
            return Response(html({'@type': 'Product', 'name': 'Device', 'offers': {'price': price, 'priceCurrency': currency}}))
    existing = tmp_path / 'existing.csv'
    existing.write_text('url\n' + URL + '\n')
    db = tmp_path / 'db.sqlite3'
    output = tmp_path / 'candidates.csv'
    monkeypatch.setattr(runner, 'Transport', Transport)
    monkeypatch.setattr(sys, 'argv', ['discovery', '--existing', str(existing), '--max-pages', '3', '--database', str(db), '--output', str(output)])
    runner.main()
    result = json.loads(capsys.readouterr().out)
    assert result['scanned'] == 3 and result['new_candidates'] == 1
    assert runner.Outbox(db).items()[0]['price_pln'] == '500.01'


def test_html_sitemap_category_fallback_excludes_facets():
    class Response:
        def __init__(self, content):
            self.content = content.encode()
        def raise_for_status(self):
            pass
    calls = []
    class Session:
        def get(self, url, **kwargs):
            calls.append(url)
            if url.endswith('sitemap.xml'):
                return Response('<!doctype html><a href="/category/1/test.html">Category</a><a href="/category/1/test.html?brand=2">Facet</a>')
            return Response('<!doctype html><a href="' + URL + '">Product</a>')
    assert list(pd.crawl_sitemaps(Session(), '')) == [URL]
    assert len(calls) == 2


def test_automatic_price_batch_failure_does_not_escape(tmp_path, monkeypatch):
    monkeypatch.setenv('PARSER_PENDING_FILE', str(tmp_path / 'pending.json'))
    class Failing:
        def update(self, **kwargs):
            raise TimeoutError()
    writer = v8.BatchWriter(Failing(), batch_size=1)
    writer.add(2, {'name': 'Success', 'price': 999})
    assert writer._pending[2][0] == 'Success'


def test_category_comes_from_breadcrumbs():
    breadcrumb = {'@type': 'BreadcrumbList', 'itemListElement': [
        {'position': 2, 'item': {'@id': '/category/2/laptops.html', 'name': 'Laptopy'}},
        {'position': 1, 'item': {'@id': '/category/1/computers.html', 'name': 'Komputery'}}]}
    product = {'@type': 'Product', 'name': 'Device', 'offers': {'price': 501, 'priceCurrency': 'PLN'}}
    result = pd.parse_product(html(breadcrumb) + html(product), URL)
    assert result['category'] == 'Laptopy'
    assert result['category_path'] == 'Komputery > Laptopy'
    assert result['category_url'] == 'https://www.komputronik.pl/category/2/laptops.html'


def test_old_is_excluded_independently():
    class Tab:
        def __init__(self, values):
            self.values = values
        def col_values(self, column):
            return self.values
    class Both:
        def worksheet(self, title):
            return Tab([URL] if title == 'old' else [])
    assert 'catalog:123' in runner.source_keys(Both())


def test_durable_queue_recovers_inflight_jobs(tmp_path):
    from catalog_worker import Queue
    q = Queue(tmp_path / 'queue.sqlite3')
    q.add('product', URL)
    q.add('product', URL)
    assert q.take('product', 4) == [URL]
    recovered = Queue(tmp_path / 'queue.sqlite3')
    assert recovered.take('product') == [URL]
    recovered.finish('product', URL)
    assert recovered.take('product') == []
    assert recovered.stats()['product_done'] == 1


def test_catalog_index_follows_real_pagination_and_excludes_facets():
    from catalog_worker import index_links
    doc = ('<!doctype html><a href="/category/5022/laptopy.html?p=2">Next</a>'
           '<a href="/category/5022/laptopy.html?brand=1">Facet</a>'
           '<a href="' + URL + '">Product</a>')
    indexes, products = index_links(doc.encode(), pd.BASE + '/sitemap')
    assert indexes == {pd.BASE + '/category/5022/laptopy.html?p=2'}
    assert products == {URL}


def test_existing_nine_columns_gain_categories_only():
    class Dest(Destination):
        col_count = 9
        def resize(self, **kwargs):
            self.col_count = kwargs['cols']
        def batch_update(self, changes, **kwargs):
            self.changes = changes
            for change in changes:
                row = int(change['range'].split(':')[0][1:]) - 1
                self.rows[row][9:12] = change['values'][0]
        def update(self, **kwargs):
            if kwargs['range_name'] == 'J1:L1':
                self.rows[0][9:12] = kwargs['values'][0]
                return
            super().update(**kwargs)
    dest = Dest()
    old_values = [item().get(f, '') for f in runner.FIELDS[:9]]
    dest.rows = [runner.FIELDS[:9], old_values.copy()]
    enriched = item(category='Laptopy', category_path='PC > Laptopy', category_url=pd.BASE+'/category/2/laptopy.html')
    assert runner.publish(Book(dest), [enriched]) == 0
    assert dest.rows[1][:9] == old_values
    assert dest.rows[1][9] == 'Laptopy'
    assert dest.rows[0] == runner.FIELDS


def test_full_worker_excludes_both_sources_and_resumes(tmp_path, monkeypatch):
    import catalog_worker as worker
    monkeypatch.setenv('DISCOVERY_DATA_DIR', str(tmp_path))
    class Response:
        def __init__(self, text):
            self.text, self.content = text, text.encode()
    requests = []
    class Transport:
        def __init__(self, delay):
            self.delay = delay
        def get(self, url):
            requests.append(url)
            if url.endswith('/robots.txt'):
                return Response('User-agent: *\nAllow: /')
            if url.endswith('/sitemap.xml'):
                return Response('<!doctype html>' + ''.join('<a href="'+URL.replace('/123/', f'/{i}/')+'">Product</a>' for i in (123,124,125,126)))
            price = '500' if pd.catalog_id(url)=='125' else '501'
            return Response(html({'@type':'Product','name':'New','offers':{'price':price,'priceCurrency':'PLN'}}))
    class Tab:
        def __init__(self, url):
            self.url = url
        def col_values(self, column):
            return [self.url]
    class Book:
        def worksheet(self, title):
            return Tab(URL if title=='k_1' else URL.replace('/123/','/124/'))
    class Client:
        def open(self, title):
            return Book()
    monkeypatch.delenv('SPREADSHEET_ID', raising=False)
    monkeypatch.setattr(worker,'get_sheets_client',Client)
    monkeypatch.setattr(worker,'Transport',Transport)
    published=[]
    monkeypatch.setattr(worker,'publish',lambda book,items: published.extend(items) or len(items))
    worker.run()
    box=worker.Outbox(tmp_path/'candidates.sqlite3')
    assert [pd.catalog_id(i['url']) for i in box.items()]==['126']
    assert not any('/123/' in u or '/124/' in u for u in requests)
    requests.clear()
    worker.run()
    assert requests == [pd.BASE+'/robots.txt']
    assert worker.Queue(tmp_path/'catalog.sqlite3').stats()['product_done']==2

