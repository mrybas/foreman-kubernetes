"""Disruption tests drain a node of a multi-node Kind cluster."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "disruption: drains a worker node; run only with make test-disruption")
