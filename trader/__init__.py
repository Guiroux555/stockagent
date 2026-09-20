"""A self-contained paper-trading agent for US large-cap equities.

Simulation only: the package has no broker credentials, no signing code and no
path to an order-routing endpoint. The single network call it makes is a
public, unauthenticated GET for daily price history.
"""

__version__ = "0.1.0"
