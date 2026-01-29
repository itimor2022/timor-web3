from web3 import Web3
from config import w3, CHAIN_ID, GAS_LIMIT

def send_eth(from_pk, to_addr, amount_eth):
    acct = w3.eth.account.from_key(from_pk)
    nonce = w3.eth.get_transaction_count(acct.address)

    tx = {
        "to": Web3.to_checksum_address(to_addr),
        "value": w3.to_wei(amount_eth, "ether"),
        "gas": GAS_LIMIT,
        "gasPrice": w3.eth.gas_price,
        "nonce": nonce,
        "chainId": CHAIN_ID
    }

    signed = acct.sign_transaction(tx)
    txid = w3.eth.send_raw_transaction(signed.rawTransaction)
    print("tx:", txid.hex())
    return txid.hex()