"""
Fitmeezo Custom Admin Dashboard - URL Configuration.
"""

from django.urls import path
from .admin_views import (
    admin_category_add_view,
    admin_category_delete_view,
    admin_category_edit_view,
    admin_category_list_view,
    admin_category_toggle_status_view,
    admin_carousel_list_view,
    admin_carousel_add_view,
    admin_carousel_edit_view,
    admin_carousel_toggle_status_view,
    admin_carousel_delete_view,
    admin_dashboard_view,
    admin_login_view,
    admin_logout_view,
    admin_order_detail_view,
    admin_order_list_view,
    admin_order_reconcile_refund_view,
    admin_order_update_payment_view,
    admin_order_update_status_view,
    admin_product_add_view,
    admin_product_detail_view,
    admin_product_edit_view,
    admin_product_image_add_view,
    admin_product_image_delete_view,
    admin_product_list_view,
    admin_product_toggle_status_view,
    admin_product_variant_add_view,
    admin_product_variant_delete_view,
    admin_product_variant_edit_view,
    admin_product_variant_toggle_view,
    admin_ledger_list_view,
    admin_export_ledger_csv_view,
    admin_manual_adjustment_view,
    admin_cod_reconcile_view,
    admin_payout_batch_list_view,
    admin_payout_batch_create_view,
    admin_payout_batch_detail_view,
    admin_payout_batch_record_manual_transfer_view,
    admin_payout_batch_process_view,
    admin_payout_batch_reverse_view,
    admin_seller_payout_management_view,
    admin_seller_payout_review_view,
    admin_seller_release_hold_entries_view,
    admin_generate_cycle_payout_batch_view,
    admin_payout_batch_approve_view,
    admin_payout_batch_hold_view,
    admin_payout_batch_release_view,
    admin_payout_batch_reject_view,
    admin_seller_payout_history_view,
    admin_invoice_list_view,
    admin_invoice_pdf_view,
    admin_seller_bank_verification_view,
    admin_return_list_view,
    admin_return_detail_view,
    admin_return_transition_view,
    admin_return_reject_view,
    admin_return_refund_view,
    admin_seller_closure_list_view,
    admin_seller_closure_detail_view,
    admin_seller_closure_action_view,
    admin_seller_closure_create_final_batch_view,
    admin_seller_closure_finalize_view,
)
from .admin_extended_views import (
    admin_seller_list_view,
    admin_seller_approvals_view,
    admin_seller_detail_view,
    admin_seller_action_view,
    admin_seller_commission_update_view,
    admin_product_approval_view,
    admin_product_delete_view,
    admin_inventory_view,
    admin_inventory_update_view,
    admin_customer_list_view,
    admin_customer_detail_view,
    admin_customer_toggle_status_view,
    admin_commissions_view,
    admin_reports_view,
    admin_export_report_csv_view,
    admin_users_view,
    admin_user_create_view,
    admin_user_role_update_view,
    admin_user_toggle_status_view,
    admin_audit_logs_view,
    admin_settings_view,
    admin_global_search_view,
)

app_name = "custom_admin"

urlpatterns = [
    # Staff Authentication
    path("login/", admin_login_view, name="login"),
    path("logout/", admin_logout_view, name="logout"),

    # Store Overview
    path("", admin_dashboard_view, name="dashboard"),

    # Seller Management & Approvals
    path("sellers/", admin_seller_list_view, name="seller_list"),
    path("seller-approvals/", admin_seller_approvals_view, name="seller_approvals"),
    path("sellers/<int:seller_id>/", admin_seller_detail_view, name="seller_detail"),
    path("sellers/<int:seller_id>/action/", admin_seller_action_view, name="seller_action"),
    path("sellers/<int:seller_id>/commission/", admin_seller_commission_update_view, name="seller_commission_update"),
    path("sellers/<int:seller_id>/bank-verify/", admin_seller_bank_verification_view, name="seller_bank_verify"),
    path("seller-closures/", admin_seller_closure_list_view, name="seller_closure_list"),
    path("seller-closures/<int:seller_id>/", admin_seller_closure_detail_view, name="seller_closure_detail"),
    path("seller-closures/<int:seller_id>/action/", admin_seller_closure_action_view, name="seller_closure_action"),
    path("seller-closures/<int:seller_id>/create-final-batch/", admin_seller_closure_create_final_batch_view, name="seller_closure_create_final_batch"),
    path("seller-closures/<int:seller_id>/finalize/", admin_seller_closure_finalize_view, name="seller_closure_finalize"),

    # Product Catalogue
    path("products/", admin_product_list_view, name="product_list"),
    path("products/add/", admin_product_add_view, name="product_add"),
    path("products/<int:pk>/", admin_product_detail_view, name="product_detail"),
    path("products/<int:pk>/edit/", admin_product_edit_view, name="product_edit"),
    path("products/<int:pk>/delete/", admin_product_delete_view, name="product_delete"),
    path("products/<int:pk>/approval/", admin_product_approval_view, name="product_approval"),
    path("products/<int:pk>/toggle-status/", admin_product_toggle_status_view, name="product_toggle_status"),
    path("products/<int:pk>/images/add/", admin_product_image_add_view, name="product_image_add"),
    path("products/<int:pk>/images/<int:image_pk>/delete/", admin_product_image_delete_view, name="product_image_delete"),
    path("products/<int:pk>/variants/add/", admin_product_variant_add_view, name="product_variant_add"),
    path("products/<int:pk>/variants/<int:variant_pk>/edit/", admin_product_variant_edit_view, name="product_variant_edit"),
    path("products/<int:pk>/variants/<int:variant_pk>/toggle/", admin_product_variant_toggle_view, name="product_variant_toggle"),
    path("products/<int:pk>/variants/<int:variant_pk>/delete/", admin_product_variant_delete_view, name="product_variant_delete"),

    # Category Management
    path("categories/", admin_category_list_view, name="category_list"),
    path("categories/add/", admin_category_add_view, name="category_add"),
    path("categories/<int:pk>/edit/", admin_category_edit_view, name="category_edit"),
    path("categories/<int:pk>/toggle-status/", admin_category_toggle_status_view, name="category_toggle_status"),
    path("categories/<int:pk>/delete/", admin_category_delete_view, name="category_delete"),

    # Storefront Hero Carousel Management
    path("carousel/", admin_carousel_list_view, name="carousel_list"),
    path("carousel/add/", admin_carousel_add_view, name="carousel_add"),
    path("carousel/<int:pk>/edit/", admin_carousel_edit_view, name="carousel_edit"),
    path("carousel/<int:pk>/toggle-status/", admin_carousel_toggle_status_view, name="carousel_toggle_status"),
    path("carousel/<int:pk>/delete/", admin_carousel_delete_view, name="carousel_delete"),

    # Inventory
    path("inventory/", admin_inventory_view, name="inventory"),
    path("inventory/update/", admin_inventory_update_view, name="inventory_update"),

    # Order Management & Fulfillment
    path("orders/", admin_order_list_view, name="order_list"),
    path("orders/<int:pk>/", admin_order_detail_view, name="order_detail"),
    path("orders/<int:pk>/update-status/", admin_order_update_status_view, name="order_update_status"),
    path("orders/<int:pk>/update-payment/", admin_order_update_payment_view, name="order_update_payment"),
    path("orders/<int:pk>/reconcile-refund/", admin_order_reconcile_refund_view, name="order_reconcile_refund"),

    # Customers
    path("customers/", admin_customer_list_view, name="customer_list"),
    path("customers/<int:user_id>/", admin_customer_detail_view, name="customer_detail"),
    path("customers/<int:user_id>/toggle-status/", admin_customer_toggle_status_view, name="customer_toggle_status"),

    # Returns & Exchange Management
    path("returns/", admin_return_list_view, name="return_list"),
    path("returns/<str:request_number>/", admin_return_detail_view, name="return_detail"),
    path("returns/<str:request_number>/transition/", admin_return_transition_view, name="return_transition"),
    path("returns/<str:request_number>/reject/", admin_return_reject_view, name="return_reject"),
    path("returns/<str:request_number>/refund/", admin_return_refund_view, name="return_refund"),

    # Financial Ledger & COD
    path("ledger/", admin_ledger_list_view, name="ledger_list"),
    path("ledger/export/", admin_export_ledger_csv_view, name="ledger_export_csv"),
    path("ledger/adjustment/", admin_manual_adjustment_view, name="manual_adjustment"),
    path("cod/reconcile/", admin_cod_reconcile_view, name="cod_reconcile"),

    # Commissions
    path("commissions/", admin_commissions_view, name="commissions"),

    # Seller Payout Management Dashboard & Cycle Controls
    path("payout-management/", admin_seller_payout_management_view, name="seller_payout_management"),
    path("payout-management/generate/", admin_generate_cycle_payout_batch_view, name="seller_payout_generate"),
    path("payout-management/review/<int:seller_id>/", admin_seller_payout_review_view, name="seller_payout_review"),
    path("payout-management/seller/<int:seller_id>/review/", admin_seller_payout_review_view),
    path("payout-management/seller/<int:seller_id>/release-hold/", admin_seller_release_hold_entries_view, name="seller_payout_release_hold"),
    path("payout-management/review/<int:seller_id>/release-hold/", admin_seller_release_hold_entries_view),
    path("payout-management/seller/<int:seller_id>/history/", admin_seller_payout_history_view, name="seller_payout_history"),
    path("payouts/seller/<int:seller_id>/history/", admin_seller_payout_history_view),

    # Payout Batches
    path("payouts/", admin_payout_batch_list_view, name="payout_list"),
    path("payouts/create/", admin_payout_batch_create_view, name="payout_create"),
    path("payouts/<str:batch_reference>/", admin_payout_batch_detail_view, name="payout_detail"),
    path("payout-management/batches/<str:batch_reference>/", admin_payout_batch_detail_view),
    path("payouts/<str:batch_reference>/record-transfer/", admin_payout_batch_record_manual_transfer_view, name="payout_record_manual_transfer"),
    path("payout-management/batches/<str:batch_reference>/record-transfer/", admin_payout_batch_record_manual_transfer_view),
    path("payouts/<str:batch_reference>/process/", admin_payout_batch_process_view, name="payout_process"),
    path("payouts/<str:batch_reference>/reverse/", admin_payout_batch_reverse_view, name="payout_reverse"),
    path("payouts/<str:batch_reference>/approve/", admin_payout_batch_approve_view, name="payout_approve"),
    path("payout-management/batches/<str:batch_reference>/approve/", admin_payout_batch_approve_view),
    path("payouts/<str:batch_reference>/hold/", admin_payout_batch_hold_view, name="payout_hold"),
    path("payout-management/batches/<str:batch_reference>/hold/", admin_payout_batch_hold_view),
    path("payouts/<str:batch_reference>/release/", admin_payout_batch_release_view, name="payout_release"),
    path("payout-management/batches/<str:batch_reference>/release/", admin_payout_batch_release_view),
    path("payouts/<str:batch_reference>/reject/", admin_payout_batch_reject_view, name="payout_reject"),
    path("payout-management/batches/<str:batch_reference>/reject/", admin_payout_batch_reject_view),

    # Reports
    path("reports/", admin_reports_view, name="reports"),
    path("reports/export/<str:report_type>/", admin_export_report_csv_view, name="reports_export"),

    # Admin Users and Roles
    path("users/", admin_users_view, name="users_list"),
    path("users/create/", admin_user_create_view, name="user_create"),
    path("users/<int:user_id>/role/", admin_user_role_update_view, name="user_role_update"),
    path("users/<int:user_id>/toggle/", admin_user_toggle_status_view, name="user_toggle_status"),

    # Audit Logs & Settings
    path("audit-logs/", admin_audit_logs_view, name="audit_logs"),
    path("settings/", admin_settings_view, name="settings"),

    # Global Quick Search
    path("search/", admin_global_search_view, name="global_search"),

    # Invoices & Tax Records
    path("invoices/", admin_invoice_list_view, name="invoice_list"),
    path("invoices/<int:pk>/pdf/", admin_invoice_pdf_view, name="invoice_pdf"),
]
