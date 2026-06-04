from .base import BaseFetcher
from .cboe import CboeFetcher
from .nasdaq import NasdaqFetcher
from .nyse import NyseFetcher
from .miax import MiaxFetcher
from .box import BoxFetcher
from .memx import MemxFetcher

FETCHER_MAP = {
    "cboe": CboeFetcher,
    "nasdaq": NasdaqFetcher,
    "nyse": NyseFetcher,
    "miax": MiaxFetcher,
    "box": BoxFetcher,
    "memx": MemxFetcher,
}


def get_fetcher(operator: str) -> type[BaseFetcher]:
    if operator not in FETCHER_MAP:
        raise ValueError(f"Unknown operator: {operator}")
    return FETCHER_MAP[operator]
