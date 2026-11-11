"""Shared pytest configuration for the integration tests."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "backup: backs up the release, deletes its namespace and restores it; "
        "run last with make test-backup")
    config.addinivalue_line(
        "markers", "content_proxy: needs the Smart Proxy with content that "
        "make test-content-proxy installs")
    config.addinivalue_line(
        "markers", "runs_last: changes what the other tests use; runs after them")


def pytest_collection_modifyitems(items):
    """Move the runs_last tests to the end, keeping their order."""
    items.sort(key=lambda item: item.get_closest_marker("runs_last") is not None)
