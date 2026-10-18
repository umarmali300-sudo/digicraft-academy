(function () {
  var menuButton = document.querySelector(".menu-button");
  var nav = document.getElementById("site-nav");
  if (menuButton && nav) {
    menuButton.addEventListener("click", function () {
      var open = nav.classList.toggle("is-open");
      menuButton.setAttribute("aria-expanded", open ? "true" : "false");
      menuButton.textContent = open ? "Close" : "Menu";
    });
    nav.querySelectorAll("a").forEach(function (link) {
      link.addEventListener("click", function () {
        nav.classList.remove("is-open");
        menuButton.setAttribute("aria-expanded", "false");
        menuButton.textContent = "Menu";
      });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        nav.classList.remove("is-open");
        menuButton.setAttribute("aria-expanded", "false");
        menuButton.textContent = "Menu";
        menuButton.focus();
      }
    });
  }

  var sectionLinks = Array.prototype.slice.call(document.querySelectorAll(".site-nav a[data-section]"));
  if (sectionLinks.length) {
    var spy = function () {
      var marker = window.scrollY + 140;
      var current = sectionLinks[0];
      sectionLinks.forEach(function (link) {
        var section = document.getElementById(link.getAttribute("data-section"));
        if (section && section.offsetTop <= marker) current = link;
      });
      sectionLinks.forEach(function (link) {
        if (link === current) link.setAttribute("aria-current", "true");
        else link.removeAttribute("aria-current");
      });
    };
    spy();
    window.addEventListener("scroll", spy, { passive: true });
  }

  if (window.location.pathname === "/" && window.location.search && !window.location.hash) {
    var courses = document.getElementById("courses");
    if (courses) courses.scrollIntoView();
  }

  document.querySelectorAll("[data-password-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      var input = document.getElementById(button.getAttribute("data-password-toggle"));
      if (!input) return;
      var showing = input.type === "text";
      input.type = showing ? "password" : "text";
      button.textContent = showing ? "Show" : "Hide";
      button.setAttribute("aria-pressed", showing ? "false" : "true");
    });
  });
})();
