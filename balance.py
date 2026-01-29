from config import w3

def get_balance(addr):
    return w3.eth.get_balance(addr)
