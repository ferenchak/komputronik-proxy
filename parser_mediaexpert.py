import requests
from bs4 import BeautifulSoup
import random
import time

RAW_PROXIES = [
    "45.192.145.148:5490:pcnxbzdp:diry84e3teka",
    "45.192.134.37:6358:pcnxbzdp:diry84e3teka",
    "45.192.134.175:6496:pcnxbzdp:diry84e3teka",
    "104.238.4.25:5588:pcnxbzdp:diry84e3teka",
    "45.192.145.76:5418:pcnxbzdp:diry84e3teka",
    "104.249.31.30:6114:pcnxbzdp:diry84e3teka",
    "45.192.134.235:6556:pcnxbzdp:diry84e3teka",
    "104.238.4.70:5633:pcnxbzdp:diry84e3teka",
    "104.249.31.0:6084:pcnxbzdp:diry84e3teka",
    "216.173.78.182:6002:pcnxbzdp:diry84e3teka",
    "154.194.10.168:6181:pcnxbzdp:diry84e3teka",
    "45.192.134.56:6377:pcnxbzdp:diry84e3teka",
    "104.249.31.15:6099:pcnxbzdp:diry84e3teka",
    "154.194.10.191:6204:pcnxbzdp:diry84e3teka",
    "45.192.134.92:6413:pcnxbzdp:diry84e3teka",
    "216.173.79.6:6412:pcnxbzdp:diry84e3teka",
    "45.192.134.117:6438:pcnxbzdp:diry84e3teka",
    "216.173.79.221:6627:pcnxbzdp:diry84e3teka",
    "104.249.31.39:6123:pcnxbzdp:diry84e3teka",
    "104.249.31.78:6162:pcnxbzdp:diry84e3teka",
    "104.249.31.126:6210:pcnxbzdp:diry84e3teka",
    "216.173.78.8:5828:pcnxbzdp:diry84e3teka",
    "104.239.13.78:6707:pcnxbzdp:diry84e3teka",
    "104.249.31.191:6275:pcnxbzdp:diry84e3teka",
    "104.238.4.219:5782:pcnxbzdp:diry84e3teka",
    "154.194.10.165:6178:pcnxbzdp:diry84e3teka",
    "45.192.134.116:6437:pcnxbzdp:diry84e3teka",
    "216.173.78.184:6004:pcnxbzdp:diry84e3teka",
    "45.192.134.16:6337:pcnxbzdp:diry84e3teka",
    "104.238.4.236:5799:pcnxbzdp:diry84e3teka",
    "154.194.10.172:6185:pcnxbzdp:diry84e3teka",
    "45.192.134.108:6429:pcnxbzdp:diry84e3teka",
    "104.249.31.36:6120:pcnxbzdp:diry84e3teka",
    "45.192.145.50:5392:pcnxbzdp:diry84e3teka",
    "45.192.145.234:5576:pcnxbzdp:diry84e3teka",
    "45.12.179.221:6752:pcnxbzdp:diry84e3teka",
    "31.223.189.175:6441:pcnxbzdp:diry84e3teka",
    "31.223.188.161:5838:pcnxbzdp:diry84e3teka",
    "84.247.60.90:6060:pcnxbzdp:diry84e3teka",
    "45.192.145.122:5464:pcnxbzdp:diry84e3teka",
    "84.247.60.58:6028:pcnxbzdp:diry84e3teka",
    "45.192.145.118:5460:pcnxbzdp:diry84e3teka",
    "31.223.189.116:6382:pcnxbzdp:diry84e3teka",
    "31.223.189.3:6269:pcnxbzdp:diry84e3teka",
    "84.247.60.157:6127:pcnxbzdp:diry84e3teka",
    "45.12.179.58:6589:pcnxbzdp:diry84e3teka",
    "216.173.79.168:6574:pcnxbzdp:diry84e3teka",
    "216.173.79.13:6419:pcnxbzdp:diry84e3teka",
    "45.192.134.221:6542:pcnxbzdp:diry84e3teka",
    "216.173.79.194:6600:pcnxbzdp:diry84e3teka"
]
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36"
}


def format_proxy(proxy_str):
    ip, port, user, pwd = proxy_str.split(":")
    return {
        "http": f"http://{user}:{pwd}@{ip}:{port}",
        "https": f"http://{user}:{pwd}@{ip}:{port}",
    }


def get_html(url, proxy_list):
    for _ in range(len(proxy_list)):
        proxy_str = random.choice(proxy_list)
        proxies = format_proxy(proxy_str)
        try:
            response = requests.get(url, headers=HEADERS, proxies=proxies, timeout=10)
            if response.status_code == 200:
                return response.text
            else:
                print(f"Status code {response.status_code} from {proxies['http']}")
        except Exception as e:
            print(f"Error with proxy {proxy_str}: {e}")
            time.sleep(1)
    return None


def parse_product_info(html):
    soup = BeautifulSoup(html, "html.parser")
    name_tag = soup.find("meta", property="og:title")
    price_tag = soup.find("meta", itemprop="price")
    availability_tag = soup.find("meta", itemprop="availability")

    name = name_tag["content"] if name_tag else "N/A"
    price = price_tag["content"] if price_tag else "N/A"
    availability = "InStock" in availability_tag["content"] if availability_tag else False

    return name, price, availability


def main():
    url = input("Введіть URL товару на MediaExpert: ").strip()
    html = get_html(url, RAW_PROXIES)
    if html:
        name, price, available = parse_product_info(html)
        print(f"Назва: {name}")
        print(f"Ціна: {price}")
        print(f"Наявність: {'Так' if available else 'Ні'}")
    else:
        print("Не вдалося отримати HTML сторінки.")


if __name__ == "__main__":
    main()
