from utils import load_wallets, sleep
from balance import get_balance
from transfer import send_eth
from config import MIN_BALANCE, w3

MAIN_PK = "主钱包私钥"

wallets = load_wallets()

for w in wallets:
    bal = get_balance(w["address"])
    print(w["address"], w3.from_wei(bal, "ether"))

    if bal < MIN_BALANCE:
        print("补充资金")
        send_eth(MAIN_PK, w["address"], 0.01)

    sleep()
