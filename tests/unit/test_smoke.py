def test_package_imports():
    from importlib.metadata import version

    import berth

    assert berth.__version__ == version("berth")
