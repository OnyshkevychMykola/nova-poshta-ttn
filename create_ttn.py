"""Створення однієї або кількох ТТН Нової Пошти.

Один рядок:
  Місто | Відділення | ПІБ | Телефон | Сума
  Місто | Відділення | ПІБ | Телефон | Сума | Відділення відправника

Кілька відправлень: CSV з колонками
  місто;відділення;піб;телефон;сума;відділення_відправника

Відправник, опис і габарити малої посилки беруться з .env.
Отримувач платить і доставку, і комісію грошового переказу. Оціночна вартість
дорівнює сумі післяплати. Номер картки API не приймає: виплата йде так,
як налаштовано в кабінеті.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API_URL = "https://api.novaposhta.ua/v2.0/json/"
ROOT = Path(__file__).resolve().parent
DEFAULTS = {
    "SENDER_CITY": "Львів",
    "SENDER_WAREHOUSE": "72",
    "SENDER_NAME": "Онишкевич Микола Тарасович",
    "PARCEL_DESCRIPTION": "Монети/сувеніри",
    "PARCEL_WEIGHT": "0.5",
    "PARCEL_LENGTH_CM": "20",
    "PARCEL_WIDTH_CM": "20",
    "PARCEL_HEIGHT_CM": "10",
}
VALUE_KEYS = ["NP_API_KEY", *DEFAULTS.keys()]


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def setting(env: dict[str, str], key: str) -> str:
    return os.environ.get(key) or env.get(key) or DEFAULTS.get(key, "")


def norm_name(value: str) -> str:
    text = value.casefold().replace("’", "'").replace("`", "'")
    return re.sub(r"\s+", " ", text).strip()


def clean_city(name: str) -> str:
    compact = re.sub(r"\s+", " ", name).strip()
    return re.sub(r"(?i)^м\.?\s+", "", compact)


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 9:
        digits = "380" + digits
    elif len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    elif len(digits) == 11 and digits.startswith("80"):
        digits = "3" + digits
    if not re.fullmatch(r"380\d{9}", digits):
        raise ValueError(f"Некоректний телефон: {raw}")
    return digits


def display_phone(phone: str) -> str:
    if phone.startswith("380") and len(phone) == 12:
        return "0" + phone[3:]
    return phone


def parse_amount(raw: str) -> str:
    text = raw.strip().replace(" ", "").replace("грн", "").replace("₴", "").replace(",", ".")
    if not re.fullmatch(r"\d+(\.0+)?", text):
        raise ValueError("Сума має бути цілим числом гривень")
    value = int(float(text))
    if value < 1:
        raise ValueError("Сума має бути більшою за 0")
    return str(value)


def split_name(full: str) -> tuple[str, str, str]:
    parts = full.split()
    if len(parts) == 2:
        return parts[0], parts[1], ""
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    raise ValueError("ПІБ: прізвище та ім'я, або прізвище, ім'я і по батькові")


def parse_line(line: str) -> dict[str, str]:
    parts = [part.strip() for part in line.split("|")]
    if len(parts) not in (5, 6):
        raise ValueError(
            "Формат: Місто | Відділення | ПІБ | Телефон | Сума"
            " [| відділення відправника]"
        )
    city, warehouse, name, phone, amount, *rest = parts
    if not city or not name:
        raise ValueError("Місто і ПІБ не можуть бути порожніми")
    if not re.fullmatch(r"\d+", warehouse):
        raise ValueError("Номер відділення отримувача має бути числом")
    sender_warehouse = ""
    if rest:
        if not re.fullmatch(r"\d+", rest[0]):
            raise ValueError("Номер відділення відправника має бути числом")
        sender_warehouse = rest[0]
    last, first, middle = split_name(name)
    return {
        "city": clean_city(city),
        "warehouse": warehouse,
        "name": " ".join(name.split()),
        "last": last,
        "first": first,
        "middle": middle,
        "phone": normalize_phone(phone),
        "amount": parse_amount(amount),
        "sender_warehouse": sender_warehouse,
    }


def positive_number(raw: str, label: str) -> str:
    text = raw.strip().replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", text) or float(text) <= 0:
        raise ValueError(f"{label} має бути додатним числом")
    return text


def volume_m3(length_cm: str, width_cm: str, height_cm: str) -> str:
    volume = float(length_cm) * float(width_cm) * float(height_cm) / 1_000_000
    if volume < 0.0004:
        raise ValueError("Об'єм посилки менший за мінімум Нової Пошти 0.0004 м³")
    return f"{volume:.4f}".rstrip("0").rstrip(".")


def call(api_key: str, model: str, method: str, props: dict) -> dict:
    body = json.dumps(
        {
            "apiKey": api_key,
            "modelName": model,
            "calledMethod": method,
            "methodProperties": props,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    for attempt in range(4):
        request = urllib.request.Request(
            API_URL,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise SystemExit(f"HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise SystemExit(f"Немає з'єднання з API Нової Пошти: {error.reason}") from error
        if payload.get("success"):
            return payload
        messages = payload.get("errors") or payload.get("errorCodes") or ["невідома помилка"]
        text = "; ".join(str(item) for item in messages)
        if "many request" in text.casefold() and attempt < 3:
            time.sleep(1.5 * (attempt + 1))
            continue
        raise SystemExit(f"{model}.{method}: {text}")
    raise SystemExit(f"{model}.{method}: забагато запитів")


def paged(api_key: str, model: str, method: str, props: dict) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, 21):
        chunk = call(api_key, model, method, {**props, "Page": str(page), "Limit": "100"}).get("data") or []
        rows.extend(chunk)
        if len(chunk) < 100:
            break
    return rows


def city_addresses(payload: dict) -> list[dict]:
    rows: list[dict] = []
    for item in payload.get("data") or []:
        if isinstance(item, dict) and "Addresses" in item:
            rows.extend(item["Addresses"])
        elif isinstance(item, dict) and item.get("MainDescription"):
            rows.append(item)
    return rows


def settlement_type(row: dict) -> str:
    code = str(row.get("SettlementTypeCode") or "").strip().casefold().rstrip(".")
    if code:
        return code
    present = str(row.get("Present") or "").strip().casefold()
    return present.split(" ", 1)[0].rstrip(".")


def choose_settlement(name: str, rows: list[dict]) -> dict:
    exact = [
        row
        for row in rows
        if norm_name(row.get("MainDescription", "")) == norm_name(name) and row.get("DeliveryCity")
    ]
    if not exact:
        suggestions = [row.get("Present") or row.get("MainDescription") for row in rows]
        hint = "; ".join(str(item) for item in suggestions[:8]) or "немає збігів"
        raise ValueError(f"Місто «{name}» не знайдено. Варіанти: {hint}")
    cities = [row for row in exact if settlement_type(row) == "м"]
    pool = cities or exact
    if len(pool) == 1:
        return pool[0]
    ranked = sorted(pool, key=lambda row: int(row.get("Warehouses") or 0), reverse=True)
    if int(ranked[0].get("Warehouses") or 0) > int(ranked[1].get("Warehouses") or 0):
        return ranked[0]
    options = "; ".join(row.get("Present", row.get("MainDescription", "")) for row in pool)
    raise ValueError(f"Місто «{name}» неоднозначне: {options}")


def find_city(api_key: str, name: str) -> dict:
    payload = call(
        api_key,
        "Address",
        "searchSettlements",
        {"CityName": name, "Limit": "20", "Page": "1"},
    )
    return choose_settlement(name, city_addresses(payload))


def is_postomat(warehouse: dict) -> bool:
    text = f"{warehouse.get('CategoryOfWarehouse', '')} {warehouse.get('Description', '')}".casefold()
    return "postomat" in text or "поштомат" in text


def find_warehouse(api_key: str, city_ref: str, number: str) -> dict:
    seen: set[str] = set()
    matches = []
    for page in range(1, 4):
        chunk = call(
            api_key,
            "Address",
            "getWarehouses",
            {"CityRef": city_ref, "FindByString": f"№{number}", "Page": str(page), "Limit": "50"},
        ).get("data") or []
        for row in chunk:
            ref = str(row.get("Ref") or "")
            if ref in seen or str(row.get("Number")) != number or is_postomat(row):
                continue
            seen.add(ref)
            matches.append(row)
        if matches or len(chunk) < 50:
            break
    branches = [row for row in matches if row.get("CategoryOfWarehouse") in (None, "", "Branch")]
    chosen = branches or matches
    if len(chosen) == 1:
        return chosen[0]
    if not chosen:
        raise ValueError(f"Відділення №{number} не знайдено")
    options = "; ".join(row.get("Description", "") for row in chosen[:8])
    raise ValueError(f"Кілька відділень №{number}: {options}")


def same_phone(left: str, right: str) -> bool:
    try:
        return normalize_phone(left) == normalize_phone(right)
    except ValueError:
        return False


def find_sender(api_key: str, sender_name: str) -> tuple[dict, dict]:
    counterparties = paged(api_key, "Counterparty", "getCounterparties", {"CounterpartyProperty": "Sender"})
    if not counterparties:
        raise SystemExit("У кабінеті немає відправника. Перевірте API-ключ.")
    named = [row for row in counterparties if norm_name(row.get("Description", "")) == norm_name(sender_name)]
    if len(named) == 1:
        counterparty = named[0]
    elif len(counterparties) == 1:
        counterparty = counterparties[0]
    else:
        options = "; ".join(row.get("Description", "") for row in counterparties)
        raise SystemExit(f"Кілька відправників, немає точного збігу з «{sender_name}»: {options}")
    contacts = call(
        api_key,
        "Counterparty",
        "getCounterpartyContactPersons",
        {"Ref": counterparty["Ref"]},
    ).get("data") or []
    if not contacts:
        raise SystemExit("У відправника немає контактної особи")
    contact = next(
        (row for row in contacts if norm_name(row.get("Description", "")) == norm_name(sender_name)),
        contacts[0],
    )
    phone = str(contact.get("Phones") or contact.get("Phone") or "")
    if not phone:
        raise SystemExit("У контактної особи відправника немає телефона")
    return counterparty, {**contact, "Phones": normalize_phone(phone)}


def find_recipient(api_key: str, phone: str) -> dict | None:
    queries = [phone, "0" + phone[3:]]
    for query in queries:
        rows = call(
            api_key,
            "Counterparty",
            "getCounterparties",
            {"CounterpartyProperty": "Recipient", "FindByString": query, "Page": "1", "Limit": "50"},
        ).get("data") or []
        for row in rows:
            row_phone = str(row.get("Phones") or row.get("Phone") or "")
            if row_phone and same_phone(row_phone, phone):
                return row
    return None


def recipient_contact(api_key: str, counterparty: dict) -> dict:
    contacts = call(
        api_key,
        "Counterparty",
        "getCounterpartyContactPersons",
        {"Ref": counterparty["Ref"]},
    ).get("data") or []
    if not contacts:
        raise SystemExit("У отримувача немає контактної особи")
    return contacts[0]


def create_recipient(api_key: str, shipment: dict[str, str]) -> tuple[dict, dict]:
    props = {
        "FirstName": shipment["first"],
        "LastName": shipment["last"],
        "Phone": shipment["phone"],
        "CounterpartyType": "PrivatePerson",
        "CounterpartyProperty": "Recipient",
    }
    if shipment["middle"]:
        props["MiddleName"] = shipment["middle"]
    created = (call(api_key, "Counterparty", "save", props).get("data") or [{}])[0]
    contact_block = created.get("ContactPerson")
    if isinstance(contact_block, dict) and contact_block.get("data"):
        return created, contact_block["data"][0]
    return created, recipient_contact(api_key, created)


def print_warnings(payload: dict) -> None:
    warnings = payload.get("warnings") or []
    if warnings:
        print("Попередження: " + "; ".join(str(item) for item in warnings))


def tracking_url(number: str) -> str:
    return f"https://novaposhta.ua/tracking/?cargo_number={number}"


def resolve_route(api_key: str, shipment: dict[str, str], config: dict[str, str], cache: dict | None = None) -> dict:
    cache = cache if cache is not None else {}
    cities: dict[str, dict] = cache.setdefault("cities", {})
    warehouses: dict[tuple[str, str], dict] = cache.setdefault("warehouses", {})

    def city(name: str) -> dict:
        key = norm_name(name)
        if key not in cities:
            cities[key] = find_city(api_key, name)
        return cities[key]

    def warehouse(city_ref: str, number: str) -> dict:
        key = (city_ref, number)
        if key not in warehouses:
            warehouses[key] = find_warehouse(api_key, city_ref, number)
        return warehouses[key]

    if "sender" not in cache:
        cache["sender"] = find_sender(api_key, config["sender_name"])
    counterparty, contact = cache["sender"]
    sender_city = city(config["sender_city"])
    sender_warehouse = warehouse(sender_city["DeliveryCity"], shipment["sender_warehouse"] or config["sender_warehouse"])
    recipient_city = city(shipment["city"])
    recipient_warehouse = warehouse(recipient_city["DeliveryCity"], shipment["warehouse"])
    return {
        "sender_city": sender_city,
        "sender_warehouse": sender_warehouse,
        "recipient_city": recipient_city,
        "recipient_warehouse": recipient_warehouse,
        "counterparty": counterparty,
        "contact": contact,
    }


def preview(shipment: dict[str, str], route: dict, config: dict[str, str], existing: dict | None) -> None:
    sender_phone = display_phone(route["contact"]["Phones"])
    recipient_name = shipment["name"]
    if existing and norm_name(existing.get("Description", "")) != norm_name(shipment["name"]):
        recipient_name = f"{shipment['name']} (у кабінеті вже є: {existing.get('Description')})"
    elif existing:
        recipient_name = f"{shipment['name']} (уже є в кабінеті)"
    else:
        recipient_name = f"{shipment['name']} (буде створений у кабінеті)"
    print(f"Відправник: {route['contact'].get('Description') or config['sender_name']}, {sender_phone}")
    print(f"Звідки: {route['sender_warehouse'].get('Description')}")
    print(f"Отримувач: {recipient_name}, {display_phone(shipment['phone'])}")
    print(f"Куди: {route['recipient_warehouse'].get('Description')}")
    print(f"Сума: {shipment['amount']} грн, оцінка і післяплата")
    print(
        f"Посилка: {config['description']}, {config['weight']} кг, "
        f"{config['length']}×{config['width']}×{config['height']} см"
    )
    print("Платник доставки і комісії переказу: отримувач, готівка")


def create_document(api_key: str, shipment: dict[str, str], route: dict, config: dict[str, str], recipient: dict, contact: dict) -> None:
    props = {
        "PayerType": "Recipient",
        "PaymentMethod": "Cash",
        "DateTime": datetime.now().strftime("%d.%m.%Y"),
        "CargoType": "Cargo",
        "Weight": config["weight"],
        "VolumeGeneral": config["volume"],
        "ServiceType": "WarehouseWarehouse",
        "SeatsAmount": "1",
        "Description": config["description"],
        "Cost": shipment["amount"],
        "CitySender": route["sender_city"]["DeliveryCity"],
        "Sender": route["counterparty"]["Ref"],
        "SenderAddress": route["sender_warehouse"]["Ref"],
        "ContactSender": route["contact"]["Ref"],
        "SendersPhone": route["contact"]["Phones"],
        "CityRecipient": route["recipient_city"]["DeliveryCity"],
        "Recipient": recipient["Ref"],
        "RecipientAddress": route["recipient_warehouse"]["Ref"],
        "ContactRecipient": contact["Ref"],
        "RecipientsPhone": shipment["phone"],
        "OptionsSeat": [
            {
                "volumetricVolume": config["volume"],
                "volumetricWidth": config["width"],
                "volumetricLength": config["length"],
                "volumetricHeight": config["height"],
                "weight": config["weight"],
            }
        ],
        "BackwardDeliveryData": [
            {
                "PayerType": "Recipient",
                "CargoType": "Money",
                "RedeliveryString": shipment["amount"],
            }
        ],
    }
    payload = call(api_key, "InternetDocumentGeneral", "save", props)
    print_warnings(payload)
    document = (payload.get("data") or [{}])[0]
    number = document.get("IntDocNumber")
    if not number:
        raise SystemExit(f"ТТН не створено: {json.dumps(document, ensure_ascii=False)}")
    print(f"ТТН: {number}")
    if document.get("CostOnSite"):
        print(f"Вартість доставки: {document['CostOnSite']} грн")
    if document.get("EstimatedDeliveryDate"):
        print(f"Орієнтовна дата: {document['EstimatedDeliveryDate']}")
    print(tracking_url(number))
    return str(number)


def config_from_values(values: dict[str, str]) -> tuple[str, dict[str, str]]:
    def value(key: str) -> str:
        return (values.get(key) or DEFAULTS.get(key) or "").strip()

    api_key = value("NP_API_KEY")
    if not api_key:
        raise ValueError("Вкажіть API-ключ Нової Пошти")
    length = positive_number(value("PARCEL_LENGTH_CM"), "Довжина")
    width = positive_number(value("PARCEL_WIDTH_CM"), "Ширина")
    height = positive_number(value("PARCEL_HEIGHT_CM"), "Висота")
    sender_warehouse = value("SENDER_WAREHOUSE")
    if not re.fullmatch(r"\d+", sender_warehouse):
        raise ValueError("Номер відділення відправника має бути числом")
    config = {
        "sender_city": clean_city(value("SENDER_CITY")),
        "sender_warehouse": sender_warehouse,
        "sender_name": " ".join(value("SENDER_NAME").split()),
        "description": value("PARCEL_DESCRIPTION"),
        "weight": positive_number(value("PARCEL_WEIGHT"), "Вага"),
        "length": length,
        "width": width,
        "height": height,
        "volume": volume_m3(length, width, height),
    }
    return api_key, config


def load_config() -> tuple[str, dict[str, str]]:
    env = load_env(ROOT / ".env")
    try:
        return config_from_values({key: setting(env, key) for key in VALUE_KEYS})
    except ValueError as error:
        if "API-ключ" in str(error):
            raise SystemExit("Додайте NP_API_KEY у файл nova-poshta-ttn/.env") from error
        raise SystemExit(str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Створити ТТН Нової Пошти")
    parser.add_argument("line", nargs="?", help="Місто | Відділення | ПІБ | Телефон | Сума")
    parser.add_argument("--csv", help="CSV: місто; відділення; піб; телефон; сума")
    parser.add_argument("--yes", action="store_true", help="Створити без підтвердження")
    parser.add_argument("--dry-run", action="store_true", help="Перевірити адреси і не створювати ТТН")
    parser.add_argument("--whoami", action="store_true", help="Показати відправника з кабінету")
    parser.add_argument("--self-test", action="store_true", help="Перевірити розбір рядка без API")
    return parser


def run_whoami(api_key: str, config: dict[str, str]) -> None:
    city = find_city(api_key, config["sender_city"])
    warehouse = find_warehouse(api_key, city["DeliveryCity"], config["sender_warehouse"])
    _counterparty, contact = find_sender(api_key, config["sender_name"])
    print(f"Відправник: {contact.get('Description')}, {display_phone(contact['Phones'])}")
    print(f"Відділення: {warehouse.get('Description')}")


def confirm(skip: bool, count: int = 1) -> None:
    if skip:
        return
    label = "ТТН" if count == 1 else f"{count} ТТН"
    answer = input(f"Створити {label}? [y/N] ").strip().casefold()
    if answer not in {"y", "yes", "т", "так"}:
        raise SystemExit("Скасовано")


HEADER_ALIASES = {
    "місто": "city",
    "city": "city",
    "відділення": "warehouse",
    "warehouse": "warehouse",
    "піб": "name",
    "name": "name",
    "телефон": "phone",
    "phone": "phone",
    "сума": "amount",
    "amount": "amount",
    "відділення відправника": "sender_warehouse",
    "відділення_відправника": "sender_warehouse",
    "sender_warehouse": "sender_warehouse",
}


def decode_table(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251")


def clean_cell(value: str) -> str:
    text = value.strip()
    if text.startswith('="') and text.endswith('"'):
        return text[2:-1]
    if text.startswith("="):
        return text[1:].strip().strip('"')
    return text


def header_key(value: str) -> str:
    return HEADER_ALIASES.get(norm_name(value).replace("_", " "), "")


def parse_table(text: str) -> list[tuple[int, dict[str, str]]]:
    if not text.strip():
        raise ValueError("Файл порожній")
    first_line = text.splitlines()[0]
    delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
    rows = [
        row
        for row in csv.reader(io.StringIO(text), delimiter=delimiter)
        if any(cell.strip() for cell in row)
    ]
    if not rows:
        raise ValueError("Файл порожній")
    indexes = {"city": 0, "warehouse": 1, "name": 2, "phone": 3, "amount": 4, "sender_warehouse": 5}
    start = 0
    if sum(1 for cell in rows[0] if header_key(cell)) >= 3:
        indexes = {}
        for index, cell in enumerate(rows[0]):
            key = header_key(cell)
            if key:
                indexes[key] = index
        missing = [name for name in ("city", "warehouse", "name", "phone", "amount") if name not in indexes]
        if missing:
            raise ValueError("У заголовку CSV бракує колонок: " + ", ".join(missing))
        start = 1
    shipments: list[tuple[int, dict[str, str]]] = []
    errors: list[str] = []
    for offset, row in enumerate(rows[start:], start=start + 1):
        def cell(key: str, current: list[str] = row) -> str:
            index = indexes.get(key)
            if index is None or index >= len(current):
                return ""
            return clean_cell(current[index])

        parts = [cell("city"), cell("warehouse"), cell("name"), cell("phone"), cell("amount")]
        if cell("sender_warehouse"):
            parts.append(cell("sender_warehouse"))
        try:
            shipments.append((offset, parse_line(" | ".join(parts))))
        except ValueError as error:
            errors.append(f"Рядок {offset}: {error}")
    if errors:
        raise ValueError("\n".join(errors))
    if not shipments:
        raise ValueError("У файлі немає відправлень")
    return shipments


def load_shipments(path: Path) -> list[tuple[int, dict[str, str]]]:
    if not path.exists():
        raise ValueError(f"Файл не знайдено: {path}")
    return parse_table(decode_table(path))


def error_message(error: Exception) -> str:
    if isinstance(error, SystemExit):
        if not isinstance(error.code, str):
            raise error
        return error.code
    return str(error)


def preview_row(line_no: int, shipment: dict[str, str], route: dict, existing: dict | None) -> None:
    name = shipment["name"]
    if existing and norm_name(existing.get("Description", "")) != norm_name(shipment["name"]):
        name += f" (у кабінеті: {existing.get('Description')})"
    elif existing:
        name += " (уже є в кабінеті)"
    else:
        name += " (новий)"
    origin = ""
    if shipment["sender_warehouse"]:
        origin = f" | з {route['sender_warehouse'].get('Description')}"
    print(f"#{line_no} {name}, {display_phone(shipment['phone'])}, {shipment['amount']} грн{origin}")
    print(f"   {route['recipient_warehouse'].get('Description')}")


def write_results(path: Path, rows: list[tuple[int, dict[str, str], str, str]]) -> Path:
    target = path.with_name(path.stem + ".results.csv")
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["рядок", "місто", "відділення", "піб", "телефон", "сума", "ттн", "помилка"])
        for line_no, shipment, number, error in rows:
            writer.writerow([
                line_no,
                shipment["city"],
                shipment["warehouse"],
                shipment["name"],
                display_phone(shipment["phone"]),
                shipment["amount"],
                number,
                error,
            ])
    print(f"Результат: {target}")
    return target


def run_batch(path: Path, api_key: str, config: dict[str, str], dry_run: bool, skip_confirm: bool) -> Path | None:
    shipments = load_shipments(path)
    print(
        f"Відправник: {config['sender_name']}, {config['sender_city']}, відділення {config['sender_warehouse']}"
    )
    print(f"Посилка: {config['description']}, {config['weight']} кг. Рядків: {len(shipments)}")
    cache: dict = {}
    ready: list[tuple[int, dict[str, str], dict, dict | None]] = []
    failed: list[tuple[int, dict[str, str], str, str]] = []
    for line_no, shipment in shipments:
        try:
            route = resolve_route(api_key, shipment, config, cache)
            existing = find_recipient(api_key, shipment["phone"])
            preview_row(line_no, shipment, route, existing)
            ready.append((line_no, shipment, route, existing))
        except (ValueError, SystemExit) as error:
            message = error_message(error)
            print(f"#{line_no} помилка: {message}")
            failed.append((line_no, shipment, "", message))
    print(f"Готово до створення: {len(ready)}. Помилок: {len(failed)}.")
    if dry_run or not ready:
        print("ТТН не створено")
        return None
    confirm(skip_confirm, len(ready))
    created_contacts: dict[str, tuple[dict, dict]] = {}
    results = list(failed)
    for line_no, shipment, route, existing in ready:
        print(f"#{line_no} {shipment['name']}")
        try:
            saved = created_contacts.get(shipment["phone"])
            if saved:
                recipient, contact = saved
            elif existing:
                recipient, contact = existing, recipient_contact(api_key, existing)
                created_contacts[shipment["phone"]] = (recipient, contact)
            else:
                recipient, contact = create_recipient(api_key, shipment)
                created_contacts[shipment["phone"]] = (recipient, contact)
            number = create_document(api_key, shipment, route, config, recipient, contact)
            results.append((line_no, shipment, number, ""))
        except (ValueError, SystemExit) as error:
            message = error_message(error)
            print(f"Помилка: {message}")
            results.append((line_no, shipment, "", message))
        time.sleep(0.5)
    results.sort(key=lambda item: item[0])
    created = sum(1 for item in results if item[2])
    print(f"Створено: {created}. Помилок: {len(results) - created}.")
    return write_results(path, results)


def self_test() -> None:
    first = parse_line("Одеса | 23 | Шкута Олег | 0505530332 | 1500")
    assert first["city"] == "Одеса" and first["warehouse"] == "23"
    assert first["last"] == "Шкута" and first["first"] == "Олег" and first["middle"] == ""
    assert first["phone"] == "380505530332" and first["amount"] == "1500"
    second = parse_line("м. Дніпро | 17 | Цупренко Данило Володимирович | +380671336906 | 800 | 12")
    assert second["city"] == "Дніпро" and second["middle"] == "Володимирович"
    assert second["sender_warehouse"] == "12" and second["phone"] == "380671336906"
    third = parse_line("Київ | 357 | Лютова Ольга Володимирівна | 0996291857 | 1200")
    assert third["phone"] == "380996291857" and third["warehouse"] == "357"
    assert normalize_phone("38 050 553 03 32") == "380505530332"
    assert volume_m3("20", "20", "10") == "0.004"
    lviv = choose_settlement(
        "Львів",
        [
            {"MainDescription": "Львів", "Present": "м. Львів, Львівська обл.", "SettlementTypeCode": "м.", "DeliveryCity": "city", "Warehouses": 300},
            {"MainDescription": "Львів", "Present": "с. Львів, Первомайський р-н, Миколаївська обл.", "SettlementTypeCode": "с.", "DeliveryCity": "village", "Warehouses": 1},
            {"MainDescription": "Львів", "Present": "с. Львів, Криворізький р-н, Дніпропетровська обл.", "SettlementTypeCode": "с.", "DeliveryCity": "village-2", "Warehouses": 1},
        ],
    )
    assert lviv["DeliveryCity"] == "city"
    table = parse_table(
        "місто;відділення;піб;телефон;сума;відділення_відправника\n"
        "Одеса;23;Шкута Олег;0505530332;1500;\n"
        "Київ;357;Лютова Ольга Володимирівна;0996291857;1 200 грн;12\n"
    )
    assert len(table) == 2
    assert table[0][1]["phone"] == "380505530332"
    assert table[1][1]["amount"] == "1200" and table[1][1]["sender_warehouse"] == "12"
    assert normalize_phone("505530332") == "380505530332"
    try:
        parse_line("Одеса 23 Шкута Олег 0505530332")
    except ValueError:
        return
    raise AssertionError("вільний текст має відхилятися")


def main() -> None:
    configure_stdio()
    args = build_parser().parse_args()
    if args.self_test:
        self_test()
        print("ok")
        return
    if args.whoami:
        api_key, config = load_config()
        run_whoami(api_key, config)
        return
    if args.csv and args.line:
        raise SystemExit("Передайте або один рядок, або --csv")
    if args.csv:
        api_key, config = load_config()
        run_batch(Path(args.csv), api_key, config, args.dry_run, args.yes)
        return
    if not args.line:
        if not sys.stdin.isatty():
            build_parser().print_help()
            raise SystemExit(2)
        args.line = input("Рядок: ").strip()
    shipment = parse_line(args.line)
    api_key, config = load_config()
    route = resolve_route(api_key, shipment, config)
    existing = find_recipient(api_key, shipment["phone"])
    preview(shipment, route, config, existing)
    if args.dry_run:
        print("ТТН не створено")
        return
    confirm(args.yes)
    if existing:
        recipient, contact = existing, recipient_contact(api_key, existing)
    else:
        recipient, contact = create_recipient(api_key, shipment)
    create_document(api_key, shipment, route, config, recipient, contact)


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
