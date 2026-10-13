/**
 * Fitmeezo Main JavaScript
 * Layout interactions, accessibility, mobile offcanvas drawer,
 * placeholder toast notifications, and search keyboard shortcuts.
 */

document.addEventListener('DOMContentLoaded', () => {
  initMobileDrawer();
  initMobileCategoryStrip();
  initAnnouncementBar();
  initPlaceholderHandler();
  initSearchShortcuts();
  initStorefrontActions();
});


/**
 * Accessible Mobile Offcanvas Drawer
 */
function initMobileDrawer() {
  const menuToggle = document.getElementById('fmMenuToggle');
  const drawer = document.getElementById('fmMobileDrawer');
  const backdrop = document.getElementById('fmDrawerBackdrop');
  const drawerClose = document.getElementById('fmDrawerClose');

  if (!menuToggle || !drawer || !backdrop) return;

  function openDrawer() {
    drawer.classList.add('is-open');
    backdrop.classList.add('is-open');
    menuToggle.setAttribute('aria-expanded', 'true');
    drawer.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';

    // Focus close button inside drawer for keyboard accessibility
    if (drawerClose) {
      setTimeout(() => drawerClose.focus(), 150);
    }
  }

  function closeDrawer() {
    drawer.classList.remove('is-open');
    backdrop.classList.remove('is-open');
    menuToggle.setAttribute('aria-expanded', 'false');
    drawer.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    menuToggle.focus();
  }

  menuToggle.addEventListener('click', openDrawer);
  if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
  backdrop.addEventListener('click', closeDrawer);

  // Allow bottom nav Categories button to toggle/open drawer
  const bottomCatBtn = document.getElementById('fmBottomCategoriesBtn');
  if (bottomCatBtn) {
    bottomCatBtn.addEventListener('click', () => {
      if (drawer.classList.contains('is-open')) {
        closeDrawer();
      } else {
        openDrawer();
      }
    });
  }

  // Close drawer on Escape key
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && drawer.classList.contains('is-open')) {
      closeDrawer();
    }
  });
}
 
/**
 * Mobile Horizontal Category Strip Interaction
 */
function initMobileCategoryStrip() {
  const catItems = document.querySelectorAll('.fm-mobile-cat-item');
  if (!catItems.length) return;

  catItems.forEach((item) => {
    item.addEventListener('click', (e) => {
      catItems.forEach((el) => {
        el.classList.remove('is-active');
        el.setAttribute('aria-selected', 'false');
      });
      item.classList.add('is-active');
      item.setAttribute('aria-selected', 'true');
    });
  });
}

/**
 * Top Announcement Bar Dismissal
 */
function initAnnouncementBar() {
  const bar = document.getElementById('fmAnnouncementBar');
  const closeBtn = document.getElementById('fmDismissAnnouncement');

  if (!bar || !closeBtn) return;

  // Check session storage
  if (sessionStorage.getItem('fm_announcement_dismissed') === 'true') {
    bar.style.display = 'none';
  }

  closeBtn.addEventListener('click', () => {
    bar.style.transition = 'opacity 200ms ease, max-height 200ms ease';
    bar.style.opacity = '0';
    setTimeout(() => {
      bar.style.display = 'none';
      sessionStorage.setItem('fm_announcement_dismissed', 'true');
    }, 200);
  });
}

/**
 * Toast Notification System for Placeholder Elements
 */
function showToast(message, title = 'Fitmeezo Preview') {
  let container = document.getElementById('fmToastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'fmToastContainer';
    container.className = 'fm-toast-container';
    document.body.appendChild(container);
  }

  const toast = document.createElement('div');
  toast.className = 'fm-toast';
  toast.setAttribute('role', 'status');
  toast.setAttribute('aria-live', 'polite');

  toast.innerHTML = `
    <div>
      <div style="font-weight: 700; font-size: 13px; color: #FF6B00; margin-bottom: 2px;">${title}</div>
      <div style="font-size: 13px; color: #E0E0E0;">${message}</div>
    </div>
    <button type="button" class="fm-toast-close" aria-label="Close message">&times;</button>
  `;

  container.appendChild(toast);

  // Force layout before adding animation class
  requestAnimationFrame(() => {
    toast.classList.add('is-visible');
  });

  const closeBtn = toast.querySelector('.fm-toast-close');
  function removeToast() {
    toast.classList.remove('is-visible');
    setTimeout(() => toast.remove(), 300);
  }

  closeBtn.addEventListener('click', removeToast);

  // Auto-dismiss after 4.5 seconds
  setTimeout(removeToast, 4500);
}

/**
 * Safe Placeholder Link and Button Handler
 */
function initPlaceholderHandler() {
  // Capture all links and buttons marked with data-placeholder
  document.addEventListener('click', (e) => {
    const placeholderEl = e.target.closest('[data-placeholder="true"], a[href="#"]');
    if (placeholderEl) {
      e.preventDefault();
      const label = placeholderEl.getAttribute('data-placeholder-label') ||
                    placeholderEl.getAttribute('aria-label') ||
                    placeholderEl.textContent.trim() ||
                    'This section';
      showToast(`${label} is currently a placeholder for Phase 1. Complete functionality will be built in an upcoming phase.`);
    }
  });
}

/**
 * Keyboard Shortcuts
 */
function initSearchShortcuts() {
  const searchInput = document.getElementById('fmSearchInput');
  if (!searchInput) return;

  document.addEventListener('keydown', (e) => {
    // Pressing '/' when not already typing in an input focuses the search bar
    if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
      e.preventDefault();
      searchInput.focus();
      searchInput.select();
    }
  });
}

/**
 * Product Card and Storefront Actions
 * Interactive client-side preview for cart badge increment,
 * wishlist toggle, and newsletter demo submission.
 */
function initStorefrontActions() {
  let cartCount = 0;
  let wishlistCount = 0;

function getCookie(name) {
  let cookieValue = null;
  if (document.cookie && document.cookie !== '') {
    const cookies = document.cookie.split(';');
    for (let i = 0; i < cookies.length; i++) {
      const cookie = cookies[i].trim();
      if (cookie.substring(0, name.length + 1) === (name + '=')) {
        cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
        break;
      }
    }
  }
  return cookieValue;
}

  document.addEventListener('click', (e) => {
    // Add to cart click
    const addCartBtn = e.target.closest('[data-action="add-to-cart"]');
    if (addCartBtn) {
      if (addCartBtn.closest('form')) {
        return;
      }
      e.preventDefault();
      const productTitle = addCartBtn.getAttribute('data-product-title') || 'Product';
      const productId = addCartBtn.getAttribute('data-product-id');

      if (!productId) {
        showToast(`Please view "${productTitle}" details to choose options.`, 'Select Options');
        return;
      }

      addCartBtn.disabled = true;
      const csrftoken = getCookie('csrftoken');
      const formData = new FormData();
      formData.append('product_id', productId);
      formData.append('quantity', '1');

      fetch('/cart/add/', {
        method: 'POST',
        body: formData,
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
          'X-CSRFToken': csrftoken,
        }
      })
      .then(res => res.json())
      .then(data => {
        addCartBtn.disabled = false;
        if (data.success) {
          document.querySelectorAll('#fmHeaderCartBadge, #fmBottomCartBadge, #fmDrawerCartBadge, .fm-bottom-cart-badge')
            .forEach(b => { b.textContent = data.cart_total_units; });
          const headerPrice = document.getElementById('fmHeaderCartPrice');
          if (headerPrice && data.cart_subtotal) {
            headerPrice.textContent = '₹' + parseFloat(data.cart_subtotal).toFixed(2);
          }
          showToast(data.message, 'Added to Cart');
        } else {
          showToast(data.message, 'Notice');
        }
      })
      .catch(err => {
        addCartBtn.disabled = false;
        window.location.href = '/cart/';
      });
      return;
    }

    // Wishlist toggle click
    const wishlistBtn = e.target.closest('[data-action="wishlist-toggle"]');
    if (wishlistBtn) {
      e.preventDefault();
      e.stopPropagation();

      const productTitle = wishlistBtn.getAttribute('data-product-title') || 'Product';
      const productId = wishlistBtn.getAttribute('data-product-id') ||
                        wishlistBtn.closest('[data-product-id]')?.getAttribute('data-product-id');

      if (!productId) {
        showToast(`Could not identify product "${productTitle}".`, 'Wishlist');
        return;
      }

      wishlistBtn.disabled = true;
      const csrftoken = getCookie('csrftoken');
      const formData = new FormData();
      formData.append('product_id', productId);
      formData.append('next', window.location.pathname + window.location.search);

      fetch('/wishlist/toggle/', {
        method: 'POST',
        body: formData,
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
          'X-CSRFToken': csrftoken,
        }
      })
      .then(res => {
        if (res.status === 401) {
          return res.json().then(data => {
            if (data && data.login_url) {
              window.location.href = data.login_url;
            } else {
              window.location.href = '/account/login/?next=' + encodeURIComponent(window.location.pathname + window.location.search);
            }
          });
        }
        return res.json();
      })
      .then(data => {
        wishlistBtn.disabled = false;
        if (!data) return;

        if (data.success) {
          const isSaved = data.is_in_wishlist;
          const icon = wishlistBtn.querySelector('i');
          const hasMe1 = icon && icon.className.includes('me-1');

          if (isSaved) {
            wishlistBtn.classList.add('is-active');
            if (icon) {
              icon.className = 'bi bi-heart-fill text-danger' + (hasMe1 ? ' me-1' : '');
            }
            const labelSpan = wishlistBtn.querySelector('span');
            if (labelSpan && labelSpan.textContent.includes('Wishlist')) {
              labelSpan.textContent = 'In Wishlist';
            }
            wishlistBtn.setAttribute('title', 'Remove from Wishlist');
            wishlistBtn.setAttribute('aria-label', `Remove ${productTitle} from wishlist`);
          } else {
            wishlistBtn.classList.remove('is-active');
            if (icon) {
              icon.className = 'bi bi-heart' + (hasMe1 ? ' me-1' : '');
            }
            const labelSpan = wishlistBtn.querySelector('span');
            if (labelSpan && labelSpan.textContent.includes('Wishlist')) {
              labelSpan.textContent = 'Save to Wishlist';
            }
            wishlistBtn.setAttribute('title', 'Save to Wishlist');
            wishlistBtn.setAttribute('aria-label', `Add ${productTitle} to wishlist`);
          }

          // Update all wishlist badge counters across page
          document.querySelectorAll('#fmHeaderWishlistBadge, #fmBottomWishlistBadge, #fmDrawerWishlistBadge, .fm-bottom-wishlist-badge')
            .forEach(badge => {
              badge.textContent = data.wishlist_count;
            });

          // Update header & bottom nav heart icons
          const headerIcon = document.getElementById('fmHeaderWishlistIcon');
          if (headerIcon) {
            headerIcon.className = data.wishlist_count > 0 ? 'bi bi-heart-fill text-danger' : 'bi bi-heart';
          }
          const bottomIcon = document.getElementById('fmBottomWishlistIcon');
          if (bottomIcon) {
            bottomIcon.className = data.wishlist_count > 0 ? 'bi bi-heart-fill text-danger' : 'bi bi-heart';
          }

          showToast(data.message, 'Wishlist');
        } else if (data.message) {
          showToast(data.message, 'Notice');
        }
      })
      .catch(err => {
        wishlistBtn.disabled = false;
        console.error('Wishlist toggle error:', err);
      });
      return;
    }
  });

  // Newsletter form submission
  const newsletterForm = document.getElementById('fmNewsletterForm');
  if (newsletterForm) {
    newsletterForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const emailInput = document.getElementById('fmNewsletterEmail');
      const email = emailInput ? emailInput.value.trim() : '';
      if (email) {
        showToast(`Thank you for subscribing (${email})! Demo subscription recorded. Backend email delivery will connect in an upcoming phase.`, 'Subscribed');
        if (emailInput) emailInput.value = '';
      }
    });
  }
}

