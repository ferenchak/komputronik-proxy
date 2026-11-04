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
        assert title == 'k_1'
        return Source()

    def worksheets(self):
        return [self.destination]


def test_first_source_row_is_included():
    assert 'catalog:123' in runner.source_keys(Book(Destination()))


def test_publish_retry_and_rerun_do_not_duplicate():
    dest = Destination()
    book = Book(dest)
    new = item(url=URL.replace('/123/', '/456/'), name='=IMPORTXML("bad")')
    with patch.object(runner.time, 'sleep'):
        assert runner.publish(book, [item(), new, new]) == 1
        assert len(dest.rows) == 2
        assert dest.calls.count('A2:I2') == 2
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
