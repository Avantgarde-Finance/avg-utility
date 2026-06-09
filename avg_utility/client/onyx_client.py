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

    def get_price_at_block(self, token_address: str, rpc_url: str, block_number: int = None) -> Optional[tuple]:
        """Fetch share price, optionally at a specific block.

        Args:
            token_address: Onyx shares contract address.
            rpc_url: RPC endpoint URL.
            block_number: Block number to query at. None for latest.

        Returns:
            (price_float, on_chain_timestamp_unix) or None on error.
        """
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(token_address), abi=self._abi
            )
            if block_number is not None:
                price_raw, price_ts = contract.functions.sharePrice().call(
                    block_identifier=block_number
                )
            else:
                price_raw, price_ts = contract.functions.sharePrice().call()
            decimals = self._get_decimals(contract)
            return price_raw / (10 ** decimals), int(price_ts)
        except Exception as e:
            logger.error(f"Failed to fetch Onyx share price{f' at block {block_number}' if block_number else ''}: {e}")
            return None
