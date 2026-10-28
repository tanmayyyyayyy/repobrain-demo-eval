from shop.orders import process_order


class Pay:
    def __init__(self, ok=True):
        self.ok = ok

    def charge(self, token, amount):
        return {"ok": self.ok, "id": "ch_1"} if self.ok else None


class Notify:
    def __init__(self, boom=False):
        self.boom, self.sent = boom, []

    def send(self, *a):
        if self.boom:
            raise RuntimeError("smtp down")
        self.sent.append(a)


def inv():
    return {"A": {"stock": 5, "price": 10}, "B": {"stock": 0, "price": 3}}


USER = {"id": 1, "card_token": "tok", "email": "a@b.c", "wallet_balance": 100}


def test_missing_order_and_user_and_inventory():
    assert process_order(None, USER, inv(), None, None, None)["errors"] == ["no order"]
    assert process_order({}, None, inv(), None, None, None)["errors"] == ["invalid user"]
    assert process_order({}, USER, {}, None, None, None)["errors"] == ["no inventory"]


def test_happy_path_card():
    n = Notify()
    r = process_order({"items": [{"sku": "A", "qty": 2}]}, dict(USER), inv(), Pay(), n, None)
    assert r["status"] == "confirmed" and r["total"] == 20 and r["payment_id"] == "ch_1" and n.sent


def test_wallet_and_unknown_method():
    o = {"items": [{"sku": "A", "qty": 1}], "payment_method": "wallet"}
    r = process_order(o, dict(USER), inv(), Pay(), None, None)
    assert r["status"] == "confirmed" and r["payment_id"] == "wallet-1"
    o["payment_method"] = "crypto"
    assert process_order(o, dict(USER), inv(), Pay(), None, None)["errors"] == ["unknown payment method"]


def test_payment_failure_and_no_card():
    o = {"items": [{"sku": "A", "qty": 1}]}
    assert process_order(o, dict(USER), inv(), Pay(ok=False), None, None)["errors"] == ["payment failed"]
    assert process_order(o, {"id": 1}, inv(), Pay(), None, None)["errors"] == ["no card"]


def test_stock_rules():
    o = {"items": [{"sku": "A", "qty": 9}]}
    assert process_order(o, dict(USER), inv(), None, None, None)["status"] == "failed"
    r = process_order(o, dict(USER), inv(), None, None, {"allow_partial": True})
    assert r["items"][0]["qty"] == 5 and r["status"] == "confirmed"
    r = process_order({"items": [{"sku": "B", "qty": 1}]}, dict(USER), inv(), None, None, {"allow_partial": True})
    assert r["status"] == "failed" and "out of stock: B" in r["errors"]


def test_bad_items_and_notify_failure():
    o = {"items": [{"qty": 1}, {"sku": "A", "qty": 0}, {"sku": "A", "qty": 1}]}
    r = process_order(o, dict(USER), inv(), None, Notify(boom=True), None)
    assert "missing sku" in r["errors"] and "bad qty for A" in r["errors"]
    assert any(e.startswith("notify failed") for e in r["errors"])
