import os
from pathlib import Path
import time
import json
import random
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, Optional, List, Tuple

# curl_cffi замість requests: імітує TLS/JA3-відбиток справжнього Chrome.
# Звичайний requests сам себе видає антибот-системам ще до перевірки IP —
# саме тому mediaexpert.pl банив навіть свіжі residential-проксі з 1-ї спроби.
from urllib.parse import urlparse
from curl_cffi import requests
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials

IMPERSONATE_PROFILE = "chrome"

# =========================
# 🔧 НАЛАШТУВАННЯ
# =========================

SPREADSHEET_NAME = "Price_and_avail_parsing"
WORKSHEET_NAME   = "k_1"
URL_COLUMN_INDEX = 2
WRITE_COL_START  = "F"

CONNECT_TIMEOUT_S = 8
READ_TIMEOUT_S    = 15
LOG_FILE          = "parser.log"

def build_headers() -> Dict[str, str]:
    """Додаткові заголовки поверх тих, що виставляє curl_cffi під обраний
    браузерний профіль. User-Agent навмисно не чіпаємо тут — curl_cffi сам
    підставляє той, що відповідає TLS-відбитку impersonate-профілю, і
    розсинхрон UA/TLS сам по собі є ознакою бота."""
    return {
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    }

# Webshare Rotating Residential — один гейтвей, IP міняється автоматично
# на кожен запит на їхній стороні (Session type = Rotating), тож власного
# списку IP і локальної ротації більше тримати не треба.
WEBSHARE_PROXY_URL = os.environ.get("WEBSHARE_PROXY_URL", "")


# =========================
# 🧬 ГІБРИД: Playwright проходить Cloudflare -> cookies -> curl_cffi
# =========================
# Для цих доменів Cloudflare ставить JS-перевірку, яку curl_cffi сам не пройде.
HYBRID_DOMAINS   = {"www.komputronik.pl"}
WEBSHARE_USER = os.environ.get("WEBSHARE_USER", "")
WEBSHARE_PWD = os.environ.get("WEBSHARE_PWD", "")
WEBSHARE_HOST = os.environ.get("WEBSHARE_HOST", "")
REFRESH_MIN_GAP  = 3      # c, мінімум між двома проходженнями challenge
REFRESH_ATTEMPTS = 3


class HybridSession:
    """Одна sticky-сесія Webshare + cf_clearance cookies + UA з браузера.
    cf_clearance прив'язаний до IP/UA, тому запити йдуть з того ж sticky-IP."""

    def __init__(self, domain: str, warmup_url: str):
        self.domain      = domain
        self.warmup_url  = warmup_url
        self._lock       = threading.Lock()
        self.generation  = 0
        self.cookies: Dict[str, str] = {}
        self.ua          = ""
        self.proxies: Optional[Dict[str, str]] = None
        self._last_refresh = 0.0

    def _new_sticky_user(self) -> str:
        return f"{WEBSHARE_USER}-{random.randint(10000, 99999)}"

    def refresh(self, seen_generation: int) -> None:
        """Проходить challenge у справжньому Chrome. Якщо інший потік уже оновив — пропускає."""
        with self._lock:
            if seen_generation != self.generation:
                return
            wait = REFRESH_MIN_GAP - (time.time() - self._last_refresh)
            if wait > 0 and self.generation > 0:
                time.sleep(wait)
            from playwright.sync_api import sync_playwright
            for attempt in range(1, REFRESH_ATTEMPTS + 1):
                user = self._new_sticky_user()
                logging.info(f"🧬 [{self.domain}] Проходжу Cloudflare (sticky {user}, спроба {attempt})...")
                try:
                    with sync_playwright() as p:
                        b = p.chromium.launch(headless=False, proxy={
                            "server": f"http://{WEBSHARE_HOST}",
                            "username": user, "password": WEBSHARE_PWD})
                        ctx = b.new_context()
                        pg = ctx.new_page()
                        pg.goto(self.warmup_url, timeout=60000)
                        for _ in range(30):
                            if "just a moment" not in pg.title().lower():
                                break
                            pg.wait_for_timeout(2000)
                        ua = pg.evaluate("navigator.userAgent")
                        cookies = {c["name"]: c["value"] for c in ctx.cookies()
                                   if self.domain.split(".", 1)[-1] in c["domain"]}
                        b.close()
                    if "cf_clearance" in cookies:
                        proxy_url = f"http://{user}:{WEBSHARE_PWD}@{WEBSHARE_HOST}"
                        self.cookies = cookies
                        self.ua      = ua
                        self.proxies = {"http": proxy_url, "https": proxy_url}
                        self.generation += 1
                        self._last_refresh = time.time()
                        logging.info(f"✅ [{self.domain}] cf_clearance отримано (покоління {self.generation})")
                        return
                    logging.warning(f"⚠️  [{self.domain}] cf_clearance не отримано")
                except Exception as e:
                    logging.warning(f"⚠️  [{self.domain}] Помилка Playwright: {e}")
            self._last_refresh = time.time()
            logging.error(f"❌ [{self.domain}] Не вдалось пройти Cloudflare")


_hybrid_sessions: Dict[str, HybridSession] = {}
_hybrid_lock = threading.Lock()


def get_hybrid(url: str) -> Optional[HybridSession]:
    host = urlparse(url).netloc.lower()
    if host not in HYBRID_DOMAINS:
        return None
    with _hybrid_lock:
        if host not in _hybrid_sessions:
            _hybrid_sessions[host] = HybridSession(host, url)
        return _hybrid_sessions[host]

# =========================
# 🎛️ ІНТЕРАКТИВНЕ МЕНЮ
# =========================

def ask_menu() -> Dict[str, Any]:
    print("\n" + "=" * 55)
    print("       🤖  PARSER — ВИБІР РЕЖИМУ ЗАПУСКУ")
    print("=" * 55)
    print()
    print("  Що зробити?")
    print("  [1] Оновити тільки ❌  (по всій таблиці)")
    print("  [2] Повний прохід з певного рядка")
    print()

    while True:
        mode = input("  Ваш вибір (1 або 2): ").strip()
        if mode in ("1", "2"):
            break
        print("  ⚠️  Введіть 1 або 2")

    start_row = 2
    if mode == "2":
        print()
        while True:
            row_input = input("  З якого рядка починати? (наприклад 715): ").strip()
            if row_input.isdigit() and int(row_input) >= 2:
                start_row = int(row_input)
                break
            print("  ⚠️  Введіть число >= 2")

    end_row = None
    if mode == "2":
        print()
        e = input("  До якого рядка включно? (Enter = до кінця таблиці): ").strip()
        if e.isdigit() and int(e) >= start_row:
            end_row = int(e)

    print()
    print("  Швидкість роботи:")
    print("  [1] Швидко    — 8 потоків, мінімальні паузи")
    print("  [2] Нормально — 4 потоки, помірні паузи (рекомендовано)")
    print()

    while True:
        speed = input("  Ваш вибір (1 або 2): ").strip()
        if speed in ("1", "2"):
            break
        print("  ⚠️  Введіть 1 або 2")

    if speed == "1":
        workers       = 8
        max_attempts  = 3
        sleep_fail    = 0.3
        sleep_between = (0.2, 0.5)
    else:
        workers       = 4
        max_attempts  = 4
        sleep_fail    = 0.7
        sleep_between = (0.5, 1.2)

    print()
    print("─" * 55)
    mode_label  = "Тільки ❌" if mode == "1" else f"Повний прохід з рядка {start_row}"
    speed_label = "Швидко" if speed == "1" else "Нормально"
    print(f"  Режим:     {mode_label}")
    print(f"  Швидкість: {speed_label} ({workers} потоків)")
    print("─" * 55)
    print()
    input("  Натисніть Enter щоб запустити...")
    print()

    return {
        "mode":          mode,
        "start_row":     start_row,
        "end_row":       end_row,
        "workers":       workers,
        "max_attempts":  max_attempts,
        "sleep_fail":    sleep_fail,
        "sleep_between": sleep_between,
    }


# =========================
# 🧠 МЕНЕДЖЕР ПРОКСІ
# =========================

class ProxyManager:
    """Обгортка над одним Webshare-гейтвеєм. Інтерфейс (get/report_failure/
    report_success) лишили той самий, щоб решта коду не мінялась — але
    ротація IP тепер відбувається на стороні Webshare автоматично."""

    def __init__(self, proxy_url: str):
        self._proxy_url  = proxy_url
        self._lock       = threading.Lock()
        self._fail_count = 0

    def get(self) -> Optional[Dict[str, str]]:
        return {"http": self._proxy_url, "https": self._proxy_url} if self._proxy_url else None

    def report_failure(self, proxy_dict: Optional[Dict]) -> None:
        if not proxy_dict:
            return
        with self._lock:
            self._fail_count += 1

    def report_success(self, proxy_dict: Optional[Dict]) -> None:
        if not proxy_dict:
            return
        with self._lock:
            self._fail_count = 0


proxy_manager = ProxyManager(WEBSHARE_PROXY_URL)

# =========================
# 🧰 СЛУЖБОВІ ФУНКЦІЇ
# =========================

def setup_logger() -> None:
    logging.basicConfig(
        filename=LOG_FILE,
        format="%(asctime)s [%(levelname)s] %(message)s",
        level=logging.INFO,
        encoding="utf-8"
    )
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(console)


def get_sheets_client():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "gspread-access.json"), scope)
    return gspread.authorize(creds)


def _do_request(url: str, use_proxy: bool, hybrid: Optional["HybridSession"]):
    gen = 0
    if hybrid:
        if not hybrid.cookies:
            hybrid.refresh(0)
        gen = hybrid.generation
        proxy = hybrid.proxies
        headers = {"User-Agent": hybrid.ua, "Accept-Language": build_headers()["Accept-Language"]}
        cookies = hybrid.cookies
    else:
        proxy = proxy_manager.get() if use_proxy else None
        headers = build_headers()
        cookies = None
    try:
        resp = requests.get(
            url,
            headers=headers,
            cookies=cookies,
            proxies=proxy,
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            impersonate=IMPERSONATE_PROFILE,
        )
    except Exception as e:
        proxy_manager.report_failure(proxy)
        raise e
    return resp, proxy, gen


def _is_challenge(resp) -> bool:
    return resp.status_code == 403 or resp.headers.get("cf-mitigated") == "challenge"


def parse_product_page(url: str, use_proxy: bool) -> Dict[str, Any]:
    hybrid = get_hybrid(url)
    resp, proxy, gen = _do_request(url, use_proxy, hybrid)

    if hybrid and _is_challenge(resp):
        # cookies протухли / IP змінився: проходимо challenge і одразу повторюємо
        # запит в межах тієї ж спроби, щоб не спалювати спроби рядка
        hybrid.refresh(gen)
        resp, proxy, gen = _do_request(url, use_proxy, hybrid)

    if resp.status_code == 403:
        proxy_manager.report_failure(proxy)
        raise RuntimeError("HTTP 403")
    elif resp.status_code == 429:
        # сайт явно каже "занадто багато запитів" — це не блокування бота,
        # а rate-limit. Проксі, що на цьому впіймалась, треба тимчасово вивести
        # з ротації і почекати стільки, скільки просить сайт (Retry-After).
        proxy_manager.report_failure(proxy)
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 20))
            except ValueError:
                pass
        raise RuntimeError("HTTP 429")
    elif resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")

    proxy_manager.report_success(proxy)

    soup = BeautifulSoup(resp.text, "html.parser")
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.text.strip())
            candidates = [data] if isinstance(data, dict) else (
                [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
            )
            for node in candidates:
                if node.get("@type") == "Product":
                    offers = node.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    return {
                        "name":         node.get("name"),
                        "price":        offers.get("price"),
                        "availability": offers.get("availability"),
                    }
        except Exception:
            continue

    raise RuntimeError("Product JSON not found")


# =========================
# 📦 БАТЧ-ЗАПИС У SHEETS
# =========================

class BatchWriter:
    def __init__(self, sheet, batch_size: int = 20):
        self._sheet      = sheet
        self._batch_size = batch_size
        self._lock       = threading.Lock()
        self._pending:   Dict[int, List] = {}
        self._spool = Path(os.environ.get('PARSER_PENDING_FILE', 'parser_pending.json'))
        if self._spool.exists():
            self._pending = {int(k): v for k, v in json.loads(self._spool.read_text()).items()}

    def _save_pending(self):
        temporary = self._spool.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as f:
            json.dump(self._pending, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(self._spool)

    def add(self, row: int, data: Optional[Dict[str, Any]]) -> None:
        if data:
            values = [data.get("name", ""), data.get("price", ""), data.get("availability", "")]
        else:
            values = ["❌", "❌", "❌"]
        with self._lock:
            self._pending[row] = values
            self._save_pending()
            if len(self._pending) >= self._batch_size:
                try:
                    self._flush_locked()
                except RuntimeError:
                    # Keep successfully parsed rows; the caller must not replace
                    # them with error markers after an automatic write failure.
                    logging.error('Sheets batch retained on disk; final flush will retry')

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._pending:
            return
        rows = sorted(self._pending.keys())
        groups: List[List[int]] = []
        cur: List[int] = []
        for r in rows:
            if not cur or r == cur[-1] + 1:
                cur.append(r)
            else:
                groups.append(cur)
                cur = [r]
        if cur:
            groups.append(cur)

        for group in groups:
            start, end = group[0], group[-1]
            values = [self._pending[r] for r in group]
            try:
                self._sheet.update(
                    range_name=f"{WRITE_COL_START}{start}:H{end}",
                    values=values
                )
                for row in group:
                    del self._pending[row]
                self._save_pending()
                logging.info(f"📝 Записано рядки {start}–{end} ({len(group)} шт.)")
            except Exception as e:
                logging.error(f"Помилка запису {start}–{end}: {e}")
        if self._pending:
            raise RuntimeError(f"Sheets write failed; {len(self._pending)} rows retained in memory. Retry flush().")


# =========================
# ⚙️ ОБРОБКА ОДНОГО РЯДКА
# =========================

def make_process_row(max_attempts: int, sleep_fail: float, base_sleep: Tuple[float, float] = (0.15, 0.4)):
    def process_row(row: int, url: str) -> Tuple[int, Optional[Dict[str, Any]]]:
        if not url or not url.startswith("http"):
            logging.warning(f"Row {row}: некоректний URL → {url!r}")
            return row, None

        for attempt in range(1, max_attempts + 1):
            try:
                # проксі використовуємо з першої спроби — інакше майже весь трафік
                # іде з нашого реального IP і сайт швидко його банить
                use_proxy = True
                if attempt > 1:
                    time.sleep(sleep_fail)
                else:
                    # невелика випадкова пауза навіть на "щасливому" шляху,
                    # щоб не бити по сайту суцільним потоком без затримок
                    time.sleep(random.uniform(*base_sleep))
                data = parse_product_page(url, use_proxy=use_proxy)
                if not data.get("name"):
                    raise RuntimeError("Empty data")
                logging.info(f"✅ Row {row} — успішно (спроба {attempt})")
                return row, data
            except Exception as e:
                logging.warning(f"⚠️  Row {row}, спроба {attempt}: {e}")

        logging.error(f"❌ Row {row} — всі спроби провалені")
        return row, None

    return process_row


# =========================
# 🚀 РЕЖИМИ РОБОТИ
# =========================

def run_full_scan(sheet, writer: BatchWriter, start_row: int, cfg: Dict) -> None:
    process_row = make_process_row(cfg["max_attempts"], cfg["sleep_fail"], cfg["sleep_between"])

    col_b  = sheet.col_values(URL_COLUMN_INDEX)
    urls   = col_b[start_row - 1:(cfg.get('end_row') or len(col_b))]
    tasks  = [(start_row + i, url) for i, url in enumerate(urls)]
    failed = []

    logging.info(f"🚀 Повний прохід з рядка {start_row}, всього {len(tasks)} рядків...")

    with ThreadPoolExecutor(max_workers=cfg["workers"]) as executor:
        futures = {executor.submit(process_row, row, url): (row, url) for row, url in tasks}
        for future in as_completed(futures):
            row, url = futures[future]
            try:
                row_result, data = future.result()
                writer.add(row_result, data)
                if data is None and url.startswith("http"):
                    failed.append((row, url))
            except Exception as e:
                logging.error(f"Помилка рядка {row}: {e}")
                writer.add(row, None)

    writer.flush()

    if failed:
        logging.info(f"🔁 Швидкий повтор: {len(failed)} рядків...")
        for row, url in failed:
            try:
                time.sleep(random.uniform(*cfg["sleep_between"]))
                data = parse_product_page(url, use_proxy=True)
                if not data.get("name"):
                    raise RuntimeError("порожньо")
                writer.add(row, data)
                logging.info(f"✅ Повтор — рядок {row} успішно")
            except Exception as e:
                writer.add(row, None)
                logging.error(f"❌ Повтор не вдався для {row}: {e}")
        writer.flush()


def run_rescan_failed(sheet, writer: BatchWriter, cfg: Dict) -> int:
    process_row = make_process_row(cfg["max_attempts"], cfg["sleep_fail"], cfg["sleep_between"])

    logging.info("📋 Читаємо таблицю для пошуку ❌...")
    col_b = sheet.col_values(URL_COLUMN_INDEX)
    col_f = sheet.col_values(6)  # колонка F

    tasks = []
    for i in range(1, len(col_b)):  # починаємо з рядка 2
        row = i + 1
        url = col_b[i] if i < len(col_b) else ""
        val = col_f[i] if i < len(col_f) else ""
        if val == "❌" and url and url.startswith("http"):
            tasks.append((row, url))

    if not tasks:
        logging.info("✅ ❌ не знайдено — таблиця чиста!")
        return 0

    logging.info(f"🔁 Знайдено {len(tasks)} рядків з ❌ — переробляємо...")

    still_failed = 0
    with ThreadPoolExecutor(max_workers=cfg["workers"]) as executor:
        futures = {executor.submit(process_row, row, url): (row, url) for row, url in tasks}
        for future in as_completed(futures):
            row, url = futures[future]
            try:
                row_result, data = future.result()
                writer.add(row_result, data)
                if data is None:
                    still_failed += 1
            except Exception as e:
                logging.error(f"Помилка rescan {row}: {e}")
                writer.add(row, None)
                still_failed += 1

    writer.flush()
    return still_failed


# =========================
# 🎬 ТОЧКА ВХОДУ
# =========================

def main():
    cfg = ask_menu()
    setup_logger()
    logging.info(f"=== Старт | режим={cfg['mode']} | потоків={cfg['workers']} ===")

    client = get_sheets_client()
    sheet  = client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
    writer = BatchWriter(sheet, batch_size=20)

    if cfg["mode"] == "1":
        # Тільки ❌ по всій таблиці
        still = run_rescan_failed(sheet, writer, cfg)
        if still == 0:
            logging.info("✅ Всі ❌ виправлено!")
        else:
            logging.info(f"⚠️  Залишилось {still} ❌ яких не вдалось виправити")

    else:
        # Повний прохід + фінальний rescan ❌
        run_full_scan(sheet, writer, cfg["start_row"], cfg)
        if cfg.get("end_row"):
            logging.info("🏁 Діапазон оброблено. Фінальний рескан ❌ пропущено "
                         "(запусти режим 1 один раз після всіх процесів).")
            return
        logging.info("🔍 Фінальний прохід — шукаємо ❌ по всій таблиці...")
        still = run_rescan_failed(sheet, writer, cfg)
        if still == 0:
            logging.info("✅ Жодного ❌ не залишилось!")
        else:
            logging.info(f"⚠️  Залишилось {still} ❌")

    logging.info("🏁 Готово.")


if __name__ == "__main__":
    main()
