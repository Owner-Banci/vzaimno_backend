const screenData = {
  map: {
    src: "assets/screen-map.png",
    alt: "Карта задач в приложении Vzaimno"
  },
  "new-task": {
    src: "assets/screen-new-task.png",
    alt: "Создание нового объявления в приложении Vzaimno"
  },
  preview: {
    src: "assets/screen-preview.png",
    alt: "Предпросмотр объявления в приложении Vzaimno"
  },
  route: {
    src: "assets/screen-route.png",
    alt: "Маршрут с задачами в приложении Vzaimno"
  },
  ads: {
    src: "assets/screen-ads.png",
    alt: "Мои объявления в приложении Vzaimno"
  },
  chats: {
    src: "assets/screen-chats.png",
    alt: "Чаты в приложении Vzaimno"
  },
  profile: {
    src: "assets/screen-profile.png",
    alt: "Профиль пользователя в приложении Vzaimno"
  }
};

const toast = document.querySelector("[data-toast]");
let toastTimer;

function showToast(message) {
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.classList.remove("is-visible");
  }, 2600);
}

document.querySelectorAll("[data-placeholder]").forEach((element) => {
  element.addEventListener("click", (event) => {
    const href = element.getAttribute("href") || "";
    if (href.startsWith("mailto:")) return;
    event.preventDefault();
    showToast("Это заглушка. Ссылку подключим перед запуском.");
  });
});

const header = document.querySelector("[data-header]");
function syncHeader() {
  if (!header) return;
  header.classList.toggle("is-scrolled", window.scrollY > 18);
}
syncHeader();
window.addEventListener("scroll", syncHeader, { passive: true });

const revealItems = document.querySelectorAll(".reveal");
if ("IntersectionObserver" in window) {
  const revealObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          revealObserver.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.12, rootMargin: "0px 0px -8% 0px" }
  );

  revealItems.forEach((item) => revealObserver.observe(item));
} else {
  revealItems.forEach((item) => item.classList.add("is-visible"));
}

const screenImage = document.querySelector("[data-screen-image]");
const screenButtons = Array.from(document.querySelectorAll("[data-screen]"));
const screenOrder = screenButtons.map((button) => button.dataset.screen);
let activeScreenIndex = 0;
let screenTimer;

function setScreen(screenKey, fromUser = false) {
  const next = screenData[screenKey];
  if (!next || !screenImage) return;

  activeScreenIndex = Math.max(0, screenOrder.indexOf(screenKey));
  screenButtons.forEach((button) => {
    const isActive = button.dataset.screen === screenKey;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-selected", String(isActive));
  });

  screenImage.style.opacity = "0";
  window.setTimeout(() => {
    screenImage.src = next.src;
    screenImage.alt = next.alt;
    screenImage.style.opacity = "1";
  }, 130);

  if (fromUser) startScreenRotation();
}

function startScreenRotation() {
  clearInterval(screenTimer);
  screenTimer = setInterval(() => {
    if (!screenOrder.length) return;
    activeScreenIndex = (activeScreenIndex + 1) % screenOrder.length;
    setScreen(screenOrder[activeScreenIndex]);
  }, 4300);
}

screenButtons.forEach((button) => {
  button.setAttribute("role", "tab");
  button.addEventListener("click", () => setScreen(button.dataset.screen, true));
});

if (screenImage) {
  screenImage.style.transition = "opacity 180ms ease";
  startScreenRotation();
}

if (window.matchMedia("(pointer: fine)").matches) {
  document.querySelectorAll("[data-tilt]").forEach((card) => {
    card.addEventListener("pointermove", (event) => {
      const rect = card.getBoundingClientRect();
      const x = (event.clientX - rect.left) / rect.width - 0.5;
      const y = (event.clientY - rect.top) / rect.height - 0.5;
      card.style.transform = `rotateX(${y * -5}deg) rotateY(${x * 7}deg) translateY(-6px)`;
    });

    card.addEventListener("pointerleave", () => {
      card.style.transform = "";
    });
  });
}
