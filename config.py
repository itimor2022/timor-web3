from web3 import Web3

RPC = "https://arb1.arbitrum.io/rpc"   # 可换 Base / OP
CHAIN_ID = 42161

w3 = Web3(Web3.HTTPProvider(RPC))

GAS_LIMIT = 21000
MIN_BALANCE = w3.to_wei(0.003, "ether")  # 留作Gas
