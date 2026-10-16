/**
 * Fitmeezo Management Console - Enterprise UI Scripts
 */

document.addEventListener("DOMContentLoaded", function () {
  // 1. Mobile Sidebar Toggle
  const sidebar = document.getElementById("consoleSidebar");
  const toggleBtn = document.getElementById("consoleSidebarToggle");

  if (toggleBtn && sidebar) {
    toggleBtn.addEventListener("click", function () {
      sidebar.classList.toggle("open");
    });

    // Close when clicking outside on mobile
    document.addEventListener("click", function (e) {
      if (
        window.innerWidth <= 1024 &&
        sidebar.classList.contains("open") &&
        !sidebar.contains(e.target) &&
        !toggleBtn.contains(e.target)
      ) {
        sidebar.classList.remove("open");
      }
    });
  }

  // 2. Collapsible Nav Groups with LocalStorage Persistence
  const navGroupHeaders = document.querySelectorAll(".console-nav-group-title");
  navGroupHeaders.forEach(function (header) {
    const groupId = header.getAttribute("data-group");
    const container = document.getElementById("group-" + groupId);

    // Restore state
    const savedState = localStorage.getItem("mc_group_" + groupId);
    if (savedState === "collapsed" && container) {
      container.style.display = "none";
      header.classList.add("collapsed");
    }

    header.addEventListener("click", function () {
      if (!container) return;
      const isVisible = container.style.display !== "none";
      if (isVisible) {
        container.style.display = "none";
        header.classList.add("collapsed");
        localStorage.setItem("mc_group_" + groupId, "collapsed");
      } else {
        container.style.display = "flex";
        header.classList.remove("collapsed");
        localStorage.setItem("mc_group_" + groupId, "expanded");
      }
    });
  });

  // 3. Global Search Keyboard Shortcut (Ctrl+K or /)
  const searchInput = document.getElementById("globalSearchInput");
  if (searchInput) {
    document.addEventListener("keydown", function (e) {
      if (
        (e.key === "/" || (e.key === "k" && (e.ctrlKey || e.metaKey))) &&
        document.activeElement !== searchInput &&
        !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)
      ) {
        e.preventDefault();
        searchInput.focus();
        searchInput.select();
      }
    });
  }

  // 4. Auto-dismiss flash alerts after 6 seconds
  const alerts = document.querySelectorAll(".console-alert");
  alerts.forEach(function (alert) {
    setTimeout(function () {
      alert.style.transition = "opacity 0.4s ease, transform 0.4s ease";
      alert.style.opacity = "0";
      alert.style.transform = "translateY(-6px)";
      setTimeout(function () {
        alert.remove();
      }, 400);
    }, 6000);
  });
});
