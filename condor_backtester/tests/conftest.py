import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))


@pytest.fixture(scope="session")
def market():
    from make_synthetic_market import simulate
    from condor_bt.data import MarketData
    return MarketData(simulate(), default_rate=0.04)
