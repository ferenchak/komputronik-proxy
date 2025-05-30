from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import json
import os
import random

app = Flask(__name__)

# 🔐 Приватні проксі
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

def get_random_proxy():
    proxy_string = random.choice(RAW_PROXIES)
    ip, port, user, pwd = proxy_string.split(":")
    proxy_auth = f"http://{user}:{pwd}@{ip}:{port}"
    return {"http": proxy_auth, "https": proxy_auth}

@app.route("/parse", methods=["GET"])
def parse_product():
    url = request.args.get("url")
    proxy = request.args.get("proxy")  # 'random' or full proxy url

    if not url:
        return jsonify({"error": "Missing URL"}), 400

    try:
        # Визначення проксі
        if proxy == "random" or not proxy:
            proxies = get_random_proxy()
        else:
            proxies = {"http": proxy, "https": proxy}

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36"
        }

        resp = requests.get(url, headers=headers, proxies=proxies, timeout=15)
        if resp.status_code != 200:
            return jsonify({"error": f"HTTP {resp.status_code}"}), resp.status_code

        soup = BeautifulSoup(resp.text, "html.parser")
        scripts = soup.find_all("script", {"type": "application/ld+json"})

        for script in scripts:
            try:
                data = json.loads(script.text)
                if isinstance(data, dict) and data.get("@type") == "Product":
                    offer = data.get("offers", {})
                    return jsonify({
                        "price": offer.get("price"),
                        "availability": offer.get("availability"),
                        "name": data.get("name")
                    })
            except Exception:
                continue

        return jsonify({"error": "Product JSON not found"}), 404

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))




