from fastapi import FastAPI

from app.cart import cart_total
from app.orders import order_status

app = FastAPI()


@app.get("/cart/total")
def get_cart_total(cart_id: str = "c1"):
    return {"total": cart_total(cart_id)}


@app.get("/orders/{order_id}")
def get_order_status(order_id: str):
    return {"status": order_status(order_id)}
