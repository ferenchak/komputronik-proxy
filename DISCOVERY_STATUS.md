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
the first product row. The initial test did not publish products. Service-account publishing was later
verified successfully; see the follow-up verification below. No full crawl has
been performed.

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


## Service-account publishing verification — 2026-11-04

The user supplied the existing Google service-account key privately. It was
used only through GOOGLE_APPLICATION_CREDENTIALS; no credential contents or
private-key identifiers were added to the repository.

A fresh bounded scan read the live k_1 assortment and saved five candidates.
All five were published to the separate product_discovery worksheet
(sheetId 1131056133). Readback verified the exact five product URLs and the
expected nine-column header. Repeating publication added zero rows and left
all existing values unchanged. The source k_1 worksheet was not written.

The live run exposed a gspread edge case: a newly created blank worksheet
returns `[[]]`. Publication now treats empty trailing rows as empty before
checking headers, while continuing to refuse unknown populated headers.
Four blank-sheet representations are covered by the parameterized test.
Automated checks after the fix: **23 tests passed**.

The key is configured only for this verification environment. A future hosted
process must receive its credentials separately; no scheduled or full-catalog
job was deployed. Webshare password rotation remains an owner action.


## Full-catalog worker and categories

Prepared catalog_worker.py and Dockerfile.discovery for an isolated Railway
one-off job. The worker maintains a persistent SQLite index/product queue and
candidate outbox under /data, recovers interrupted work, follows real category
pagination (?p=2 etc.), and publishes periodically. There is no product-count
or index-page sample cap. It stops on HTTP 403/429 rather than retrying an
access restriction. Failed pages are retained in queue statistics; a completed
traversal must not be described as all products verified if failures remain.

Both k_1 and old column B are excluded by normalized URL/catalog ID. Category
metadata comes from the product's BreadcrumbList: J category, K category_path,
L category_url. The prior nine columns remain in their original positions.
The publisher upgrades the known nine-column schema and enriches empty
category cells in existing discovered rows without changing A-I. Unknown
populated headers still stop publication.

Verification: 29 tests passed, including breadcrumb extraction, old-only
exclusion, schema migration, real pagination, interrupted queue recovery and
end-to-end worker restart without rescanning completed product jobs.

Railway project komputronik-discovery and service catalog-scanner have been
prepared with a dedicated 512 MB progress volume at /data. The code is not yet
connected/deployed. Automatic approval review rejected uploading the Google
service-account private key to Railway because that destination had not been
explicitly authorized for the credential. No credential was transferred.
Full hosted execution remains blocked pending the user's explicit approval
for placing the key in that service's protected environment variables.


## Public-only Railway execution

The user explicitly authorized transferring the Google service-account key to
Railway, but automatic approval review still rejected that transfer. A later
attempt to supply an existing-assortment ID snapshot was also rejected. Neither
credential contents nor the source-sheet snapshot were transmitted to Railway.

The worker therefore also supports DISCOVERY_PUBLIC_CATALOG_ONLY=1. This mode
fetches only public Komputronik catalog data and keeps candidates on the dedicated
persistent /data volume. It has no Google credentials or private-assortment input.
Deduplication against k_1/old and Google Sheets publication are deferred until a
separately authorized Google-connected sync. Adding credentials later enables
that sync, which rechecks both source sheets before writing the durable outbox.
Public-only candidates must not be described as confirmed new-assortment items
until that comparison is completed. No automatic Google sync is active in this
mode. The previous five published rows retain their verified categories.

Validation after adding this mode: 30 tests passed, including no-Google startup,
public-only collection, restart recovery and deferred publication.


### Verified hosted startup

Deployment 813af1e5-0e8d-4b57-a29c-24e08a50ad94 reached SUCCESS on
2026-11-04. Runtime logs confirm public_catalog_only, no Google credentials,
source filtering deferred, and state running. The initial public sitemap
produced 660 pending index/category pages after the first index completed.
This is a running traversal, not a completed catalog audit or confirmed new
assortment result. Candidates and queues are persisted on the attached /data
volume; no automatic Google Sheets synchronization is active.
The service is pinned to executable commit 9b6f35a71e6be1325bfcc61490a53b7504870d3e.


## Daily scheduled operation

The user manually configured GOOGLE_SERVICE_ACCOUNT_JSON and SPREADSHEET_ID
in Railway. Runtime logs confirmed successful publication of 44 candidates and
zero duplicate additions on the following restart. The connected Google branch
of the worker now runs, with k_1/old exclusions and periodic Sheets writes.

Daily Railway cron is configured as `0 1 * * *` (01:00 UTC, currently 03:00
Europe/Kyiv in winter; 04:00 under summer UTC+3). Railway skips a scheduled
execution while the prior one is still active. No separate ChatGPT scheduling
job or manual assistant intervention is required.

DISCOVERY_RECURRING=1 starts a fresh traversal only after a finished or
finished_with_failures pass. Completed queues are reset transactionally so new
catalog products and prior below-threshold prices are rechecked. Interrupted
passes continue from existing checkpoints. Products now found in either source
sheet are skipped. Each process exits after its pass; there is no busy waiting
between scheduled executions. Access restrictions still stop a pass.

Validation after the recurring-pass change: 31 tests passed. The service keeps
the same persistent /data volume. Source credentials were not read or changed
by the agent during this schedule configuration.


## Manual control from Google Sheets (replaces the daily schedule)

The user requested on-demand operation instead of the daily schedule. The
service cron is removed. discovery_control.py polls only the native checkbox
Discovery_керування!B3 every 20 seconds; the catalog worker itself is idle until
requested. Status, source-page progress, pending index pages, candidate count,
result rows and last start/finish are written to B4:B12. A durable control-state
file preserves accepted requests and resumes an interrupted requested scan
across controller restarts. Idle restarts do not initiate a scan. Repeated
checkbox clicks during an active scan are acknowledged without a second worker.
Only a boolean is accepted from the sheet; no sheet text becomes a command.

After a completed pass, the next manual request starts a new traversal; an
unfinished pass continues existing checkpoints. The worker still follows all
reachable pagination without a product/index count cap. 660/662 in early logs
is the number of queued index/category pages, never a product-total limit.
At manual-control setup, the output contained 72 products plus a header row;
the prior interrupted pass had processed 202 product pages and still had 662
index pages pending. These were partial results, not a complete catalog scan.
The native price threshold remains strictly above 500 PLN on parsed product
offers; no unverified site filter URL parameter was introduced.

Validation: 35 tests passed, including one worker per checkbox request,
ignoring repeated clicks during a scan, resuming a prior accepted request,
idle restart behavior and preserving a request after child-process spawn failure.
Only the lightweight controller remains online between manual requests.
