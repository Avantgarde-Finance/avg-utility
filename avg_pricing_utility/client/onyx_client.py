"""Onyx Shares Contract client — fetches share price from on-chain."""
import json
import logging
from pathlib import Path
from typing import Optional

from web3 import Web3

logger = logging.getLogger(__name__)

_ABI_DIR = Path(__file__).parent.parent / "abi"


def _load_abi():
    with open(_ABI_DIR / "OnyxSharesContract.json") as f:
        return json.load(f)


class OnyxClient:
    """Fetches Onyx vault share prices via on-chain sharePrice() calls."""

    def __init__(self):
        self._abi = _load_abi()
        self._decimals_cache = {}

    def _get_decimals(self, contract) -> int:
        addr = contract.address.lower()
        if addr not in self._decimals_cache:
            self._decimals_cache[addr] = contract.functions.decimals().call()
        return self._decimals_cache[addr]

    def get_current_price(self, token_address: str, rpc_url: str) -> Optional[float]:
        """Fetch current share price."""
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(token_address), abi=self._abi
            )
            price_raw, _ = contract.functions.sharePrice().call()
            decimals = self._get_decimals(contract)
            return price_raw / (10 ** decimals)
        except Exception as e:
            logger.error(f"Failed to fetch Onyx share price: {e}")
            return None

    def get_price_at_block(self, token_address: str, rpc_url: str, block_number: int) -> Optional[float]:
        """Fetch share price at a specific block."""
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(token_address), abi=self._abi
            )
            price_raw, _ = contract.functions.sharePrice().call(
                block_identifier=block_number
            )
            decimals = self._get_decimals(contract)
            return price_raw / (10 ** decimals)
        except Exception as e:
            logger.error(f"Failed to fetch Onyx share price at block {block_number}: {e}")
            return None
