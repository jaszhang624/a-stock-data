"""Test registry module."""
from astock_api.registry import register, get_function, list_functions, is_available


def test_register_and_get():
    def dummy_func(x): return x
    
    register("test_dummy", "test", "local", "Test function", func=dummy_func)
    assert get_function("test_dummy") is dummy_func


def test_list_functions():
    funcs = list_functions()
    assert isinstance(funcs, list)
    # At least our test function should be there
    names = [f["name"] for f in funcs]
    assert "test_dummy" in names


def test_not_found():
    assert get_function("nonexistent") is None
