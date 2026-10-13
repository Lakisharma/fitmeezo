# Fitmeezo REST API Documentation
**Platform:** Fitmeezo E-Commerce (Web & Mobile Android App Backend)  
**Version:** 1.0.0  
**Base URL:** `/api/`  
**Authentication Header:** `Authorization: Token <token_key>` (or Session Cookie for Web)  
**Response Format:** `application/json`

---

## 1. Authentication & Customer Profile

### 1.1 Customer Registration
- **Method:** `POST`
- **Endpoint:** `/api/auth/register/`
- **Access:** Public
- **Request Body:**
  ```json
  {
    "full_name": "Aarav Patel",
    "email": "aarav.patel@example.com",
    "password": "SecurePassword123!",
    "confirm_password": "SecurePassword123!"
  }
  ```
- **Response (201 Created):**
  ```json
  {
    "status": "success",
    "message": "Registration successful. Welcome to Fitmeezo!",
    "token": "9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b",
    "user": {
      "id": 12,
      "username": "aarav.patel@example.com",
      "email": "aarav.patel@example.com",
      "first_name": "Aarav",
      "last_name": "Patel",
      "full_name": "Aarav Patel",
      "phone_number": "",
      "date_joined": "2026-10-10T15:30:00Z"
    }
  }
  ```

---

### 1.2 Customer Login
- **Method:** `POST`
- **Endpoint:** `/api/auth/login/`
- **Access:** Public
- **Request Body:**
  ```json
  {
    "username_or_email": "aarav.patel@example.com",
    "password": "SecurePassword123!"
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "status": "success",
    "message": "Welcome back, Aarav!",
    "token": "9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b",
    "user": {
      "id": 12,
      "username": "aarav.patel@example.com",
      "email": "aarav.patel@example.com",
      "full_name": "Aarav Patel",
      "phone_number": "+91 9876543210"
    }
  }
  ```

---

### 1.3 Customer Logout
- **Method:** `POST`
- **Endpoint:** `/api/auth/logout/`
- **Access:** Authenticated (`Authorization: Token <token_key>`)
- **Response (200 OK):**
  ```json
  {
    "status": "success",
    "message": "Successfully logged out."
  }
  ```

---

### 1.4 Profile Management
- **Method:** `GET`, `PUT`, `PATCH`
- **Endpoint:** `/api/auth/profile/`
- **Access:** Authenticated
- **Request Body (PATCH):**
  ```json
  {
    "full_name": "Aarav K. Patel",
    "phone_number": "9876543210"
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "status": "success",
    "message": "Profile updated successfully.",
    "profile": {
      "id": 12,
      "username": "aarav.patel@example.com",
      "email": "aarav.patel@example.com",
      "full_name": "Aarav K. Patel",
      "phone_number": "9876543210"
    }
  }
  ```

---

## 2. Product Catalogue

### 2.1 List Categories
- **Method:** `GET`
- **Endpoint:** `/api/categories/`
- **Access:** Public
- **Response (200 OK):**
  ```json
  {
    "status": "success",
    "count": 6,
    "categories": [
      {
        "id": 1,
        "name": "Electronics",
        "slug": "electronics",
        "description": "Smartphones, Audio & Wearables",
        "image_url": "http://127.0.0.1:8000/media/categories/electronics.jpg",
        "icon_class": "bi-laptop",
        "badge": "Hot",
        "display_order": 1,
        "product_count": 14
      }
    ]
  }
  ```

---

### 2.2 Category Details
- **Method:** `GET`
- **Endpoint:** `/api/categories/<slug>/`
- **Access:** Public
- **Response (200 OK):** Returns category metadata and up to 24 active products in this category.

---

### 2.3 Product Listing & Search
- **Method:** `GET`
- **Endpoint:** `/api/products/`
- **Access:** Public
- **Query Parameters:**
  - `q`: Search query string (title, SKU, description)
  - `category`: Category slug
  - `ordering`: `price_asc`, `price_desc`, `name`, `newest`
  - `in_stock`: `true`
  - `page`: Page number (default: 1)
  - `page_size`: Results per page (default: 12)
- **Response (200 OK):**
  ```json
  {
    "count": 42,
    "next": "http://127.0.0.1:8000/api/products/?page=2",
    "previous": null,
    "results": [
      {
        "id": 5,
        "name": "FitPulse Pro Max Smartwatch",
        "slug": "fitpulse-pro-max-smartwatch",
        "sku": "EL-SMW-001",
        "category_name": "Electronics",
        "category_slug": "electronics",
        "short_description": "1.43-inch AMOLED display with Bluetooth calling",
        "price": "2499.00",
        "compare_at_price": "4999.00",
        "discount_percentage": 50,
        "image_url": "http://127.0.0.1:8000/media/products/smartwatch.jpg",
        "is_in_stock": true,
        "available_inventory": 35,
        "has_variants": true,
        "is_featured": true,
        "average_rating": 4.8,
        "review_count": 312
      }
    ]
  }
  ```

---

### 2.4 Product Details
- **Method:** `GET`
- **Endpoint:** `/api/products/<slug>/`
- **Access:** Public
- **Response (200 OK):**
  Full specifications, gallery images, active variants with option types, values, prices, and stock indicators, and customer review star breakdown.

---

## 3. Customer Saved Addresses

### 3.1 List and Add Addresses
- **Method:** `GET`, `POST`
- **Endpoint:** `/api/addresses/`
- **Access:** Authenticated
- **Request Body (POST):**
  ```json
  {
    "full_name": "Aarav Patel",
    "phone_number": "9876543210",
    "address_line1": "Flat 402, Shanti Heights, MG Road",
    "address_line2": "Near Shivaji Park",
    "city": "Mumbai",
    "state": "Maharashtra",
    "postal_code": "400028",
    "landmark": "Opposite Metro Station",
    "address_type": "home",
    "is_default": true
  }
  ```

---

### 3.2 Address Details & Operations
- **Method:** `GET`, `PUT`, `PATCH`, `DELETE`
- **Endpoint:** `/api/addresses/<id>/`
- **Access:** Authenticated (Strict user ownership enforced)

### 3.3 Set Default Address
- **Method:** `POST`
- **Endpoint:** `/api/addresses/<id>/set-default/`
- **Access:** Authenticated

---

## 4. Shopping Cart

### 4.1 Get Cart
- **Method:** `GET`
- **Endpoint:** `/api/cart/`
- **Access:** Public / Authenticated (Supports hybrid persistent + session cart)
- **Response (200 OK):**
  ```json
  {
    "status": "success",
    "cart": {
      "lines": [
        {
          "key": "5_12",
          "product_id": 5,
          "product_name": "FitPulse Pro Max Smartwatch",
          "product_slug": "fitpulse-pro-max-smartwatch",
          "variant_id": 12,
          "variant_label": "Color: Midnight Black",
          "sku": "EL-SMW-001-BLK",
          "unit_price": "2499.00",
          "compare_at_price": "4999.00",
          "quantity": 1,
          "line_subtotal": "2499.00",
          "line_discount": "2500.00",
          "discount_percent": 50,
          "available_stock": 18,
          "is_available": true,
          "has_stock_warning": false,
          "warning_message": null
        }
      ],
      "total_units": 1,
      "subtotal": "2499.00",
      "original_subtotal": "4999.00",
      "total_discount": "2500.00",
      "total_payable": "2499.00",
      "is_empty": false,
      "is_checkout_ready": true,
      "has_warnings": false
    }
  }
  ```

---

### 4.2 Add to Cart
- **Method:** `POST`
- **Endpoint:** `/api/cart/add/`
- **Access:** Public / Authenticated
- **Request Body:**
  ```json
  {
    "product_id": 5,
    "variant_id": 12,
    "quantity": 1
  }
  ```

---

### 4.3 Update Quantity
- **Method:** `POST`
- **Endpoint:** `/api/cart/update/`
- **Request Body:**
  ```json
  {
    "key": "5_12",
    "quantity": 2
  }
  ```

---

### 4.4 Remove Line Item
- **Method:** `POST`
- **Endpoint:** `/api/cart/remove/`
- **Request Body:**
  ```json
  {
    "key": "5_12"
  }
  ```

---

### 4.5 Clear Cart
- **Method:** `POST`
- **Endpoint:** `/api/cart/clear/`

---

## 5. Checkout & Order Fulfillment

### 5.1 Checkout Summary
- **Method:** `GET`
- **Endpoint:** `/api/checkout/summary/`
- **Access:** Authenticated
- **Response (200 OK):**
  Returns live cart subtotal, shipping fee (free over ₹499), discount savings, total amount payable, and saved addresses.

---

### 5.2 Place Order (Cash on Delivery)
- **Method:** `POST`
- **Endpoint:** `/api/checkout/place-order/`
- **Access:** Authenticated
- **Request Body:**
  ```json
  {
    "address_id": 4,
    "payment_method": "cod",
    "customer_notes": "Please call before delivery",
    "idempotency_token": "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
  }
  ```
- **Response (201 Created):**
  ```json
  {
    "status": "success",
    "message": "Order #FM-9B2F3A10 confirmed successfully!",
    "order": {
      "id": 28,
      "order_number": "FM-9B2F3A10",
      "created_at": "2026-10-10T16:00:00Z",
      "status": "pending",
      "status_display": "Pending",
      "payment_method": "cod",
      "payment_method_display": "Cash on Delivery (COD)",
      "payment_status": "unpaid",
      "payment_status_display": "Unpaid",
      "subtotal": "2499.00",
      "shipping_fee": "0.00",
      "discount_amount": "2500.00",
      "total_amount": "2499.00",
      "currency": "INR",
      "shipping_address": {
        "full_name": "Aarav Patel",
        "phone_number": "9876543210",
        "address_line1": "Flat 402, Shanti Heights",
        "city": "Mumbai",
        "state": "Maharashtra",
        "postal_code": "400028",
        "formatted": "Flat 402, Shanti Heights, Mumbai, Maharashtra - 400028"
      },
      "items": [
        {
          "id": 41,
          "product_name": "FitPulse Pro Max Smartwatch",
          "variant_label": "Color: Midnight Black",
          "sku": "EL-SMW-001-BLK",
          "unit_price": "2499.00",
          "quantity": 1,
          "line_total": "2499.00"
        }
      ]
    }
  }
  ```

---

## 6. Customer Order History

### 6.1 Order List
- **Method:** `GET`
- **Endpoint:** `/api/orders/`
- **Access:** Authenticated (Isolated strictly to `request.user`)
- **Response (200 OK):** Paginated list of past orders with status badges and financial summaries.

---

### 6.2 Order Details
- **Method:** `GET`
- **Endpoint:** `/api/orders/<order_number>/`
- **Access:** Authenticated
- **Isolation Policy:** If the order belongs to another customer, returns `404 Not Found` without disclosing any details.

---

### 6.3 Cancel Order
- **Method:** `POST`
- **Endpoint:** `/api/orders/<order_number>/cancel/`
- **Access:** Authenticated (`Authorization: Token <token_key>`)
- **Request Body (Optional JSON):**
  ```json
  {
    "reason": "Changed my mind"
  }
  ```
- **Business Rules:**
  - Allowed only for orders in `pending` or `processing` status.
  - Returns `400 Bad Request` if the order is already `shipped`, `delivered`, or `cancelled`.
  - Atomically restores variant or product stock to inventory exactly once.
  - If the order was paid online, sets `refund_status: "pending"` without claiming that a refund has been issued.
  - For COD orders, sets `refund_status: "not_applicable"`.
- **Response (200 OK):**
  ```json
  {
    "status": "success",
    "message": "Order #FM-9B2F10A2 has been cancelled successfully.",
    "order": {
      "id": 14,
      "order_number": "FM-9B2F10A2",
      "status": "cancelled",
      "status_display": "Cancelled",
      "is_cancellable": false,
      "payment_method": "cod",
      "payment_status": "unpaid",
      "refund_status": "not_applicable",
      "refund_status_display": "Not Applicable",
      "inventory_restored": true,
      "cancelled_at": "2026-10-10T16:20:00Z",
      "cancellation_reason": "Changed my mind"
    }
  }
  ```
- **Error Response (400 Bad Request):**
  ```json
  {
    "status": "error",
    "message": "Order #FM-9B2F10A2 cannot be cancelled because it is already 'Shipped'."
  }
  ```

