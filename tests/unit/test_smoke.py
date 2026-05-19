def test_package_imports():
    from importlib.metadata import version

    import berth

    # Both should agree: the in-package constant and the installed
    # distribution metadata. The PyPI distribution name stays
    # "serve-engine" until the PEP 541 reclaim of `berth` lands.
    assert berth.__version__ == version("serve-engine")
