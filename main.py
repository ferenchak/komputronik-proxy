from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import json
import os


app = Flask(__name__)

@app.route("/parse", methods=["GET"])
def parse_product():
    url = request.args.get("url")
    proxy = request.args.get("proxy")

    if not url:
        return jsonify({"error": "Missing URL"}), 400

    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
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



