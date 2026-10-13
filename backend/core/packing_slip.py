"""
Fitmeezo Secure Internal Packing Summary & Warehouse Manifest Generator.

Provides high-quality, professional ReportLab A4 vector PDF generation
for warehouse pick-pack and internal inspection operations.

CRITICAL SAFETY & PRIVACY DIRECTIVES:
- This document is an INTERNAL warehouse pick-pack summary, NEVER a carrier shipping label.
- Prominently states: NON-CARRIER DOCUMENT - NOT A VALID CARRIER SHIPPING LABEL.
- Uses ReportLab (vector A4 portrait) with responsive tables, clean typography, and repeated table headers.
- Masks customer personal telephone numbers and residential street addresses to prevent PII exposure.
- Never displays merchant financial margins, marketplace commissions, customer tax splits, or provider API secrets.
- Scoped strictly to the authorized seller's sub-order package items.
"""

from __future__ import annotations

from decimal import Decimal
import io
import re
from typing import Any

from django.conf import settings
from django.utils import timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.graphics.barcode.code128 import Code128
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing


def mask_phone_number(phone: str | None) -> str:
    """
    Masks customer telephone numbers to prevent unauthorized PII disclosure.
    Example: '+919876543210' -> '******3210'
    """
    if not phone:
        return "[UNLISTED]"
    clean = re.sub(r"[^\d]", "", str(phone).strip())
    if len(clean) >= 4:
        return "******" + clean[-4:]
    return "****"


def mask_address_line(address: str | None) -> str:
    """
    Masks detailed residential address line 1 to avoid street-level PII harvesting
    while retaining enough identifying characters for warehouse cross-referencing.
    """
    if not address:
        return "[ADDRESS NOT PROVIDED]"
    clean = str(address).strip()
    if len(clean) <= 8:
        return "***"
    return clean[:4] + ("*" * min(len(clean) - 8, 12)) + clean[-4:]


class PackingSlipNumberedCanvas(canvas.Canvas):
    """
    Two-pass canvas for dynamic total page counting ("Page X of Y")
    and standardized operational security running footer.
    """

    def __init__(self, *args, **kwargs):
        kwargs["pageCompression"] = 0
        super().__init__(*args, **kwargs)
        self.pageCompression = 0
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count: int):
        self.saveState()
        self.setFont("Helvetica-Bold", 7)
        self.setFillColor(colors.HexColor("#64748B"))

        # Thin rule above footer
        self.setStrokeColor(colors.HexColor("#CBD5E1"))
        self.setLineWidth(0.5)
        self.line(36, 32, A4[0] - 36, 32)

        # Left: Non-carrier legal disclaimer and brand note
        self.drawString(
            36,
            22,
            "FITMEEZO LOGISTICS • INTERNAL WAREHOUSE PACKING SUMMARY • NON-CARRIER DOCUMENT — NOT A VALID SHIPPING LABEL",
        )

        # Right: Page count
        self.setFont("Helvetica", 7)
        self.drawRightString(
            A4[0] - 36,
            22,
            f"Page {self._pageNumber} of {page_count}",
        )
        self.restoreState()


def generate_internal_packing_summary_pdf(package: Any) -> bytes:
    """
    Generates a professional, secure, ReportLab A4 portrait PDF containing
    an internal warehouse pick-pack manifest for a SellerOrder package.

    Enforces:
    - Clean ReportLab vector layout with proper fonts, margins, and auto-wrapping.
    - Clear title and prominent non-carrier disclaimer.
    - Masked customer contact and street address.
    - Full package itemization (SKU, title, variant, quantity) with repeating table headers.
    - Package physical dimensions and simulated mock reference (if booked).
    - Zero internal financial / margin / tax disclosures.
    """
    from core.models import Order, SellerOrder, ShipmentBooking

    if not isinstance(package, SellerOrder):
        raise ValueError("Package must be an instance of SellerOrder.")

    order: Order = package.order
    seller = package.seller
    booking: ShipmentBooking | None = getattr(package, "shipment_booking", None)
    pickup_details = package.get_pickup_location_details()
    eff_dim = package.get_effective_measurements()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=45,
        pageCompression=0,
    )

    usable_width = A4[0] - 72  # 523.27 pt
    styles = getSampleStyleSheet()

    # Typography and color palette
    brand_title_style = ParagraphStyle(
        "BrandTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#0F172A"),
    )
    brand_sub_style = ParagraphStyle(
        "BrandSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#64748B"),
    )
    doc_title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        alignment=2,  # Right align
        textColor=colors.HexColor("#1E293B"),
    )
    doc_meta_style = ParagraphStyle(
        "DocMeta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        alignment=2,
        textColor=colors.HexColor("#475569"),
    )
    sec_heading_style = ParagraphStyle(
        "SecHeading",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#0F172A"),
    )
    body_text_style = ParagraphStyle(
        "BodyText",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#334155"),
    )
    body_muted_style = ParagraphStyle(
        "BodyMuted",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=10,
        textColor=colors.HexColor("#64748B"),
    )
    body_bold_style = ParagraphStyle(
        "BodyBold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#1E293B"),
    )
    warning_title_style = ParagraphStyle(
        "WarningTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#991B1B"),
    )
    warning_body_style = ParagraphStyle(
        "WarningBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor("#7F1D1D"),
    )
    th_style = ParagraphStyle(
        "TableHead",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#FFFFFF"),
    )
    td_style = ParagraphStyle(
        "TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=10.5,
        textColor=colors.HexColor("#1E293B"),
    )
    td_sku_style = ParagraphStyle(
        "TableCellSKU",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor("#334155"),
    )
    td_center_style = ParagraphStyle(
        "TableCellCenter",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        alignment=1,
        textColor=colors.HexColor("#1E293B"),
    )

    story = []

    # 1. Header: Branding (Left) & Document Identifiers (Right)
    brand_name = getattr(settings, "BRAND_NAME", "Fitmeezo").upper()
    now_str = timezone.now().strftime("%d-%b-%Y %H:%M UTC")

    brand_block = [
        Paragraph(f"{brand_name} LOGISTICS", brand_title_style),
        Paragraph("Fulfillment & Warehouse Operations • Pick-Pack Manifest", brand_sub_style),
        Spacer(1, 2),
        Paragraph(f"Operations Support: {getattr(settings, 'FITMEEZO_SUPPORT_EMAIL', 'support@fitmeezo.com')}", brand_sub_style),
    ]

    title_block = [
        Paragraph("INTERNAL WAREHOUSE PACKING SUMMARY", doc_title_style),
        Spacer(1, 2),
        Paragraph(f"<b>Package Ref:</b> #{package.sub_order_number}", doc_meta_style),
        Paragraph(f"<b>Parent Order:</b> #{order.order_number}", doc_meta_style),
        Paragraph(f"<b>Generated:</b> {now_str}", doc_meta_style),
    ]

    header_table = Table([[brand_block, title_block]], colWidths=[usable_width * 0.55, usable_width * 0.45])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 6))

    # 2. Prominent Non-Carrier Warning Banner
    warning_content = [
        Paragraph(
            "<b>[NON-CARRIER INTERNAL DOCUMENT - NOT A VALID CARRIER SHIPPING LABEL]</b>",
            warning_title_style,
        ),
        Spacer(1, 1),
        Paragraph(
            "<b>NOTICE:</b> This document is strictly for internal merchant warehouse picking, packing, and dispatch quality control. "
            "It is <b>NOT A VALID</b> courier shipping label and cannot be used for carrier handover. "
            "Affix the authorized courier shipping label before logistics carrier pickup.",
            warning_body_style,
        ),
    ]
    warning_table = Table([[warning_content]], colWidths=[usable_width])
    warning_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEF2F2")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#F87171")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(warning_table)
    story.append(Spacer(1, 8))

    # 3. Two-Column Metadata Box: Origin / Shipper vs Package Details
    shipper_name = seller.store_name if seller else "Fitmeezo Central Platform"
    legal_name = seller.legal_business_name if seller and seller.legal_business_name else ""
    pickup_fac_name = pickup_details.get("name") or "Primary Facility"
    pickup_pin = pickup_details.get("postal_code") or "N/A"
    pickup_city = pickup_details.get("city") or "N/A"
    pickup_state = pickup_details.get("state") or "N/A"

    shipper_lines = [
        Paragraph("<b>1. SHIPPER / ORIGIN DISPATCH FACILITY</b>", sec_heading_style),
        Spacer(1, 3),
        Paragraph(f"<b>Store:</b> {shipper_name}", body_text_style),
    ]
    if legal_name and legal_name != shipper_name:
        shipper_lines.append(Paragraph(f"<b>Legal Entity:</b> {legal_name}", body_muted_style))
    shipper_lines.append(Paragraph(f"<b>Facility:</b> {pickup_fac_name}", body_text_style))
    shipper_lines.append(Paragraph(f"<b>Dispatch Location:</b> {pickup_city}, {pickup_state} - {pickup_pin}", body_muted_style))
    if seller and seller.pickup_location_nickname:
        shipper_lines.append(Paragraph(f"<b>Facility Tag:</b> {seller.pickup_location_nickname}", body_muted_style))

    pkg_details_lines = [
        Paragraph("<b>2. PACKAGE &amp; ORDER IDENTIFIERS</b>", sec_heading_style),
        Spacer(1, 3),
        Paragraph(f"<b>Sub-Order Package:</b> #{package.sub_order_number}", body_bold_style),
        Paragraph(f"<b>Parent Order Ref:</b> #{order.order_number}", body_text_style),
        Paragraph(f"<b>Order Placement:</b> {order.created_at.strftime('%d-%b-%Y %H:%M UTC')}", body_muted_style),
        Paragraph(f"<b>Fulfillment Status:</b> {package.get_status_display().upper()}", body_text_style),
        Paragraph(
            f"<b>Payment Mode:</b> {order.get_payment_method_display()} ({order.get_payment_status_display().upper()})",
            body_muted_style,
        ),
    ]

    meta_table = Table([[shipper_lines, pkg_details_lines]], colWidths=[usable_width * 0.50, usable_width * 0.50])
    meta_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 8))

    # 4. Destination Summary (PII-Protected) & Package Measurements (Two columns)
    dest_lines = [
        Paragraph("<b>3. DESTINATION (PROTECTED RECIPIENT DATA)</b>", sec_heading_style),
        Spacer(1, 3),
        Paragraph(f"<b>Recipient Name:</b> {order.shipping_full_name}", body_bold_style),
        Paragraph(
            f"<b>Destination:</b> {order.shipping_city}, {order.shipping_state} (PIN: {order.shipping_postal_code})",
            body_text_style,
        ),
        Paragraph(f"<b>Masked Street:</b> {mask_address_line(order.shipping_address_line1)}", body_muted_style),
        Paragraph(f"<b>Protected Phone:</b> {mask_phone_number(order.shipping_phone_number)}", body_muted_style),
    ]

    dead_wt = package.get_dead_weight_kg() or Decimal("0.000")
    vol_wt = package.get_volumetric_weight_kg() or Decimal("0.000")
    chargeable_wt = package.get_chargeable_weight_kg() or Decimal("0.000")
    l_cm = eff_dim.get("length_cm") or Decimal("0.0")
    b_cm = eff_dim.get("breadth_cm") or Decimal("0.0")
    h_cm = eff_dim.get("height_cm") or Decimal("0.0")

    measurement_lines = [
        Paragraph("<b>4. PACKAGE MEASUREMENTS &amp; WEIGHT</b>", sec_heading_style),
        Spacer(1, 3),
        Paragraph(f"<b>Chargeable Weight:</b> {chargeable_wt:.3f} kg", body_bold_style),
        Paragraph(f"<b>Dead Weight:</b> {dead_wt:.3f} kg &nbsp;|&nbsp; <b>Volumetric:</b> {vol_wt:.3f} kg", body_text_style),
        Paragraph(f"<b>Carton Dimensions:</b> {l_cm:.1f} x {b_cm:.1f} x {h_cm:.1f} cm", body_muted_style),
        Paragraph(f"<b>Measurement Tier:</b> {package.get_package_dimension_status_display()}", body_muted_style),
    ]

    dim_table = Table([[dest_lines, measurement_lines]], colWidths=[usable_width * 0.50, usable_width * 0.50])
    dim_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(dim_table)
    story.append(Spacer(1, 8))

    # 5. Simulated Booking Record (Full Width)
    booking_lines = [
        Paragraph("<b>5. SHIPMENT BOOKING &amp; DISPATCH RECORD</b>", sec_heading_style),
        Spacer(1, 2),
    ]
    if booking:
        booking_lines.append(
            Paragraph(
                f"<b>Simulation Status:</b> SIMULATED TEST BOOKING (OFFLINE / MOCK) &nbsp;|&nbsp; "
                f"<b>Carrier:</b> {booking.courier_name} &nbsp;|&nbsp; "
                f"<b>Mock AWB:</b> {booking.awb_code} &nbsp;|&nbsp; "
                f"<b>Mock Shipment ID:</b> {booking.shipment_id}",
                body_text_style,
            )
        )
        booking_lines.append(
            Paragraph(
                f"Booked on: {booking.created_at.strftime('%d-%b-%Y %H:%M UTC')} &bull; "
                "SAFETY DIRECTIVE: ZERO LIVE COURIER INTERACTION — SIMULATION ONLY",
                body_muted_style,
            )
        )
    else:
        booking_lines.append(
            Paragraph(
                "<b>Simulation Status:</b> NOT YET MOCK-BOOKED &nbsp;|&nbsp; "
                "Fulfillment Note: Generate mock shipment booking via seller portal when carton is sealed and ready for dispatch.",
                body_muted_style,
            )
        )

    booking_table = Table([[booking_lines]], colWidths=[usable_width])
    booking_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F1F5F9")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(booking_table)
    story.append(Spacer(1, 10))

    # 6. Itemized Pick & Pack Table (With repeatRows=1 for clean multi-page pagination)
    table_data = [
        [
            Paragraph("#", th_style),
            Paragraph("SKU", th_style),
            Paragraph("Product Name &amp; Variant Details", th_style),
            Paragraph("Qty", th_style),
        ]
    ]

    items = list(package.items.all())
    total_qty = 0
    col_w_idx = 26
    col_w_sku = 110
    col_w_qty = 42
    col_w_desc = usable_width - (col_w_idx + col_w_sku + col_w_qty)

    for idx, item in enumerate(items, start=1):
        total_qty += item.quantity
        sku_val = (item.sku or "SKU-N/A").strip()
        title_val = (item.product_name or "Item").strip()
        variant_val = item.variant_label.strip() if item.variant_label else ""

        desc_cell = [Paragraph(f"<b>{title_val}</b>", td_style)]
        if variant_val:
            desc_cell.append(Paragraph(f"<font color='#64748B'>Variant: {variant_val}</font>", body_muted_style))

        table_data.append([
            Paragraph(str(idx), td_center_style),
            Paragraph(sku_val, td_sku_style),
            desc_cell,
            Paragraph(str(item.quantity), td_center_style),
        ])

    # Table summary row
    total_summary_cell = Paragraph(
        f"<b>TOTAL PACKAGE UNITS:</b> {total_qty} units across {len(items)} line item(s)",
        body_bold_style,
    )
    table_data.append([
        Paragraph("", td_style),
        Paragraph("", td_style),
        total_summary_cell,
        Paragraph(str(total_qty), td_center_style),
    ])

    items_table = Table(
        table_data,
        colWidths=[col_w_idx, col_w_sku, col_w_desc, col_w_qty],
        repeatRows=1,  # Header row repeats on subsequent pages automatically!
    )

    t_style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),  # Dark Navy Header
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (3, 0), (3, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("GRID", (0, 0), (-1, -2), 0.5, colors.HexColor("#E2E8F0")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
    ]

    # Alternating row background for readability
    for r in range(1, len(items) + 1):
        if r % 2 == 0:
            t_style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#F8FAFC")))

    # Summary row styling
    summary_row_idx = len(items) + 1
    t_style.append(("BACKGROUND", (0, summary_row_idx), (-1, summary_row_idx), colors.HexColor("#EDF2F7")))
    t_style.append(("TOPPADDING", (0, summary_row_idx), (-1, summary_row_idx), 6))
    t_style.append(("BOTTOMPADDING", (0, summary_row_idx), (-1, summary_row_idx), 6))

    items_table.setStyle(TableStyle(t_style))
    story.append(items_table)
    story.append(Spacer(1, 10))

    # 7. Confidentiality & Dispatch Quality Checklist
    notes_block = [
        Paragraph("<b>6. DISPATCH INTEGRITY &amp; OPERATIONAL CHECKLIST</b>", sec_heading_style),
        Spacer(1, 2),
        Paragraph(
            "&bull; <b>Inspection:</b> Verify physical merchandise against listed SKU and quantities prior to carton sealing.<br/>"
            "&bull; <b>Confidentiality:</b> For authorized merchant warehouse operations only. Customer pricing, taxes, and margins are strictly excluded.<br/>"
            "&bull; <b>Non-Carrier Directive:</b> This manifest cannot be used as a courier waybill. Affix carrier shipping label to outer carton.",
            body_muted_style,
        ),
    ]
    notes_table = Table([[notes_block]], colWidths=[usable_width])
    notes_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(KeepTogether(notes_table))

    # Build PDF with dynamic 2-pass NumberedCanvas
    doc.build(story, canvasmaker=PackingSlipNumberedCanvas)
    return buffer.getvalue()


def generate_courier_shipping_label_pdf(package: Any, page_size: str = "4x6") -> bytes:
    """
    Generates a professional, compact courier-style shipping and dispatch label PDF.
    Inspired by standard e-commerce courier waybill layouts (e.g. Delhivery / Shiprocket).

    Key Specifications:
    - Default page size: 4x6 inch (288 x 432 pt) thermal sticker label, with print-friendly A4 option.
    - Customer delivery address block with high-visibility postal PIN code for routing.
    - Seller / return address block with legal entity, pickup facility, and GSTIN.
    - Courier partner name and routing details when genuine carrier booking exists.
    - If unbooked or simulated mock booking, clearly labeled as internal dispatch without
      faking courier names, AWB numbers, barcodes, or QR payloads.
    - Authentic Code128 barcode and QR code only when valid provider-issued identifiers exist.
    - Itemized manifest table with auto-wrapping Paragraph cells.
    - High-contrast black and white formatting with strong borders and compact spacing.
    """
    from core.models import Order, SellerOrder, ShipmentBooking

    if not isinstance(package, SellerOrder):
        raise ValueError("Package must be an instance of SellerOrder.")

    order: Order = package.order
    seller = package.seller
    booking: ShipmentBooking | None = getattr(package, "shipment_booking", None)
    items = list(package.items.all())

    # Format geometry
    is_a4 = str(page_size).strip().lower() in ("a4", "a4_sheet")
    if is_a4:
        page_dim = A4
        margin = 36
        scale = 1.25
    else:
        page_dim = (4 * inch, 6 * inch)
        margin = 8
        scale = 1.0

    usable_w = page_dim[0] - (margin * 2)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_dim,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        pageCompression=0,
    )

    styles = getSampleStyleSheet()
    bold_title = ParagraphStyle(
        "LabelBoldTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10 * scale,
        leading=12 * scale,
    )
    sm_bold = ParagraphStyle(
        "LabelSmBold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7 * scale,
        leading=9 * scale,
    )
    sm_reg = ParagraphStyle(
        "LabelSmReg",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=6.5 * scale,
        leading=8 * scale,
    )
    pin_style = ParagraphStyle(
        "LabelPIN",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=13 * scale,
        leading=15 * scale,
    )
    center_bold = ParagraphStyle("LabelCB", parent=sm_bold, alignment=1)
    center_reg = ParagraphStyle("LabelCR", parent=sm_reg, alignment=1)

    story = []

    # Carrier verification gate
    has_real_carrier_booking = (
        booking is not None
        and not getattr(booking, "is_mock", True)
        and bool(getattr(booking, "awb_code", "").strip())
    )
    has_mock_booking = (
        booking is not None
        and getattr(booking, "is_mock", False)
    )

    # 1. Header Table (Carrier / Dispatch Title + Payment Mode)
    if has_real_carrier_booking:
        c_name = getattr(booking, "courier_name", "COURIER EXPRESS").upper()
        head_left = [
            Paragraph(f"<b>{c_name}</b>", bold_title),
            Paragraph("<b>STANDARD SURFACE / AIR EXPRESS</b>", sm_bold),
            Paragraph(f"Routing Hub: {order.shipping_city.upper()[:3]}", sm_reg),
        ]
    elif has_mock_booking:
        c_name = getattr(booking, "courier_name", "Delhivery Surface")
        head_left = [
            Paragraph("<b>FITMEEZO DISPATCH</b>", bold_title),
            Paragraph("<b>[SIMULATED DISPATCH &bull; NOT A CARRIER LABEL]</b>", sm_bold),
            Paragraph(f"Simulated Carrier: {c_name}", sm_reg),
        ]
    else:
        head_left = [
            Paragraph("<b>FITMEEZO DISPATCH</b>", bold_title),
            Paragraph("<b>[INTERNAL DISPATCH &amp; PACKING LABEL]</b>", sm_bold),
            Paragraph("Carrier: PENDING CARRIER BOOKING", sm_reg),
        ]

    # Payment badge
    is_cod = (order.payment_method == Order.PAYMENT_METHOD_COD) or getattr(order, "is_cod", False)
    if is_cod:
        head_right = [
            Paragraph('<font color="white"><b>C O D</b></font>', ParagraphStyle("PayH", parent=bold_title, alignment=1, fontSize=12 * scale)),
            Paragraph(f'<font color="white"><b>COLLECT: Rs. {order.total_amount:.2f}</b></font>', ParagraphStyle("PaySub", parent=sm_bold, alignment=1, fontSize=7.5 * scale)),
        ]
        right_bg = colors.black
    else:
        head_right = [
            Paragraph("<b>PREPAID</b>", ParagraphStyle("PayH", parent=bold_title, alignment=1, fontSize=12 * scale)),
            Paragraph("Cash to Collect: Rs. 0.00", ParagraphStyle("PaySub", parent=sm_reg, alignment=1)),
        ]
        right_bg = colors.white

    head_tbl = Table([[head_left, head_right]], colWidths=[usable_w * 0.62, usable_w * 0.38])
    head_tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.2, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.75, colors.black),
        ("BACKGROUND", (1, 0), (1, 0), right_bg),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(head_tbl)
    story.append(Spacer(1, 2))

    # 2. Barcode & Routing Identification Box
    if has_real_carrier_booking:
        awb_val = str(booking.awb_code).strip()
        bc = Code128(awb_val, barHeight=28 * scale, barWidth=0.85 * scale)
        bc_elements = [bc, Paragraph(f"<b>AWB No: {awb_val}</b>", center_bold)]

        # Check for genuine tracking URL QR code
        qr_flow = None
        if package.tracking_url:
            qr = QrCodeWidget(str(package.tracking_url).strip())
            b = qr.getBounds()
            qw = b[2] - b[0]
            qh = b[3] - b[1]
            qr_side = 42 * scale
            qr_draw = Drawing(qr_side, qr_side, transform=[qr_side / qw, 0, 0, qr_side / qh, 0, 0])
            qr_draw.add(qr)
            qr_flow = [qr_draw, Paragraph("Track", center_reg)]

        if qr_flow:
            bc_tbl = Table([[bc_elements, qr_flow]], colWidths=[usable_w * 0.82, usable_w * 0.18])
            bc_tbl.setStyle(TableStyle([
                ("BOX", (0, 0), (-1, -1), 1, colors.black),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
        else:
            bc_tbl = Table([[bc_elements]], colWidths=[usable_w])
            bc_tbl.setStyle(TableStyle([
                ("BOX", (0, 0), (-1, -1), 1, colors.black),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
        story.append(bc_tbl)
    else:
        # Strict requirement: No fake courier barcodes or fake QR payloads
        ref_block = [
            Paragraph(f"<b>Package Ref: #{package.sub_order_number}</b> &bull; <b>Order Ref: #{order.order_number}</b>", center_bold),
            Paragraph("[INTERNAL DISPATCH IDENTIFIER &bull; NO CARRIER AWB GENERATED]", center_reg),
            Paragraph("* Affix official carrier barcode shipping label prior to logistics handover *", center_reg),
        ]
        ref_tbl = Table([[ref_block]], colWidths=[usable_w])
        ref_tbl.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.75, colors.black),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F5F5F5")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(ref_tbl)

    story.append(Spacer(1, 2))

    # 3. Delivery Address & Routing Details (Split Box)
    addr_line2 = f"<br/>{order.shipping_address_line2}" if order.shipping_address_line2 else ""
    ship_to_p = [
        Paragraph("<b>SHIP TO (DELIVERY ADDRESS):</b>", sm_bold),
        Paragraph(f"<b>{order.shipping_full_name}</b>", ParagraphStyle("SN", parent=sm_bold, fontSize=8.5 * scale, leading=10.5 * scale)),
        Paragraph(f"{order.shipping_address_line1}{addr_line2}", sm_reg),
        Paragraph(f"{order.shipping_city}, {order.shipping_state}", sm_reg),
        Paragraph(f"PIN: {order.shipping_postal_code}", pin_style),
        Paragraph(f"Phone: {order.shipping_phone_number}", sm_reg),
    ]

    chargeable_wt = package.get_chargeable_weight_kg()
    weight_str = f"{chargeable_wt:.3f} kg" if chargeable_wt else "Not Measured"
    if package.actual_length_cm and package.actual_breadth_cm and package.actual_height_cm:
        dim_str = f"{package.actual_length_cm}x{package.actual_breadth_cm}x{package.actual_height_cm} cm"
    else:
        dim_str = "Standard Carton"

    routing_p = [
        Paragraph("<b>DESTINATION ROUTING:</b>", sm_bold),
        Paragraph(f"<b>{order.shipping_city.upper()}</b>", ParagraphStyle("CityH", parent=bold_title, fontSize=11 * scale, leading=13 * scale)),
        Paragraph(f"Weight: {weight_str}", sm_reg),
        Paragraph(f"Dimensions: {dim_str}", sm_reg),
        Paragraph(f"Order Date: {order.created_at.strftime('%d-%b-%Y')}", sm_reg),
        Paragraph(f"Sub-Order: #{package.sub_order_number}", sm_reg),
    ]

    ship_tbl = Table([[ship_to_p, routing_p]], colWidths=[usable_w * 0.62, usable_w * 0.38])
    ship_tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.75, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(ship_tbl)
    story.append(Spacer(1, 2))

    # 4. Return Address / Seller Information Box
    if seller:
        seller_store = seller.store_name
        seller_legal = seller.legal_business_name
        seller_addr = f"{seller.pickup_address}, {seller.pickup_city}, {seller.pickup_state} - {seller.pickup_postal_code}"
        seller_gstin = seller.gstin or "Unregistered"
        seller_phone = getattr(seller, "registered_mobile", "")
    else:
        seller_store = "Fitmeezo Direct Logistics"
        seller_legal = "Fitmeezo E-Commerce Pvt Ltd"
        seller_addr = "Fitmeezo Central Logistics Hub, Industrial Corridor, Bengaluru, Karnataka - 560100"
        seller_gstin = "29AAAAA0000A1Z5"
        seller_phone = "+91 80 4000 0000"

    seller_p = [
        Paragraph("<b>RETURN TO / SOLD BY:</b>", sm_bold),
        Paragraph(f"<b>{seller_store}</b> ({seller_legal})", sm_reg),
        Paragraph(f"{seller_addr}", sm_reg),
        Paragraph(f"GSTIN: {seller_gstin} | Phone: {seller_phone}", sm_reg),
    ]
    seller_tbl = Table([[seller_p]], colWidths=[usable_w])
    seller_tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.75, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(seller_tbl)
    story.append(Spacer(1, 2))

    # 5. Product Manifest Table (Compact item details)
    item_rows = [[
        Paragraph("<b>#</b>", sm_bold),
        Paragraph("<b>Product / Item Description</b>", sm_bold),
        Paragraph("<b>SKU</b>", sm_bold),
        Paragraph("<b>Variant</b>", sm_bold),
        Paragraph("<b>Qty</b>", sm_bold),
    ]]
    total_qty = 0
    for idx, it in enumerate(items, 1):
        total_qty += it.quantity
        item_rows.append([
            Paragraph(str(idx), sm_reg),
            Paragraph(it.product_name, sm_reg),
            Paragraph(it.sku or "-", sm_reg),
            Paragraph(it.variant_label or "Std", sm_reg),
            Paragraph(str(it.quantity), sm_bold),
        ])

    # Summary row
    item_rows.append([
        Paragraph("", sm_reg),
        Paragraph(f"<b>Total: {len(items)} line item(s)</b>", sm_bold),
        Paragraph("", sm_reg),
        Paragraph("<b>Total Units:</b>", sm_bold),
        Paragraph(f"<b>{total_qty}</b>", sm_bold),
    ])

    item_tbl = Table(
        item_rows,
        colWidths=[usable_w * 0.06, usable_w * 0.48, usable_w * 0.22, usable_w * 0.14, usable_w * 0.10],
        repeatRows=1,
    )
    item_tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.75, colors.black),
        ("INNERGRID", (0, 0), (-1, -2), 0.5, colors.HexColor("#CCCCCC")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAEAEA")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F0F0F0")),
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (4, 0), (4, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(item_tbl)
    story.append(Spacer(1, 2))

    # 6. Operational Footer
    foot = Paragraph(
        "<i>If undelivered, return to the seller address specified above. Manufactured &amp; Sold in India. Fitmeezo Logistics.</i>",
        center_reg,
    )
    story.append(foot)

    if is_a4:
        story.append(Spacer(1, 15))
        cut_p = Paragraph(
            "- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - ✂ Cut along line and paste on parcel - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -",
            ParagraphStyle("CutLine", parent=center_reg, fontName="Helvetica-Oblique", textColor=colors.HexColor("#666666")),
        )
        story.append(cut_p)

    doc.build(story)
    return buffer.getvalue()

