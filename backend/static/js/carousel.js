/**
 * Fitmeezo Homepage Hero Image Carousel
 * Implements smooth auto-rotation (~4s), left/right arrows, pagination dots,
 * pause-on-hover/touch, and responsive mobile swipe gestures.
 */
(function () {
  'use strict';

  function initHeroCarousel() {
    var carousel = document.getElementById('fmHeroCarousel');
    if (!carousel) return;

    var track = document.getElementById('fmCarouselTrack');
    var slides = carousel.querySelectorAll('.fm-carousel-slide');
    var prevBtn = document.getElementById('fmCarouselPrevBtn');
    var nextBtn = document.getElementById('fmCarouselNextBtn');
    var dotsContainer = document.getElementById('fmCarouselDots');
    var dots = dotsContainer ? dotsContainer.querySelectorAll('.fm-carousel-dot') : [];

    var totalSlides = slides.length;
    if (totalSlides <= 1) {
      // Single slide: no cycling or navigation required
      return;
    }

    var currentIndex = 0;
    var autoplayInterval = 4000; // ~4 seconds
    var autoplayTimer = null;
    var isPaused = false;

    // Updates track transform and active state of slides & dots
    function goToSlide(index) {
      if (index < 0) {
        currentIndex = totalSlides - 1;
      } else if (index >= totalSlides) {
        currentIndex = 0;
      } else {
        currentIndex = index;
      }

      if (track) {
        track.style.transform = 'translateX(-' + (currentIndex * 100) + '%)';
      }

      // Update active classes for slides (accessibility)
      slides.forEach(function (slide, idx) {
        if (idx === currentIndex) {
          slide.classList.add('is-active');
          slide.setAttribute('aria-hidden', 'false');
        } else {
          slide.classList.remove('is-active');
          slide.setAttribute('aria-hidden', 'true');
        }
      });

      // Update active dot
      dots.forEach(function (dot, idx) {
        if (idx === currentIndex) {
          dot.classList.add('is-active');
          dot.setAttribute('aria-selected', 'true');
        } else {
          dot.classList.remove('is-active');
          dot.setAttribute('aria-selected', 'false');
        }
      });
    }

    function nextSlide() {
      goToSlide(currentIndex + 1);
    }

    function prevSlide() {
      goToSlide(currentIndex - 1);
    }

    function startAutoplay() {
      stopAutoplay();
      if (!isPaused) {
        autoplayTimer = setInterval(nextSlide, autoplayInterval);
      }
    }

    function stopAutoplay() {
      if (autoplayTimer) {
        clearInterval(autoplayTimer);
        autoplayTimer = null;
      }
    }

    function resetAutoplay() {
      stopAutoplay();
      startAutoplay();
    }

    // Arrow Buttons
    if (prevBtn) {
      prevBtn.addEventListener('click', function (e) {
        e.preventDefault();
        prevSlide();
        resetAutoplay();
      });
    }

    if (nextBtn) {
      nextBtn.addEventListener('click', function (e) {
        e.preventDefault();
        nextSlide();
        resetAutoplay();
      });
    }

    // Pagination Dots
    dots.forEach(function (dot) {
      dot.addEventListener('click', function (e) {
        e.preventDefault();
        var targetIndex = parseInt(dot.getAttribute('data-slide-to'), 10);
        if (!isNaN(targetIndex)) {
          goToSlide(targetIndex);
          resetAutoplay();
        }
      });
    });

    // Pause on mouse hover (desktop)
    carousel.addEventListener('mouseenter', function () {
      isPaused = true;
      stopAutoplay();
    });

    carousel.addEventListener('mouseleave', function () {
      isPaused = false;
      startAutoplay();
    });

    // Keyboard navigation when carousel has focus
    carousel.setAttribute('tabindex', '0');
    carousel.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        prevSlide();
        resetAutoplay();
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        nextSlide();
        resetAutoplay();
      }
    });

    // Mobile Swipe Gestures
    var touchStartX = 0;
    var touchStartY = 0;
    var touchEndX = 0;
    var touchEndY = 0;

    carousel.addEventListener('touchstart', function (e) {
      if (e.changedTouches && e.changedTouches.length > 0) {
        touchStartX = e.changedTouches[0].screenX;
        touchStartY = e.changedTouches[0].screenY;
        stopAutoplay();
      }
    }, { passive: true });

    carousel.addEventListener('touchend', function (e) {
      if (e.changedTouches && e.changedTouches.length > 0) {
        touchEndX = e.changedTouches[0].screenX;
        touchEndY = e.changedTouches[0].screenY;
        handleSwipe();
        startAutoplay();
      }
    }, { passive: true });

    function handleSwipe() {
      var diffX = touchEndX - touchStartX;
      var diffY = touchEndY - touchStartY;
      // Must be horizontally dominant and exceed threshold
      if (Math.abs(diffX) > 35 && Math.abs(diffX) > Math.abs(diffY)) {
        if (diffX < 0) {
          nextSlide(); // Swiped left -> next
        } else {
          prevSlide(); // Swiped right -> prev
        }
        resetAutoplay();
      }
    }

    // Initial state
    goToSlide(0);
    startAutoplay();
  }

  // Initialize on DOM load
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initHeroCarousel);
  } else {
    initHeroCarousel();
  }
})();
