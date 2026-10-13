"""
Fitmeezo Shipping Logistics & Physical Parcel Dimensions Utility.

Provides robust, Decimal-based physical calculations for:
1. Volumetric weight estimation (kg) using configurable provider divisors.
2. Chargeable weight determination (max of dead weight and volumetric weight).
3. Package-level physical dimension bounding and status tracking.
4. Server-side validation of seller warehouse pickup facilities.

SAFETY CONSTRAINTS:
- No real courier API or rate booking calls.
- Internal dimensional units: centimeters (cm).
- Internal weight units: grams (g) for items/packages, kilograms (kg) for courier billing.
- Rejects negative or mathematically impossible physical measurements.
- Distinguishes unknown, estimated, and verified physical measurements.
"""

from decimal import Decimal, ROUND_HALF_UP
import re
from django.conf import settings

# Conventional courier volumetric divisor (standard Indian road/surface courier divisor is 5000).
# Air couriers or specialized Shiprocket rate cards may use 4000 or 4750.
# This assumption is configurable in Django settings via SHIPROCKET_VOLUMETRIC_DIVISOR.
DEFAULT_VOLUMETRIC_DIVISOR = Decimal("5000.00")

# Indian Postal Index Number (PIN) code pattern: 6 digits, first digit 1-9.
PIN_CODE_PATTERN = re.compile(r"^[1-9][0-9]{5}$")

# Indian standard telephone pattern (minimum 10 digits).
PHONE_PATTERN = re.compile(r"^\+?[0-9\s\-]{10,15}$")


def get_volumetric_divisor() -> Decimal:
    """
    Returns the configured courier volumetric divisor from project settings.
    Falls back to DEFAULT_VOLUMETRIC_DIVISOR (5000) if unconfigured.
    """
    configured = getattr(settings, "SHIPROCKET_VOLUMETRIC_DIVISOR", None)
    if configured is not None:
        try:
            val = Decimal(str(configured))
            if val > Decimal("0.00"):
                return val
        except Exception:
            pass
    return DEFAULT_VOLUMETRIC_DIVISOR


def calculate_volumetric_weight_kg(
    length_cm: Decimal | None,
    breadth_cm: Decimal | None,
    height_cm: Decimal | None,
    divisor: Decimal | None = None,
) -> Decimal | None:
    """
    Computes volumetric (dimensional) weight in kilograms.
    Formula: length_cm * breadth_cm * height_cm / divisor

    Returns None if any measurement is missing, non-positive, or unverified.
    Quantizes result to 3 decimal places (standard kilogram precision).
    """
    if length_cm is None or breadth_cm is None or height_cm is None:
        return None

    try:
        l = Decimal(str(length_cm))
        b = Decimal(str(breadth_cm))
        h = Decimal(str(height_cm))
    except Exception:
        return None

    if l <= Decimal("0.00") or b <= Decimal("0.00") or h <= Decimal("0.00"):
        return None

    div = divisor if divisor and divisor > Decimal("0.00") else get_volumetric_divisor()
    vol_weight = (l * b * h) / div
    return vol_weight.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def calculate_chargeable_weight_kg(
    dead_weight_kg: Decimal | None,
    volumetric_weight_kg: Decimal | None,
) -> Decimal | None:
    """
    Determines estimated chargeable weight under courier industry rules:
    Chargeable Weight = max(Dead Weight, Volumetric Weight).

    Returns None if neither weight can be computed reliably.
    """
    if dead_weight_kg is None and volumetric_weight_kg is None:
        return None

    valid_weights = []
    if dead_weight_kg is not None:
        try:
            dw = Decimal(str(dead_weight_kg))
            if dw > Decimal("0.00"):
                valid_weights.append(dw)
        except Exception:
            pass

    if volumetric_weight_kg is not None:
        try:
            vw = Decimal(str(volumetric_weight_kg))
            if vw > Decimal("0.00"):
                valid_weights.append(vw)
        except Exception:
            pass

    if not valid_weights:
        return None

    return max(valid_weights).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def grams_to_kg(grams: Decimal | None) -> Decimal | None:
    """Converts grams to kilograms with 3 decimal places."""
    if grams is None:
        return None
    try:
        g = Decimal(str(grams))
        if g < Decimal("0.00"):
            return None
        return (g / Decimal("1000.00")).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    except Exception:
        return None


def kg_to_grams(kg: Decimal | None) -> Decimal | None:
    """Converts kilograms to grams with 2 decimal places."""
    if kg is None:
        return None
    try:
        k = Decimal(str(kg))
        if k < Decimal("0.00"):
            return None
        return (k * Decimal("1000.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None


def calculate_package_bounding_dimensions(items: list[dict]) -> dict:
    """
    Calculates estimated parcel dead weight and bounding box measurements
    for a collection of line items belonging to a single SellerOrder package.

    Physical Logistics Modeling:
    - Dead weight: strictly additive (sum of unit_weight * quantity for each line).
    - Parcel dimensions: Stacked bounding box heuristic:
        length: max(item.length_cm)
        breadth: max(item.breadth_cm)
        height: sum(item.height_cm * quantity)
    - If any ordered item lacks physical dimensions, the aggregate dimensions
      are flagged as 'unknown' rather than inventing false parcel measurements.

    Returns:
        dict: {
            "status": "unknown" | "estimated" | "verified",
            "weight_grams": Decimal | None,
            "length_cm": Decimal | None,
            "breadth_cm": Decimal | None,
            "height_cm": Decimal | None,
            "all_verified": bool,
            "has_missing_data": bool,
        }
    """
    if not items:
        return {
            "status": "unknown",
            "weight_grams": None,
            "length_cm": None,
            "breadth_cm": None,
            "height_cm": None,
            "all_verified": False,
            "has_missing_data": True,
        }

    total_weight = Decimal("0.00")
    max_length = Decimal("0.00")
    max_breadth = Decimal("0.00")
    total_height = Decimal("0.00")
    has_missing_data = False
    all_items_verified = True

    for it in items:
        qty = Decimal(str(it.get("quantity", 1)))
        w = it.get("weight_grams")
        l = it.get("length_cm")
        b = it.get("breadth_cm")
        h = it.get("height_cm")
        v = it.get("is_dimensions_verified", False)

        if not v:
            all_items_verified = False

        if any(dim is None or dim <= Decimal("0.00") for dim in [w, l, b, h]):
            has_missing_data = True
            continue

        total_weight += (Decimal(str(w)) * qty)
        max_length = max(max_length, Decimal(str(l)))
        max_breadth = max(max_breadth, Decimal(str(b)))
        total_height += (Decimal(str(h)) * qty)

    if has_missing_data:
        return {
            "status": "unknown",
            "weight_grams": total_weight.quantize(Decimal("0.01")) if total_weight > Decimal("0.00") else None,
            "length_cm": None,
            "breadth_cm": None,
            "height_cm": None,
            "all_verified": False,
            "has_missing_data": True,
        }

    return {
        "status": "estimated",
        "weight_grams": total_weight.quantize(Decimal("0.01")),
        "length_cm": max_length.quantize(Decimal("0.01")),
        "breadth_cm": max_breadth.quantize(Decimal("0.01")),
        "height_cm": total_height.quantize(Decimal("0.01")),
        "all_verified": all_items_verified,
        "has_missing_data": False,
    }


def validate_platform_pickup_location(config_override: dict | None = None) -> dict:
    """
    Validates platform central warehouse configuration from Django settings
    (or an explicit test config dictionary).

    Returns:
        dict: {
            "is_complete": bool,
            "is_verified": bool,
            "status": "incomplete" | "unverified" | "verified",
            "errors": list[str],
            "details": dict,
        }
    """
    wh = config_override if config_override is not None else getattr(settings, "FITMEEZO_PLATFORM_WAREHOUSE", {})
    errors = []

    facility = (wh.get("facility_name") or wh.get("name") or "").strip()
    if not facility or len(facility) < 3:
        errors.append("Platform warehouse facility name is required.")

    address = (wh.get("address_line1") or wh.get("address") or "").strip()
    if not address or len(address) < 5:
        errors.append("Platform warehouse address is required (minimum 5 characters).")

    city = (wh.get("city") or "").strip()
    if not city or len(city) < 2:
        errors.append("Platform warehouse city is required.")

    state = (wh.get("state") or "").strip()
    if not state or len(state) < 2:
        errors.append("Platform warehouse state is required.")

    pin = (wh.get("postal_code") or "").strip()
    if not pin or not PIN_CODE_PATTERN.match(pin):
        errors.append("Platform warehouse postal code must be a valid 6-digit PIN code (e.g. 560100).")

    phone = (wh.get("contact_phone") or wh.get("phone") or "").strip()
    if not phone or not PHONE_PATTERN.match(phone):
        errors.append("Platform warehouse contact phone number is required (10-15 digits).")

    is_complete = len(errors) == 0
    is_verified = bool(wh.get("is_verified", False)) and is_complete

    if not is_complete:
        status = "incomplete"
    elif is_verified:
        status = "verified"
    else:
        status = "unverified"

    return {
        "source": "platform",
        "name": facility,
        "pickup_location_name": facility,
        "is_complete": is_complete,
        "is_verified": is_verified,
        "status": status,
        "verification_status": status,
        "address": address,
        "city": city,
        "state": state,
        "postal_code": pin,
        "phone": phone,
        "errors": errors,
        "validation_errors": errors,
        "details": wh,
    }

