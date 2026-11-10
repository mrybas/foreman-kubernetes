"""Shared pytest configuration for the integration tests."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "backup: backs up the release, deletes its namespace and restores it; "
        "run last with make test-backup")
