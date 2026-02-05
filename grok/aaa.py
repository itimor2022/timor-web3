import requests
import pandas as pd
from datetime import datetime

url = "https://www.okx.com/api/v5/market/candles"
params = {"instId": "BTC-USDT", "bar": "1H", "limit": "3"}

r = requests.get(url, params=params).json()
data = r.get("data", [])

if not data:
    print("API返回空")
else:
    df = pd.DataFrame(data, columns=["ts", "o", "h", "l", "c", "-","-","-","-"])
    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
    df[["o","c"]] = df[["o","c"]].astype(float)
    print(df[["ts", "o", "c"]].to_string(index=False))
    print("\n当前UTC时间:", datetime.utcnow().isoformat())