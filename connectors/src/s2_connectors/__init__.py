"""Connectors that write a drone company's engineering events into Samanvaya 2.

Each connector reads one document produced by a tool outside S2 and turns it into
objects, links and governed actions:

    bench   a HILS or actuator bench run   s2.bench-report/v1
    build   a CI pipeline build            s2.build-record/v1

The contracts live in `connectors/schema`. `plan()` in each module is pure, so what a
document becomes can be read and tested without an engine.
"""

from s2_connectors.contract import ContractError

__all__ = ["ContractError"]
