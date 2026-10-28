import itertools

import pytest

from shop.pricing import active_prices, discount_for, final_total, is_bulk_order, shipping_cost


@pytest.mark.parametrize("qty,expected", [(0, False), (9, False), (10, True), (500, True)])
def test_bulk(qty, expected):
    assert is_bulk_order(qty) is expected


def test_active_prices():
    items = [{"price": 5, "active": True}, None, {"price": 7, "active": False}, {"price": 9, "active": True}]
    assert active_prices(items) == [5, 9]
    assert active_prices([]) == []


@pytest.mark.parametrize("user,total,coupon,expected", [
    (None, 50, "SAVE10", 0),
    ({"tier": "gold"}, 200, "SAVE10", 50.0),
    ({"tier": "gold"}, 200, None, 40.0),
    ({"tier": "gold"}, 50, "SAVE10", 5.0),
    ({"tier": "silver"}, 80, "SAVE10", 4.0),
    ({"tier": "silver"}, 80, None, 0),
])
def test_discount(user, total, coupon, expected):
    assert discount_for(user, total, coupon) == expected


@pytest.mark.parametrize("weight,zone,express,expected", [
    (1, "local", False, 50), (1, "local", True, 80), (2, "local", False, 90), (2, "local", True, 120),
    (3, "intl", False, 250), (3, "intl", True, 400), (None, "intl", True, 0),
])
def test_shipping(weight, zone, express, expected):
    assert shipping_cost(weight, zone, express) == expected


def test_final_total():
    items = [{"price": 100, "active": True}, {"price": 120, "active": True}, {"price": 1, "active": False}]
    assert final_total({"tier": "gold"}, items, "SAVE10", "local", False) == 220 - 55.0 + 90
    assert final_total(None, [], None, "local", False) == 0
