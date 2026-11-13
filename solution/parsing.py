"""Schema-driven parsing for the interaction ledger."""

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

IST = timezone(timedelta(hours=5, minutes=30))
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def header_key(value):
    return re.sub(r"[_\s]+", " ", value.strip().lower())


def phone_number(value):
    if not isinstance(value, str):
        return None
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return "+91" + digits if re.fullmatch(r"[6-9][0-9]{9}", digits) else None


def calendar_date(value):
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return date.fromisoformat(value).isoformat()
        parts = re.fullmatch(r"([0-9]{2})([-/])([0-9]{2}|[a-zA-Z]{3})\2([0-9]{4})", value)
        if parts:
            day, separator, month, year = parts.groups()
            if month.isalpha():
                if separator != "-":
                    return None
                month = MONTHS.index(month.lower()) + 1
            return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None
    return None


def money(value):
    value = re.sub(r"^(?:₹|rs\.?|inr)\s*", "", value, flags=re.IGNORECASE)
    if not re.fullmatch(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", value):
        return None
    try:
        amount = Decimal(value.replace(",", ""))
        if amount < 0:
            return None
        with localcontext() as context:
            context.prec = max(28, len(amount.as_tuple().digits) + 3)
            return format(abs(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
    except InvalidOperation:
        return None


def field_value(field, value):
    match field["type"]:
        case "string":
            return value
        case "phone":
            return phone_number(value)
        case "date":
            return calendar_date(value)
        case "decimal":
            return money(value)
        case "enum":
            value = value.lower()
            return value if value in field["values"] else None
    raise ValueError("Unsupported field type in workflow schema")


def parse_lead(row, fields, positions):
    values = {}
    warnings = 0
    for field, index in zip(fields, positions):
        name = field["name"]
        raw = row[index].strip() if index is not None and index < len(row) else ""
        if not raw:
            if field.get("required"):
                return {}, 0, f"missing_required:{name}"
            continue
        parsed = field_value(field, raw)
        if parsed is not None:
            values[name] = parsed
        elif field["type"] == "phone":
            return {}, 0, "invalid_phone"
        elif field.get("required"):
            return {}, 0, f"invalid_required:{name}"
        else:
            warnings += 1
    return values, warnings, None


def event_timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
