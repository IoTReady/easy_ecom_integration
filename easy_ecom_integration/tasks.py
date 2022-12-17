import frappe
import requests
import json
from datetime import datetime, timedelta

redis = frappe.cache()

def authorize_easy_ecom():
    config = frappe.get_doc('Easy Ecom Configuration', '')
    email = config.email
    password = frappe.utils.password.get_decrypted_password("Easy Ecom Configuration", "Easy Ecom Configuration", fieldname="password")
    import requests
    url = "https://app.easyecom.io/getApiToken"
    payload = json.dumps({
        "email": email,
        "password": password
    })
    headers = {
        'Content-Type': 'application/json',
    }
    response = requests.post(url, headers=headers, data=payload).json()
    api_token = response['data']['api_token']
    redis.set('ee_api_token', api_token)
    return api_token

def get_orders(since=None, until=None, limit=250, next_url=None):
    ee_api_base_url = frappe.db.get_value("Easy Ecom Configuration", "Easy Ecom Configuration", "api_base_url")
    if ee_api_base_url[-1] == '/':
        ee_api_base_url = ee_api_base_url[:-1]
    if next_url:
        url = f"{ee_api_base_url}{next_url}"
    else:
        if not until:
            until = datetime.combine(datetime.today(), datetime.min.time())
        if not since:
            since = until - timedelta(days=1)
        since = since.isoformat().replace('T', ' ')
        until = until.isoformat().replace('T', ' ')
        api_token = authorize_easy_ecom()
        url = f"{ee_api_base_url}/orders/V2/getAllOrders?api_token={api_token}&start_date={since}&end_date={until}&limit={limit}"
        print(url)
    headers = {
        'Content-Type': 'application/json',
    }
    response = requests.get(url, headers=headers).json()
    if response['code'] == 200:
        data = response['data']
        return data['orders'], data['nextUrl']
    return None, None


def get_all_orders():
    orders, next_url = get_orders()
    while next_url:
        new_orders, next_url = get_orders(next_url=next_url)
        orders += new_orders
    return orders

def get_item_count():
    orders = get_all_orders()
    items = {}
    for order in orders:
        for suborder in order['suborders']:
            try:
                sku = suborder['sku']
                quantity = suborder['item_quantity']
                item = frappe.db.get_value('Easy Ecom SKU Mapping', sku, 'item')
                if item:
                    if item in items:
                        items[item] += quantity
                    else:
                        items[item] = quantity
            except Exception as e:
                print(str(e))
    return items


def create_stock_entry():
    config = frappe.get_doc('Easy Ecom Configuration', '')
    items = get_item_count()
    source_warehouse = config.source_warehouse
    target_warehouse = config.target_warehouse
    stock_entry = frappe.new_doc('Stock Entry')
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
                    is_stock_item = frappe.db.get_value("Item", child_item_code, "is_stock_item")
                    if is_stock_item:
                        if child_item_code in to_store:
                            to_store[child_item_code] += child_item_quantity
                        else:
                            to_store[child_item_code] = child_item_quantity
    for item in to_store:
        row = stock_entry.append('items', {})
        row.s_warehouse = source_warehouse
        row.t_warehouse = target_warehouse
        row.item_code = item
        row.allow_zero_valuation_rate = 1
        row.qty = to_store[item]
    stock_entry.save()
    frappe.db.commit()
    return stock_entry


def daily():
    create_stock_entry()

