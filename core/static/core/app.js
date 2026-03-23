(() => {
  const toggle = document.getElementById("menuToggle");
  const desktopToggle = document.getElementById("sidebarCollapseToggle");
  const overlay = document.getElementById("navOverlay");
  const desktopMedia = window.matchMedia("(min-width: 901px)");
  const desktopStorageKey = "yeo_sidebar_collapsed";

  if (!toggle && !desktopToggle) return;

  const closeNav = () => document.body.classList.remove("nav-open");
  const openNav = () => document.body.classList.add("nav-open");
  const isDesktop = () => desktopMedia.matches;
  const isCollapsed = () => document.body.classList.contains("sidebar-collapsed");

  const persistDesktopState = (collapsed) => {
    try {
      window.localStorage.setItem(desktopStorageKey, collapsed ? "1" : "0");
    } catch (error) {
      // Ignore storage failures and keep the in-memory state.
    }
  };

  const setDesktopCollapsed = (collapsed) => {
    if (!isDesktop()) return;
    document.body.classList.toggle("sidebar-collapsed", collapsed);
    persistDesktopState(collapsed);
    if (desktopToggle) {
      desktopToggle.textContent = collapsed ? "Expandir menu" : "Contraer menu";
      desktopToggle.setAttribute("aria-pressed", collapsed ? "true" : "false");
    }
  };

  const syncNavMode = () => {
    if (isDesktop()) {
      closeNav();
      let storedCollapsed = false;
      try {
        storedCollapsed = window.localStorage.getItem(desktopStorageKey) === "1";
      } catch (error) {
        storedCollapsed = isCollapsed();
      }
      document.body.classList.remove("nav-open");
      setDesktopCollapsed(storedCollapsed);
      return;
    }

    document.body.classList.remove("sidebar-collapsed");
    if (desktopToggle) {
      desktopToggle.textContent = "Contraer menu";
      desktopToggle.setAttribute("aria-pressed", "false");
    }
  };

  toggle?.addEventListener("click", () => {
    if (document.body.classList.contains("nav-open")) {
      closeNav();
    } else {
      openNav();
    }
  });

  desktopToggle?.addEventListener("click", () => {
    setDesktopCollapsed(!isCollapsed());
  });

  overlay?.addEventListener("click", closeNav);
  window.addEventListener("resize", syncNavMode);
  syncNavMode();
})();
