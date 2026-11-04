# Product Discovery

Implemented from uploaded parser_local_v8.py, not the old main.py service.

- Dedicated entry point: product_discovery.py / discovery_runner.py.
- Uses v8 curl_cffi profile, timeouts and Google service-account client.
- Reads Price_and_avail_parsing / k_1 column B, including row 1: live inspection
  on 2026-11-04 confirmed row 1 is a product, not a header.
- Default threshold: strictly above 500 PLN. Unknown currencies are excluded.
- Source sheet is read-only. Explicit --publish writes product_discovery only.
- Identity: Komputronik product ID, normalized URL, separate EAN/SKU/MPN namespaces.
- SQLite outbox commits each candidate before Sheets. Fixed-range RAW writes
  can be retried after ambiguous timeouts. A rerun reads the destination first.
- Run only one discovery writer per database/destination. Existing output rows
  are retained, including manual decisions. Outbox entries are discovery
  snapshots; --publish-only does not revalidate current prices/availability.
- v8 proxy secrets moved to environment variables. The previous password still
  needs rotation by the owner in Webshare. No password rotation was performed.
- v8 BatchWriter now journals pending rows on disk and retains failed batches.
  Use a separate PARSER_PENDING_FILE per price-parser process. Do not reuse
  journals for another spreadsheet. Price parser's normal F-H behavior remains.

## Run

```bash
python3 -m pip install -r requirements-discovery.txt
# Set environment variables from .env.discovery.example in your process.
# Variables are not loaded automatically from a .env file.
python3 product_discovery.py --max-pages 20
python3 product_discovery.py --max-pages 20 --publish
python3 product_discovery.py --publish-only
python3 product_discovery.py --max-pages 0 --publish
```

The default reads the live assortment via the service account. A read-only
offline assortment may instead be supplied with --existing existing.csv
(column `url`, optionally `ean`, `sku`, `mpn`). CSV matching intentionally does
not treat model descriptions as exact identifiers.

Discovery honors robots.txt and a global request interval across all workers.
403/429 stop the run without challenge-solving or proxy rotation. Sitemap
limits/errors fail visibly. A sitemap pass is not proof of complete catalog
coverage. Failed pages are retried on subsequent runs; saved candidates are
skipped. Bounded defaults deliberately prevent an accidental full crawl.

## Verification and limitations

Google Drive connection can access the live spreadsheet. This does not grant
the standalone Python process service-account credentials. The deployment must
provide GOOGLE_APPLICATION_CREDENTIALS and share the spreadsheet with that
service account. Do not commit the credential file.

Initial urllib access returned HTTP 403; v8 curl_cffi subsequently received
HTTP 200 for robots.txt and a real product. Chasing M2 PRO 200M parsed as
24990 PLN, EAN 6971636380504, SoldOut. The public sitemap.xml redirects to
an HTML sitemap. Discovery supports category links and pagination as well as
XML sitemap indexes. The crawl follows only unfiltered categories and known
pagination query keys; coverage is explicitly unverified.

The live assortment was read through the connected Google Sheets account
(4005 returned rows in B1:B4545); this confirmed identity matching includes
the first product row. End-to-end service-account publishing has not been
validated. No full crawl or live product publication has occurred.

## Bounded live test — 2026-11-04

Command: `python3 product_discovery.py --existing existing_live.csv --max-pages 5 --workers 2`.
The CSV was a read-only snapshot of live k_1 column B, not a synthetic assortment.
Result: 5 pages scanned, 5 candidates above 500 PLN, 0 failed, 0 published.
All five reported InStock. They are absent by normalized URL/catalog ID in the
source snapshot; no EAN comparison against the live source was possible because
the source input column contains URLs only.

| Product ID | Product | Price PLN |
| --- | --- | --- |
| 1006431 | HP OmniBook 5 Ultra 5 225U | 3799 |
| 1011438 | Lenovo LOQ 15 Ryzen 7 7445HS | 3699 |
| 1017738 | HP OmniBook 3 Ryzen 7 250 OLED | 3999 |
| 1017822 | HyperX Omen 15 i5-14500HX RTX5060 | 5799 |
| 1018791 | Lenovo ThinkPad E16 G3 Ultra 7 258V | 5590 |

Automated validation: `python3 -m pytest -q tests` — 20 passed.
Checks cover catalog identity/identifier namespaces, invalid prices, JSON-LD
graphs, strict price/currency filtering, bounded crawl, HTML fallback, durable
outbox recovery, first-row inclusion, ambiguous Sheets write timeouts, duplicate
publication prevention, preserving unknown headers, price-parser journal
recovery, and stopping on access restrictions. Syntax compilation also passed.
No original embedded proxy credential values remain in the changed files.
