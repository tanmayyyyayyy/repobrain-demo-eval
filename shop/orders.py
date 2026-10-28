"""
Simulated AI-generated order processor.
High cyclomatic complexity — typical of unguided LLM output.
"""

def process_order(order, user, inventory, payments, notifications, flags):
    result = {"status": "pending", "items": [], "total": 0, "errors": []}
    if order is None:
        result["status"] = "failed"
        result["errors"].append("no order")
        return result
    if user is None or not user.get("id"):
        result["status"] = "failed"
        result["errors"].append("invalid user")
        return result
    if not inventory:
        result["status"] = "failed"
        result["errors"].append("no inventory")
        return result

    for item in order.get("items", []):
        sku = item.get("sku")
        qty = item.get("qty", 0)
        if not sku:
            result["errors"].append("missing sku")
            continue
        stock = inventory.get(sku, {}).get("stock", 0)
        price = inventory.get(sku, {}).get("price", 0)
        if qty <= 0:
            result["errors"].append(f"bad qty for {sku}")
            continue
        if stock < qty:
            if flags and flags.get("allow_partial"):
                available = stock
                if available > 0:
                    result["items"].append({"sku": sku, "qty": available, "price": price})
                    result["total"] += available * price
                    inventory[sku]["stock"] = 0
                else:
                    result["errors"].append(f"out of stock: {sku}")
            else:
                result["errors"].append(f"insufficient stock: {sku}")
                result["status"] = "failed"
                return result
        else:
            result["items"].append({"sku": sku, "qty": qty, "price": price})
            result["total"] += qty * price
            inventory[sku]["stock"] = stock - qty

    if not result["items"] and result["errors"]:
        result["status"] = "failed"
        return result

    if payments and result["total"] > 0:
        method = order.get("payment_method", "card")
        if method == "card":
            if not user.get("card_token"):
                result["status"] = "failed"
                result["errors"].append("no card")
                return result
            charge = payments.charge(user["card_token"], result["total"])
            if not charge or not charge.get("ok"):
                result["status"] = "failed"
                result["errors"].append("payment failed")
                return result
            result["payment_id"] = charge.get("id")
        elif method == "wallet":
            if user.get("wallet_balance", 0) < result["total"]:
                result["status"] = "failed"
                result["errors"].append("insufficient wallet")
                return result
            user["wallet_balance"] -= result["total"]
            result["payment_id"] = "wallet-" + str(user["id"])
        else:
            result["status"] = "failed"
            result["errors"].append("unknown payment method")
            return result

    if notifications and result["status"] != "failed":
        try:
            notifications.send(user.get("email"), "Order confirmed", result)
        except Exception as e:
            result["errors"].append(f"notify failed: {e}")

    if result["status"] == "pending" and result["items"]:
        result["status"] = "confirmed"
    return result
