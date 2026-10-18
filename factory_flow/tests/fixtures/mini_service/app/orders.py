ORDERS = {"1": {"id": "1", "status": "shipped"}}


def get_order(order_id: str):
    return ORDERS.get(order_id)


def order_status(order_id: str) -> str:
    return get_order(order_id).get("status")
