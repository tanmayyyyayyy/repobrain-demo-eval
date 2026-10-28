import os
import json
from typing import List, Dict


def is_bulk_order(qty):
    if qty >= 10:
        return True
    else:
        return False


def active_prices(items):
    prices = []
    for item in items:
        if item is not None:
            if item.get("active"):
                prices.append(item["price"])
    return prices


def discount_for(user, total, coupon):
    if user is not None:
        if user.get("tier") == "gold":
            if total > 100:
                if coupon == "SAVE10":
                    return round(total * 0.25, 2)
                else:
                    return round(total * 0.20, 2)
            else:
                return round(total * 0.10, 2)
        else:
            if coupon == "SAVE10":
                return round(total * 0.05, 2)
            else:
                return 0
    else:
        return 0


def shipping_cost(weight, zone, express):
    # Local zone has weight tiers; everything else is flat-rate.
    if zone == "local":
        if weight <= 1:
            if express:
                return 80
            else:
                return 50
        else:
            if express:
                return 120
            else:
                return 90
    else:
        if weight == None:
            return 0
        if express:
            return 400
        else:
            return 250


def final_total(user, items, coupon, zone, express):
    prices = active_prices(items)
    total = sum(prices)
    if total > 0:
        total = total - discount_for(user, total, coupon)
        total = total + shipping_cost(len(prices), zone, express)  # weight ~ item count
    return round(total, 2)
