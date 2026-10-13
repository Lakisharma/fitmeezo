"""
Fitmeezo Invoice Generation & Safe Numbering Service (Phase 8).

Implements:
1. Transaction-safe monotonic sequential invoice numbering using row-level locking (select_for_update).
2. Immutable invoice document records for Customer Parent Invoices and Seller Package Invoices.
3. High-quality vector PDF generation using ReportLab with multi-page handling, safe wrapping,
   and zero fabricated tax/legal identifiers.
4. Strict separation between Customer Parent Invoices and Seller Package Invoices.
5. Absolute prevention of commission or internal ledger disclosures in customer/seller documents.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import PermissionDenied, ValidationError

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    KeepTogether,
    HRFlowable,
)
from reportlab.pdfgen import canvas

from core.models import (
    Order,
    OrderItem,
    SellerOrder,
    Seller,
    Invoice,
    InvoiceSequence,
)


# ==============================================================================
# NUMBERING & ALLOCATION SERVICE
# ==============================================================================

def allocate_invoice_number(prefix: str | None = None, date_str: str | None = None) -> str:
    """
    Allocates a unique, strictly monotonic invoice number within an atomic transaction.
    
    Guarantees:
    - Never uses count() + 1.
    - Uses InvoiceSequence.objects.select_for_update() row locking to prevent race conditions.
    - Format: {PREFIX}-{YYYYMMDD}-{SEQUENCE_NUMBER:05d}
      Example: FM-INV-20261011-00001
    """
    if not prefix:
        prefix = getattr(settings, "FITMEEZO_INVOICE_PREFIX", "FM-INV")
    
    if not date_str:
        date_str = timezone.now().strftime("%Y%m%d")
    
    seq_key = f"{prefix}-{date_str}"
    
    with transaction.atomic():
        seq_obj, _ = InvoiceSequence.objects.select_for_update().get_or_create(
            prefix=seq_key,
            defaults={"last_number": 0},
        )
        seq_obj.last_number += 1
        seq_obj.save(update_fields=["last_number", "updated_at"])
        allocated_num = f"{prefix}-{date_str}-{seq_obj.last_number:05d}"
        
    return allocated_num


# ==============================================================================
# ISSUANCE POLICY CHECKS
# ==============================================================================

def is_order_eligible_for_invoice(order: Order) -> tuple[bool, str]:
    """
    Evaluates whether an invoice can be issued for this order based on the configured
    FITMEEZO_INVOICE_ISSUANCE_RULE.
    
    Configurable Rules:
    - 'on_payment_or_cod' (default): Online paid orders OR Cash on Delivery orders (placed/confirmed).
    - 'on_dispatch': Orders at 'shipped' or 'delivered' status.
    - 'on_delivery': Orders marked 'delivered'.
    - 'always_on_placement': Any placed non-cancelled order.
    """
    # If invoice was already issued, it remains valid and permanently downloadable
    if order.invoices.filter(invoice_type=Invoice.TYPE_CUSTOMER_PARENT).exists():
        return True, "Invoice already issued."
    
    # Cancelled orders cannot generate a fresh first-time invoice
    if order.status == Order.STATUS_CANCELLED:
        return False, "Cannot issue invoice for cancelled orders."
    
    rule = getattr(settings, "FITMEEZO_INVOICE_ISSUANCE_RULE", "on_payment_or_cod").strip().lower()
    
    if rule == "always_on_placement":
        return True, "Eligible on placement."
    
    if rule == "on_delivery":
        if order.status == Order.STATUS_DELIVERED:
            return True, "Order delivered."
        return False, "Invoice is generated upon delivery confirmation."
    
    if rule == "on_dispatch":
        if order.status in (Order.STATUS_SHIPPED, Order.STATUS_DELIVERED):
            return True, "Order dispatched."
        return False, "Invoice is generated once shipment has been dispatched."
    
    # Default 'on_payment_or_cod'
    if order.payment_status == Order.PAYMENT_STATUS_PAID:
        return True, "Payment received."
    if order.payment_method == Order.PAYMENT_METHOD_COD:
        return True, "Cash on Delivery confirmed."
    
    return False, "Invoice becomes available once payment is settled."


# ==============================================================================
# CUSTOMER PARENT INVOICE RECORD
# ==============================================================================

def get_or_create_customer_invoice(order: Order) -> Invoice:
    """
    Retrieves or generates the immutable Customer Parent Invoice record for an order.
    
    Enforces:
    - Idempotency: Returns existing invoice if already generated.
    - Database uniqueness constraint: Only 1 customer_parent invoice per Order.
    - Accurate financial snapshotting: Preserves historical subtotal, shipping, discounts.
    - Zero fabrication: If GSTIN or tax registration is unconfigured, displays empty / unconfigured notes.
    """
    existing = order.invoices.filter(invoice_type=Invoice.TYPE_CUSTOMER_PARENT).first()
    if existing:
        return existing
    
    eligible, reason = is_order_eligible_for_invoice(order)
    if not eligible:
        raise ValidationError(f"Invoice cannot be issued: {reason}")
    
    with transaction.atomic():
        # Re-check inside transaction with row lock
        locked_order = Order.objects.select_for_update().get(pk=order.pk)
        existing = locked_order.invoices.filter(invoice_type=Invoice.TYPE_CUSTOMER_PARENT).first()
        if existing:
            return existing
        
        prefix = getattr(settings, "FITMEEZO_INVOICE_PREFIX", "FM-INV")
        invoice_number = allocate_invoice_number(prefix=prefix)
        
        # Prepare recipient snapshot
        addr_parts = [
            locked_order.shipping_address_line1,
            locked_order.shipping_address_line2,
            locked_order.shipping_city,
            locked_order.shipping_state,
            f"PIN: {locked_order.shipping_postal_code}",
        ]
        recipient_addr = ", ".join([p for p in addr_parts if p.strip()])
        
        # Prepare issuer snapshot from verified configuration
        issuer_name = getattr(settings, "FITMEEZO_LEGAL_ENTITY_NAME", "Fitmeezo India Retail")
        issuer_addr = getattr(settings, "FITMEEZO_REGISTERED_OFFICE_ADDRESS", "")
        issuer_gstin = getattr(settings, "FITMEEZO_PLATFORM_GSTIN", "").strip()
        issuer_pan = getattr(settings, "FITMEEZO_PLATFORM_PAN", "").strip()
        
        # Snapshot item details for audit and immutability
        items_data = []
        for it in locked_order.items.select_related("seller", "variant").all():
            items_data.append({
                "product_name": it.product_name,
                "variant_label": it.variant_label or "",
                "sku": it.sku or "",
                "quantity": it.quantity,
                "unit_price": str(it.unit_price),
                "line_total": str(it.line_total),
                "seller_name": it.seller.store_name if it.seller else "Fitmeezo Direct",
            })
            
        audit_meta = {
            "order_number": locked_order.order_number,
            "created_at": locked_order.created_at.isoformat(),
            "items_count": len(items_data),
            "items": items_data,
            "issuance_rule": getattr(settings, "FITMEEZO_INVOICE_ISSUANCE_RULE", "on_payment_or_cod"),
            "generator": "Fitmeezo ReportLab Invoice Service v1.0",
        }
        
        invoice = Invoice.objects.create(
            invoice_number=invoice_number,
            invoice_type=Invoice.TYPE_CUSTOMER_PARENT,
            status=Invoice.STATUS_ISSUED,
            order=locked_order,
            subtotal=locked_order.subtotal,
            shipping_fee=locked_order.shipping_fee,
            discount_amount=locked_order.discount_amount,
            tax_amount=Decimal("0.00"),  # Verified tax amount; 0.00 until CA-approved tax engine
            total_amount=locked_order.total_amount,
            currency=locked_order.currency,
            payment_method=locked_order.get_payment_method_display(),
            payment_status=locked_order.get_payment_status_display(),
            issuer_name=issuer_name,
            issuer_address=issuer_addr,
            issuer_gstin=issuer_gstin,
            issuer_pan=issuer_pan,
            recipient_name=locked_order.shipping_full_name,
            recipient_phone=locked_order.shipping_phone_number,
            recipient_shipping_address=recipient_addr,
            issued_at=timezone.now(),
            audit_metadata=audit_meta,
        )
        
    return invoice


# ==============================================================================
# SELLER PACKAGE INVOICE RECORD
# ==============================================================================

def get_or_create_seller_package_invoice(seller_order: SellerOrder) -> Invoice:
    """
    Retrieves or generates an immutable Seller Package Commercial Invoice.
    
    Enforces:
    - Scoped strictly to seller_order items and package subtotal.
    - Completely excludes marketplace commissions, platform fee cuts, and net payout math.
    - Captures the fulfilling seller's verified legal name, registered address, and GSTIN/PAN.
    - For platform-fulfilled packages, falls back to platform issuer details.
    """
    existing = seller_order.invoices.filter(invoice_type=Invoice.TYPE_SELLER_PACKAGE).first()
    if existing:
        return existing
        
    with transaction.atomic():
        locked_pkg = SellerOrder.objects.select_for_update().select_related("order", "seller").get(pk=seller_order.pk)
        existing = locked_pkg.invoices.filter(invoice_type=Invoice.TYPE_SELLER_PACKAGE).first()
        if existing:
            return existing
            
        prefix = getattr(settings, "FITMEEZO_SELLER_INVOICE_PREFIX", "FM-SINV")
        invoice_number = allocate_invoice_number(prefix=prefix)
        order = locked_pkg.order
        seller = locked_pkg.seller
        
        # Fulfilling Issuer details
        if seller:
            issuer_name = seller.legal_business_name or seller.store_name
            addr_parts = [
                seller.registered_address,
                seller.city,
                seller.state,
                f"PIN: {seller.postal_code}",
            ]
            issuer_addr = ", ".join([p for p in addr_parts if p.strip()])
            issuer_gstin = seller.gstin or ""
            issuer_pan = getattr(seller, "pan_number", "") or ""
        else:
            issuer_name = getattr(settings, "FITMEEZO_LEGAL_ENTITY_NAME", "Fitmeezo India Retail")
            issuer_addr = getattr(settings, "FITMEEZO_REGISTERED_OFFICE_ADDRESS", "")
            issuer_gstin = getattr(settings, "FITMEEZO_PLATFORM_GSTIN", "").strip()
            issuer_pan = getattr(settings, "FITMEEZO_PLATFORM_PAN", "").strip()
            
        addr_parts = [
            order.shipping_address_line1,
            order.shipping_address_line2,
            order.shipping_city,
            order.shipping_state,
            f"PIN: {order.shipping_postal_code}",
        ]
        recipient_addr = ", ".join([p for p in addr_parts if p.strip()])
        
        items_data = []
        for it in locked_pkg.items.select_related("variant").all():
            items_data.append({
                "product_name": it.product_name,
                "variant_label": it.variant_label or "",
                "sku": it.sku or "",
                "quantity": it.quantity,
                "unit_price": str(it.unit_price),
                "line_total": str(it.line_total),
            })
            
        pkg_total = locked_pkg.subtotal + locked_pkg.customer_shipping_charge
        
        audit_meta = {
            "sub_order_number": locked_pkg.sub_order_number,
            "parent_order_number": order.order_number,
            "seller_id": seller.pk if seller else None,
            "seller_store": seller.store_name if seller else "Platform Central",
            "items": items_data,
        }
        
        invoice = Invoice.objects.create(
            invoice_number=invoice_number,
            invoice_type=Invoice.TYPE_SELLER_PACKAGE,
            status=Invoice.STATUS_ISSUED,
            order=order,
            seller_order=locked_pkg,
            seller=seller,
            subtotal=locked_pkg.subtotal,
            shipping_fee=locked_pkg.customer_shipping_charge,
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total_amount=pkg_total,
            currency="INR",
            payment_method=order.get_payment_method_display(),
            payment_status=order.get_payment_status_display(),
            issuer_name=issuer_name,
            issuer_address=issuer_addr,
            issuer_gstin=issuer_gstin,
            issuer_pan=issuer_pan,
            recipient_name=order.shipping_full_name,
            recipient_phone=order.shipping_phone_number,
            recipient_shipping_address=recipient_addr,
            issued_at=timezone.now(),
            audit_metadata=audit_meta,
        )
        
    return invoice


# ==============================================================================
# REPORTLAB NUMBERED CANVAS (PAGINATION SUPPORT)
# ==============================================================================

class NumberedCanvas(canvas.Canvas):
    """
    Two-pass canvas to calculate total page count and render:
    'Page X of Y' in the running document footer.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_number(num_pages)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

    def draw_page_number(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748B"))
        # Footer text: left side verification note, right side page number
        self.drawString(36, 25, "Fitmeezo System-Generated Commercial Document • Valid without physical signature")
        self.drawRightString(A4[0] - 36, 25, f"Page {self._pageNumber} of {page_count}")
        # Thin divider line above footer
        self.setStrokeColor(colors.HexColor("#E2E8F0"))
        self.setLineWidth(0.5)
        self.line(36, 35, A4[0] - 36, 35)
        self.restoreState()


# ==============================================================================
# PDF RENDERING ENGINES
# ==============================================================================

def generate_customer_invoice_pdf(invoice: Invoice) -> bytes:
    """
    Generates a high-quality, professional customer invoice PDF for an issued Invoice.
    
    Design Aesthetics:
    - Elegant slate & indigo theme (#0F172A, #2563EB).
    - Responsive table wrapping: Handles long product titles and addresses smoothly.
    - Full breakdown: Item Subtotal, Shipping, Applied Discounts, Final Amount.
    - Verified tax disclosure: Never displays fake GSTIN or unverified tax splits.
    - Customer support contacts and grievance details.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=45,
    )
    
    usable_width = A4[0] - 72  # 523.27 pt
    styles = getSampleStyleSheet()
    
    # Custom styles
    brand_title_style = ParagraphStyle(
        "BrandTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#0F172A"),
    )
    tagline_style = ParagraphStyle(
        "BrandTagline",
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
        fontSize=13,
        leading=16,
        alignment=2,  # Right align
        textColor=colors.HexColor("#1E293B"),
    )
    meta_label_style = ParagraphStyle(
        "MetaLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#475569"),
    )
    meta_val_style = ParagraphStyle(
        "MetaVal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#0F172A"),
    )
    table_head_style = ParagraphStyle(
        "TableHead",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#FFFFFF"),
    )
    cell_text_style = ParagraphStyle(
        "CellText",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#1E293B"),
    )
    cell_bold_style = ParagraphStyle(
        "CellBold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#0F172A"),
    )
    cell_muted_style = ParagraphStyle(
        "CellMuted",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=10,
        textColor=colors.HexColor("#64748B"),
    )
    cell_right_style = ParagraphStyle(
        "CellRight",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        alignment=2,
        textColor=colors.HexColor("#1E293B"),
    )
    cell_right_bold = ParagraphStyle(
        "CellRightBold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=11,
        alignment=2,
        textColor=colors.HexColor("#0F172A"),
    )
    note_style = ParagraphStyle(
        "LegalNote",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=10,
        textColor=colors.HexColor("#475569"),
    )

    story = []

    # 1. Header: Brand (Left) & Document Title / Invoice Number (Right)
    brand_cell = [
        Paragraph(getattr(settings, "BRAND_NAME", "Fitmeezo").upper(), brand_title_style),
        Paragraph(getattr(settings, "BRAND_TAGLINE", "SHOP SMART • LIVE BETTER"), tagline_style),
        Spacer(1, 4),
        Paragraph(f"Support: {getattr(settings, 'FITMEEZO_SUPPORT_EMAIL', 'support@fitmeezo.com')}", tagline_style),
    ]
    title_cell = [
        Paragraph("TAX INVOICE / ORDER RECEIPT", doc_title_style),
        Spacer(1, 3),
        Paragraph(f"<b>Invoice No:</b> {invoice.invoice_number}", ParagraphStyle("InvNo", parent=doc_title_style, fontSize=9, leading=12, textColor=colors.HexColor("#2563EB"))),
        Paragraph(f"<b>Issue Date:</b> {invoice.issued_at.strftime('%d-%b-%Y %H:%M')}", ParagraphStyle("InvDt", parent=doc_title_style, fontSize=8, leading=11, textColor=colors.HexColor("#64748B"))),
    ]
    header_table = Table([[brand_cell, title_cell]], colWidths=[usable_width * 0.55, usable_width * 0.45])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#CBD5E1"), spaceBefore=2, spaceAfter=10))

    # 2. Key Metadata: Order Number, Placement Date, Payment Info
    order = invoice.order
    meta_data = [
        [
            Paragraph("Order Reference:", meta_label_style),
            Paragraph(f"#{order.order_number}", meta_val_style),
            Paragraph("Payment Method:", meta_label_style),
            Paragraph(invoice.payment_method, meta_val_style),
        ],
        [
            Paragraph("Order Date:", meta_label_style),
            Paragraph(order.created_at.strftime("%d-%b-%Y %H:%M"), meta_val_style),
            Paragraph("Payment Status:", meta_label_style),
            Paragraph(invoice.payment_status.upper(), meta_val_style),
        ],
    ]
    meta_table = Table(meta_data, colWidths=[usable_width * 0.20, usable_width * 0.30, usable_width * 0.22, usable_width * 0.28])
    meta_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 10))

    # 3. Party Details: Issuer (Left) vs Shipping Destination (Right)
    issuer_lines = [
        Paragraph("<b>SOLD & ISSUED BY:</b>", cell_bold_style),
        Spacer(1, 2),
        Paragraph(f"<b>{invoice.issuer_name}</b>", cell_text_style),
    ]
    if invoice.issuer_address:
        issuer_lines.append(Paragraph(invoice.issuer_address, cell_muted_style))
    if invoice.issuer_gstin:
        issuer_lines.append(Paragraph(f"<b>GSTIN:</b> {invoice.issuer_gstin}", cell_text_style))
    else:
        issuer_lines.append(Paragraph("<b>GSTIN:</b> Not Applicable / Unregistered", cell_muted_style))
    if invoice.issuer_pan:
        issuer_lines.append(Paragraph(f"<b>PAN:</b> {invoice.issuer_pan}", cell_muted_style))
        
    recipient_lines = [
        Paragraph("<b>SHIPPED & BILLED TO:</b>", cell_bold_style),
        Spacer(1, 2),
        Paragraph(f"<b>{invoice.recipient_name}</b>", cell_text_style),
        Paragraph(invoice.recipient_shipping_address, cell_muted_style),
    ]
    if invoice.recipient_phone:
        clean_phone = invoice.recipient_phone.strip()
        recipient_lines.append(Paragraph(f"Phone: {clean_phone}", cell_muted_style))

    parties_table = Table([[issuer_lines, recipient_lines]], colWidths=[usable_width * 0.50, usable_width * 0.50])
    parties_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(parties_table)
    story.append(Spacer(1, 12))

    # 4. Itemized Merchandise Table
    item_rows = [
        [
            Paragraph("#", table_head_style),
            Paragraph("Item Description & Variant", table_head_style),
            Paragraph("SKU", table_head_style),
            Paragraph("Qty", table_head_style),
            Paragraph("Price (₹)", table_head_style),
            Paragraph("Total (₹)", table_head_style),
        ]
    ]

    items_list = order.items.select_related("seller", "variant").all()
    for idx, it in enumerate(items_list, start=1):
        desc_parts = [Paragraph(f"<b>{it.product_name}</b>", cell_text_style)]
        if it.variant_label:
            desc_parts.append(Paragraph(f"Option: {it.variant_label}", cell_muted_style))
        if it.seller:
            desc_parts.append(Paragraph(f"Merchant: {it.seller.store_name}", cell_muted_style))
            
        item_rows.append([
            Paragraph(str(idx), cell_text_style),
            desc_parts,
            Paragraph(it.sku or "-", cell_muted_style),
            Paragraph(str(it.quantity), cell_text_style),
            Paragraph(f"{it.unit_price:.2f}", cell_right_style),
            Paragraph(f"{it.line_total:.2f}", cell_right_bold),
        ])

    col_widths = [
        usable_width * 0.06,  # #
        usable_width * 0.44,  # Description
        usable_width * 0.16,  # SKU
        usable_width * 0.08,  # Qty
        usable_width * 0.13,  # Unit Price
        usable_width * 0.13,  # Line Total
    ]
    items_table = Table(item_rows, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 10))

    # 5. Financial Summary Box (Right-aligned totals)
    summary_data = [
        [Paragraph("Item Subtotal:", cell_bold_style), Paragraph(f"₹ {invoice.subtotal:.2f}", cell_right_style)],
    ]
    if invoice.shipping_fee > Decimal("0.00"):
        summary_data.append([Paragraph("Shipping & Handling:", cell_bold_style), Paragraph(f"₹ {invoice.shipping_fee:.2f}", cell_right_style)])
    else:
        summary_data.append([Paragraph("Shipping & Handling:", cell_bold_style), Paragraph("FREE", cell_right_bold)])
        
    if invoice.discount_amount > Decimal("0.00"):
        summary_data.append([Paragraph("Promotional Discounts:", cell_bold_style), Paragraph(f"- ₹ {invoice.discount_amount:.2f}", cell_right_style)])
        
    summary_data.append([
        Paragraph("<b>Total Amount Payable:</b>", cell_bold_style),
        Paragraph(f"<b>₹ {invoice.total_amount:.2f}</b>", cell_right_bold),
    ])

    summary_table = Table(summary_data, colWidths=[usable_width * 0.28, usable_width * 0.22])
    summary_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("LINEBELOW", (0, -2), (-1, -2), 0.5, colors.HexColor("#CBD5E1")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F1F5F9")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))

    # Align summary table to right by embedding in a 2-col table
    summary_wrapper = Table([[Paragraph("", cell_text_style), summary_table]], colWidths=[usable_width * 0.50, usable_width * 0.50])
    summary_wrapper.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(KeepTogether(summary_wrapper))
    story.append(Spacer(1, 14))

    # 6. Statutory & Tax Disclosures
    legal_notes = [
        Paragraph("<b>Tax & Commercial Declarations:</b>", cell_bold_style),
        Paragraph(
            "1. Prices indicated above are final customer transaction prices inclusive of applicable taxes under current marketplace terms. "
            "Formal GST registration and tax breakdown are subject to statutory verification and chartered accountant guidance.",
            note_style,
        ),
        Paragraph(
            "2. For returns or exchanges, items must be in original condition with original tags intact. Returns window: 7 days from delivery.",
            note_style,
        ),
        Paragraph(
            f"3. Inquiries & Grievances: Email {getattr(settings, 'FITMEEZO_SUPPORT_EMAIL', 'support@fitmeezo.com')} | "
            f"Helpline: {getattr(settings, 'FITMEEZO_SUPPORT_PHONE', '+91 98765 43210')} ({getattr(settings, 'FITMEEZO_SUPPORT_HOURS', 'Mon-Sat 10am-7pm IST')}).",
            note_style,
        ),
    ]

    # Check for Return/Refund records linked to this order to reference adjustment note
    refund_records = getattr(order, "refund_records", None)
    if refund_records and refund_records.exists():
        legal_notes.append(Spacer(1, 4))
        legal_notes.append(Paragraph("<b>Post-Purchase Financial Adjustments (Phase 6):</b>", cell_bold_style))
        for ref in refund_records.all():
            legal_notes.append(Paragraph(
                f"• Refund Record #{ref.pk}: ₹{ref.amount} ({ref.get_status_display()}) on {ref.created_at.strftime('%d-%b-%Y')}. "
                f"Original invoice remains immutable per standard tax accounting rules.",
                note_style,
            ))

    story.append(KeepTogether(legal_notes))

    # Build PDF using NumberedCanvas
    doc.build(story, canvasmaker=NumberedCanvas)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


def generate_seller_package_invoice_pdf(invoice: Invoice) -> bytes:
    """
    Generates an isolated Seller Package Commercial Invoice PDF.
    
    Guarantees:
    - Scoped strictly to the seller's package items.
    - Zero exposure of commission rates, platform fees, or net seller payable.
    - Displays fulfilling seller's legal identity and customer delivery details.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=45,
    )
    
    usable_width = A4[0] - 72
    styles = getSampleStyleSheet()
    
    brand_title_style = ParagraphStyle(
        "BrandTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#0F172A"),
    )
    doc_title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        alignment=2,
        textColor=colors.HexColor("#1E293B"),
    )
    meta_label_style = ParagraphStyle("MetaLabel", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8, leading=11, textColor=colors.HexColor("#475569"))
    meta_val_style = ParagraphStyle("MetaVal", parent=styles["Normal"], fontName="Helvetica", fontSize=8, leading=11, textColor=colors.HexColor("#0F172A"))
    table_head_style = ParagraphStyle("TableHead", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.HexColor("#FFFFFF"))
    cell_text_style = ParagraphStyle("CellText", parent=styles["Normal"], fontName="Helvetica", fontSize=8, leading=11, textColor=colors.HexColor("#1E293B"))
    cell_bold_style = ParagraphStyle("CellBold", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8, leading=11, textColor=colors.HexColor("#0F172A"))
    cell_muted_style = ParagraphStyle("CellMuted", parent=styles["Normal"], fontName="Helvetica", fontSize=7.5, leading=10, textColor=colors.HexColor("#64748B"))
    cell_right_style = ParagraphStyle("CellRight", parent=styles["Normal"], fontName="Helvetica", fontSize=8, leading=11, alignment=2, textColor=colors.HexColor("#1E293B"))
    cell_right_bold = ParagraphStyle("CellRightBold", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8, leading=11, alignment=2, textColor=colors.HexColor("#0F172A"))
    note_style = ParagraphStyle("LegalNote", parent=styles["Normal"], fontName="Helvetica", fontSize=7.5, leading=10, textColor=colors.HexColor("#475569"))

    story = []
    seller_order = invoice.seller_order
    order = invoice.order

    # 1. Header
    brand_cell = [
        Paragraph(getattr(settings, "BRAND_NAME", "Fitmeezo").upper(), brand_title_style),
        Paragraph("SELLER COMMERCIAL PACKAGE INVOICE", ParagraphStyle("SubH", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=9, textColor=colors.HexColor("#2563EB"))),
        Paragraph("Marketplace Logistics & Commercial Document", cell_muted_style),
    ]
    title_cell = [
        Paragraph("COMMERCIAL INVOICE", doc_title_style),
        Spacer(1, 2),
        Paragraph(f"<b>Invoice No:</b> {invoice.invoice_number}", ParagraphStyle("InvNo", parent=doc_title_style, fontSize=8.5, textColor=colors.HexColor("#2563EB"))),
        Paragraph(f"<b>Issue Date:</b> {invoice.issued_at.strftime('%d-%b-%Y %H:%M')}", ParagraphStyle("InvDt", parent=doc_title_style, fontSize=8, textColor=colors.HexColor("#64748B"))),
    ]
    header_table = Table([[brand_cell, title_cell]], colWidths=[usable_width * 0.55, usable_width * 0.45])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#CBD5E1"), spaceBefore=2, spaceAfter=10))

    # 2. Package & Order Metadata
    sub_num = seller_order.sub_order_number if seller_order else "-"
    meta_data = [
        [
            Paragraph("Sub-Order Package:", meta_label_style),
            Paragraph(f"#{sub_num}", meta_val_style),
            Paragraph("Payment Method:", meta_label_style),
            Paragraph(invoice.payment_method, meta_val_style),
        ],
        [
            Paragraph("Parent Order Ref:", meta_label_style),
            Paragraph(f"#{order.order_number}", meta_val_style),
            Paragraph("Fulfillment Status:", meta_label_style),
            Paragraph(seller_order.get_status_display().upper() if seller_order else "-", meta_val_style),
        ],
    ]
    meta_table = Table(meta_data, colWidths=[usable_width * 0.22, usable_width * 0.28, usable_width * 0.22, usable_width * 0.28])
    meta_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 10))

    # 3. Parties
    issuer_lines = [
        Paragraph("<b>FULFILLING MERCHANT / SELLER:</b>", cell_bold_style),
        Spacer(1, 2),
        Paragraph(f"<b>{invoice.issuer_name}</b>", cell_text_style),
    ]
    if invoice.issuer_address:
        issuer_lines.append(Paragraph(invoice.issuer_address, cell_muted_style))
    if invoice.issuer_gstin:
        issuer_lines.append(Paragraph(f"<b>GSTIN:</b> {invoice.issuer_gstin}", cell_text_style))
    else:
        issuer_lines.append(Paragraph("<b>GSTIN:</b> Unregistered / Not Applicable", cell_muted_style))

    recipient_lines = [
        Paragraph("<b>DELIVERY DESTINATION:</b>", cell_bold_style),
        Spacer(1, 2),
        Paragraph(f"<b>{invoice.recipient_name}</b>", cell_text_style),
        Paragraph(invoice.recipient_shipping_address, cell_muted_style),
    ]
    parties_table = Table([[issuer_lines, recipient_lines]], colWidths=[usable_width * 0.50, usable_width * 0.50])
    parties_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(parties_table)
    story.append(Spacer(1, 12))

    # 4. Items in this package
    item_rows = [
        [
            Paragraph("#", table_head_style),
            Paragraph("Item Description & Variant", table_head_style),
            Paragraph("SKU", table_head_style),
            Paragraph("Qty", table_head_style),
            Paragraph("Unit Price (₹)", table_head_style),
            Paragraph("Total (₹)", table_head_style),
        ]
    ]

    pkg_items = seller_order.items.select_related("variant").all() if seller_order else []
    for idx, it in enumerate(pkg_items, start=1):
        desc_parts = [Paragraph(f"<b>{it.product_name}</b>", cell_text_style)]
        if it.variant_label:
            desc_parts.append(Paragraph(f"Option: {it.variant_label}", cell_muted_style))
            
        item_rows.append([
            Paragraph(str(idx), cell_text_style),
            desc_parts,
            Paragraph(it.sku or "-", cell_muted_style),
            Paragraph(str(it.quantity), cell_text_style),
            Paragraph(f"{it.unit_price:.2f}", cell_right_style),
            Paragraph(f"{it.line_total:.2f}", cell_right_bold),
        ])

    col_widths = [
        usable_width * 0.06,
        usable_width * 0.44,
        usable_width * 0.16,
        usable_width * 0.08,
        usable_width * 0.13,
        usable_width * 0.13,
    ]
    items_table = Table(item_rows, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 10))

    # 5. Financial Summary
    summary_data = [
        [Paragraph("Package Merchandise Subtotal:", cell_bold_style), Paragraph(f"₹ {invoice.subtotal:.2f}", cell_right_style)],
    ]
    if invoice.shipping_fee > Decimal("0.00"):
        summary_data.append([Paragraph("Customer Shipping Allocated:", cell_bold_style), Paragraph(f"₹ {invoice.shipping_fee:.2f}", cell_right_style)])
        
    summary_data.append([
        Paragraph("<b>Commercial Package Total:</b>", cell_bold_style),
        Paragraph(f"<b>₹ {invoice.total_amount:.2f}</b>", cell_right_bold),
    ])

    summary_table = Table(summary_data, colWidths=[usable_width * 0.32, usable_width * 0.18])
    summary_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("LINEBELOW", (0, -2), (-1, -2), 0.5, colors.HexColor("#CBD5E1")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F1F5F9")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))

    summary_wrapper = Table([[Paragraph("", cell_text_style), summary_table]], colWidths=[usable_width * 0.50, usable_width * 0.50])
    summary_wrapper.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(KeepTogether(summary_wrapper))
    story.append(Spacer(1, 14))

    # 6. Disclaimers
    story.append(KeepTogether([
        Paragraph("<b>Confidentiality & Compliance Notice:</b>", cell_bold_style),
        Paragraph("• This commercial document represents the merchandise items contained specifically within this package.", note_style),
        Paragraph("• Internal merchant settlement, marketplace commissions, and net payout records are maintained in the Seller Financial Ledger.", note_style),
        Paragraph("• This document is for commercial and fulfillment reference. Not a carrier courier shipping label.", note_style),
    ]))

    doc.build(story, canvasmaker=NumberedCanvas)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes
