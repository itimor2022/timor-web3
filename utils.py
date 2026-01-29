import json, random, time

def load_wallets():
    with open("wallets.json") as f:
        return json.load(f)

def sleep(min_s=20, max_s=120):
    t = random.randint(min_s, max_s)
    print(f"sleep {t}s")
    time.sleep(t)
