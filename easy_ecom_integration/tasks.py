import frappe
import requests
import json
from datetime import datetime, timedelta
from pprint import pprint

redis = frappe.cache()


def authorize_easy_ecom():
    config = frappe.get_doc("Easy Ecom Configuration", "")
    email = config.email
    password = frappe.utils.password.get_decrypted_password(
        "Easy Ecom Configuration", "Easy Ecom Configuration", fieldname="password"
    )
    if config.api_base_url.endswith("/"):
        config.api_base_url = config.api_base_url[:-1]
    url = f"{config.api_base_url}/getApiToken"
    payload = json.dumps({"email": email, "password": password})
    headers = {
        "Content-Type": "application/json",
    }
    response = requests.post(url, headers=headers, data=payload)
    # print("response", response.text)
    response = response.json()
    api_token = response["data"]["api_token"]
    redis.set("ee_api_token", api_token)
    return api_token


def get_orders(since=None, until=None, limit=250, next_url=None):
    ee_api_base_url = frappe.db.get_value(
        "Easy Ecom Configuration", "Easy Ecom Configuration", "api_base_url"
    )
    if ee_api_base_url[-1] == "/":
        ee_api_base_url = ee_api_base_url[:-1]
    if next_url:
        url = f"{ee_api_base_url}{next_url}"
    else:
        if not until:
            until = datetime.combine(datetime.today(), datetime.min.time())
        if not since:
            since = until - timedelta(days=1)
        since = since.isoformat().replace("T", " ")
        until = until.isoformat().replace("T", " ")
        api_token = authorize_easy_ecom()
        url = f"{ee_api_base_url}/orders/V2/getAllOrders?api_token={api_token}&start_date={since}&end_date={until}&limit={limit}"
        # print(url)
    headers = {
        "Content-Type": "application/json",
    }
    response = requests.get(url, headers=headers).json()
    if response["code"] == 200:
        data = response["data"]
        return data["orders"], data["nextUrl"]
    return None, None


def get_all_orders():
    orders, next_url = get_orders()
    while next_url:
        new_orders, next_url = get_orders(next_url=next_url)
        orders += new_orders
    return orders


def get_item_count():
    combined_orders = {}
    grouped_by_order = {}
    orders = get_all_orders()
    for order in orders:
        items = {}
        for suborder in order["suborders"]:
            try:
                sku = suborder["sku"]
                quantity = suborder["item_quantity"]
                item = frappe.db.get_value("Easy Ecom SKU Mapping", sku, "item")
                if item:
                    if item in combined_orders:
                        combined_orders[item] += quantity
                    else:
                        combined_orders[item] = quantity
                    if item in items:
                        items[item] += quantity
                    else:
                        items[item] = quantity
                else:
                    print(f"Mapping not found for SKU {sku}")
            except Exception as e:
                print(str(e))
        if items:
            # pprint(order)
            order_id = order["order_id"]
            grouped_by_order[order_id] = {
                "customer_name": order["customer_name"],
                "email": order["email"],
                "order_date": order["order_date"],
                "items": items,
            }
    return combined_orders, grouped_by_order


def create_stock_entry(items):
    config = frappe.get_doc("Easy Ecom Configuration", "")
    source_warehouse = config.source_warehouse
    target_warehouse = config.target_warehouse
    stock_entry = frappe.new_doc("Stock Entry")
    stock_entry.stock_entry_type = config.stock_entry_type
    to_store = {}
    for item in items:
        is_stock_item = frappe.db.get_value("Item", item, "is_stock_item")
        if is_stock_item:
            if item in to_store:
                to_store[item] += items[item]
            else:
                to_store[item] = items[item]
        else:
            # check if bundle
            if frappe.db.exists("Product Bundle", item):
                doc = frappe.get_doc("Product Bundle", item)
                for child_item in doc.items:
                    child_item_code = child_item.item_code
                    child_item_quantity = items[item] * child_item.qty
                    is_stock_item = frappe.db.get_value(
                        "Item", child_item_code, "is_stock_item"
                    )
                    if is_stock_item:
                        if child_item_code in to_store:
                            to_store[child_item_code] += child_item_quantity
                        else:
                            to_store[child_item_code] = child_item_quantity
    for item in to_store:
        row = stock_entry.append("items", {})
        row.s_warehouse = source_warehouse
        row.t_warehouse = target_warehouse
        row.item_code = item
        row.allow_zero_valuation_rate = 1
        row.qty = to_store[item]
    stock_entry.save()
    frappe.db.commit()
    return stock_entry


def create_sales_order(
    items, order_id=None, order_date=None, customer_name=None, email=None
):
    """Create Sales Order from Easy Ecom Orders. Orders are created against customer_for_sales_order from Easy Ecom Configuration"""
    config = frappe.get_single("Easy Ecom Configuration")
    customer = config.customer_for_sales_order
    sales_order = frappe.new_doc("Sales Order")
    sales_order.customer = customer
    if order_id:
        sales_order.po_no = order_id
    if order_date:
        sales_order.po_date = order_date
    if customer_name:
        sales_order.contact_display = customer_name
    if email:
        sales_order.contact_email = email
    # insert items from Easy Ecom Orders
    for item in items:
        row = sales_order.append("items", {})
        row.item_code = item
        row.qty = items[item]
    sales_order.delivery_date = datetime.today()
    sales_order.save()
    frappe.db.commit()
    return sales_order


def daily():
    """Create Stock Entry and Sales Order from Easy Ecom Orders"""
    config = frappe.get_single("Easy Ecom Configuration")
    if not config.should_create_stock_entry and config.sales_order_mode == "None":
        print("Nothing to do")
        return
    combined_orders, grouped_by_order = get_item_count()
    if not combined_orders:
        print("No orders found")
        return
    if config.should_create_stock_entry:
        create_stock_entry(combined_orders)
    if config.sales_order_mode in ["Combined", "Separate"]:
        if config.sales_order_mode == "Combined":
            create_sales_order(combined_orders)
        else:
            if not grouped_by_order:
                print("No orders found")
                return
            for order_id in grouped_by_order:
                items = grouped_by_order[order_id]["items"]
                customer_name = grouped_by_order[order_id]["customer_name"]
                email = grouped_by_order[order_id]["email"]
                order_date = grouped_by_order[order_id]["order_date"]
                create_sales_order(
                    items=items,
                    order_id=order_id,
                    order_date=order_date,
                    customer_name=customer_name,
                    email=email,
                )
