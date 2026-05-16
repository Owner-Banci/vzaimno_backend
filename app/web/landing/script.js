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

const guideData = {
  find: {
    title: "Как найти задание",
    lead: "Начните с карты: на ней видно, какие задания находятся рядом и какие можно взять по пути.",
    steps: [
      {
        title: "Откройте карту",
        text: "Первый экран показывает задания рядом. Масштабируйте карту, возвращайтесь к своей геопозиции и переключайтесь между картой и списком.",
        src: "assets/guide-map-overview.png",
        alt: "Карта заданий в приложении Vzaimno",
        focus: { x: 3, y: 3.4, w: 78, h: 6.4 }
      },
      {
        title: "Управляйте картой",
        text: "Кнопки справа помогают приблизить карту, отдалить ее и быстро вернуться к вашему местоположению.",
        src: "assets/guide-map-overview.png",
        alt: "Кнопки управления картой в приложении Vzaimno",
        focus: { x: 83, y: 35, w: 12, h: 31 }
      },
      {
        title: "Откройте список",
        text: "Если удобнее читать объявления подряд, переключитесь в список: там видны названия, адреса, бюджет и быстрые действия.",
        src: "assets/guide-map-list.png",
        alt: "Список заданий рядом в приложении Vzaimno",
        focus: { x: 4, y: 16.8, w: 92, h: 57 }
      },
      {
        title: "Откликнитесь",
        text: "В карточке задания проверьте адреса, время и бюджет. Можно отправить быстрый отклик или предложить свою цену.",
        src: "assets/guide-task-respond.png",
        alt: "Детали задания и кнопки отклика в приложении Vzaimno",
        focus: { x: 3.5, y: 78.5, w: 93, h: 17.8 }
      }
    ]
  },
  create: {
    title: "Как создать объявление",
    lead: "Создание начинается с понятного сценария: приложение само оставит только подходящие поля.",
    steps: [
      {
        title: "Нажмите «Создать объявление»",
        text: "Перейдите во вкладку «Объявления» и нажмите большую кнопку внизу экрана.",
        src: "assets/guide-my-ads.png",
        alt: "Экран моих объявлений с кнопкой создания",
        focus: { x: 4, y: 81, w: 92, h: 6.8 }
      },
      {
        title: "Выберите главный сценарий",
        text: "Сначала выберите, что нужно сделать: забрать, купить, перенести, подвезти, помощь от профи или другое.",
        src: "assets/guide-new-draft.png",
        alt: "Выбор главного сценария нового объявления",
        focus: { x: 8, y: 24, w: 84, h: 41 }
      },
      {
        title: "Уточните предмет",
        text: "После главного сценария выберите, что именно нужно забрать или купить: продукты, документы, техника, хрупкая вещь и так далее.",
        src: "assets/guide-new-scenario.png",
        alt: "Уточнение типа предмета в новом объявлении",
        focus: { x: 8, y: 78, w: 72, h: 16 }
      },
      {
        title: "Заполните маршрут",
        text: "Укажите, откуда забрать и куда привезти. Переключатели помогают описать точку простыми словами: ПВЗ, адрес, человек, офис или другое.",
        src: "assets/guide-new-address.png",
        alt: "Заполнение адресов в новом объявлении",
        focus: { x: 7, y: 31, w: 86, h: 61 }
      },
      {
        title: "Проверьте и отправьте",
        text: "В предпросмотре видно, как объявление будет выглядеть для других пользователей. Если все верно, отправьте его на проверку.",
        src: "assets/guide-new-preview.png",
        alt: "Предпросмотр объявления перед отправкой",
        focus: { x: 4, y: 88.5, w: 92, h: 7 }
      }
    ]
  },
  manage: {
    title: "Как выбрать исполнителя",
    lead: "Когда люди откликаются на объявление, заказчик видит заявки и выбирает подходящего исполнителя.",
    steps: [
      {
        title: "Откройте свое объявление",
        text: "Во вкладке «Мои объявления» видны активные, ожидающие и архивные задачи. Нажмите нужную карточку.",
        src: "assets/guide-my-ads.png",
        alt: "Список моих объявлений",
        focus: { x: 4, y: 30, w: 92, h: 16 }
      },
      {
        title: "Посмотрите статус",
        text: "В деталях видно, назначен ли исполнитель, сколько откликов пришло и принимает ли объявление новые заявки.",
        src: "assets/guide-task-details.png",
        alt: "Детали объявления со статусом и откликами",
        focus: { x: 4, y: 35, w: 92, h: 20 }
      },
      {
        title: "Откройте отклики",
        text: "На вкладке «Отклики» видно имя, город, рейтинг, цену и причину отклика. Принятый исполнитель отмечается отдельным статусом.",
        src: "assets/guide-responses.png",
        alt: "Отклики исполнителей по объявлению",
        focus: { x: 5, y: 38, w: 90, h: 28 }
      },
      {
        title: "Продолжите в чате",
        text: "После согласования чат открыт для заказчика и исполнителя. Там удобно уточнить детали выполнения.",
        src: "assets/guide-chat-active.png",
        alt: "Чат заказчика и исполнителя",
        focus: { x: 3.5, y: 24.5, w: 93, h: 18 }
      }
    ]
  },
  route: {
    title: "Как следить за маршрутом",
    lead: "Маршрут помогает заказчику видеть свои задачи, а исполнителю понимать путь и этапы выполнения.",
    steps: [
      {
        title: "Откройте вкладку «Маршрут»",
        text: "Переключатель сверху показывает режим исполнителя или заказчика. На карте видна точка задачи и кнопка открытия маршрута в картах.",
        src: "assets/guide-route-customer.png",
        alt: "Вкладка маршрута в приложении Vzaimno",
        focus: { x: 4, y: 6.8, w: 92, h: 50 }
      },
      {
        title: "Смотрите этапы задачи",
        text: "Карточка задачи показывает стоимость, статус и этапы: принято, в пути, на месте и следующие шаги.",
        src: "assets/guide-route-customer.png",
        alt: "Этапы выполнения задачи на маршруте",
        focus: { x: 4.5, y: 67.5, w: 91, h: 22 }
      },
      {
        title: "Откройте внешний маршрут",
        text: "Если нужен подробный путь, откройте маршрут в картах и используйте привычную навигацию.",
        src: "assets/guide-external-route.png",
        alt: "Маршрут во внешнем картографическом приложении",
        focus: { x: 2, y: 3, w: 96, h: 64 }
      }
    ]
  },
  chat: {
    title: "Как пользоваться чатами",
    lead: "Все переписки по задачам собираются в одном месте: обычные чаты, согласованные задания и поддержка.",
    steps: [
      {
        title: "Откройте список переписок",
        text: "Во вкладке «Чаты» отображаются все диалоги: имя пользователя, название задачи, последнее сообщение и дата.",
        src: "assets/guide-chats-list.png",
        alt: "Список переписок в приложении Vzaimno",
        focus: { x: 4, y: 6.5, w: 92, h: 78 }
      },
      {
        title: "Напишите сообщение",
        text: "Внутри чата можно обмениваться сообщениями, прикреплять изображение и уточнять детали выполнения.",
        src: "assets/guide-chat-active.png",
        alt: "Активная переписка по заданию",
        focus: { x: 18, y: 91, w: 64, h: 5.8 }
      },
      {
        title: "Обратитесь в поддержку",
        text: "Если нужна помощь по приложению или спорной ситуации, отдельный диалог поддержки находится рядом с обычными чатами.",
        src: "assets/guide-chat-support.png",
        alt: "Чат поддержки Vzaimno",
        focus: { x: 8, y: 21, w: 84, h: 14 }
      }
    ]
  },
  profile: {
    title: "Как настроить профиль",
    lead: "Профиль помогает другим пользователям понимать, с кем они взаимодействуют, и хранит основные настройки.",
    steps: [
      {
        title: "Проверьте карточку профиля",
        text: "Здесь видны имя, контакт, рейтинг, оценки, выполненные и отмененные задачи.",
        src: "assets/guide-profile.png",
        alt: "Профиль пользователя Vzaimno",
        focus: { x: 4.5, y: 10.5, w: 91, h: 30 }
      },
      {
        title: "Нажмите «Изм.»",
        text: "Кнопка редактирования открывает экран, где можно обновить имя, город, описание и удобный адрес.",
        src: "assets/guide-profile.png",
        alt: "Кнопка редактирования профиля",
        focus: { x: 76, y: 14.5, w: 16, h: 4.5 }
      },
      {
        title: "Заполните данные о себе",
        text: "Чем понятнее профиль, тем проще другим пользователям выбрать вас для задачи или принять ваш отклик.",
        src: "assets/guide-profile-edit.png",
        alt: "Редактирование профиля пользователя",
        focus: { x: 8, y: 17.5, w: 84, h: 23 }
      },
      {
        title: "Сохраните изменения",
        text: "После редактирования нажмите «Сохранить», чтобы обновленные данные начали отображаться в приложении.",
        src: "assets/guide-profile-edit.png",
        alt: "Сохранение изменений профиля",
        focus: { x: 4, y: 90.5, w: 92, h: 6.6 }
      }
    ]
  }
};

document.body.classList.add("is-loading");

const appLoader = document.querySelector(".app-loader");
function hideAppLoader() {
  if (!appLoader) return;
  appLoader.classList.add("is-hidden");
  document.body.classList.remove("is-loading");
  window.setTimeout(() => appLoader.remove(), 520);
}

if (document.readyState === "complete") {
  window.setTimeout(hideAppLoader, 520);
} else {
  window.addEventListener("load", () => window.setTimeout(hideAppLoader, 520), { once: true });
  window.setTimeout(hideAppLoader, 2200);
}

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

const guideRoot = document.querySelector("[data-guide]");
if (guideRoot) {
  const guideTabs = Array.from(guideRoot.querySelectorAll("[data-guide-tab]"));
  const guideTitle = guideRoot.querySelector("[data-guide-title]");
  const guideLead = guideRoot.querySelector("[data-guide-lead]");
  const guideCount = guideRoot.querySelector("[data-guide-count]");
  const guideSteps = guideRoot.querySelector("[data-guide-steps]");
  const guideImage = guideRoot.querySelector("[data-guide-image]");
  const guidePhone = guideRoot.querySelector(".guide-phone");
  const guideFocus = guideRoot.querySelector("[data-guide-focus]");
  const guideDot = guideRoot.querySelector("[data-guide-dot]");
  const guideStepTitle = guideRoot.querySelector("[data-guide-step-title]");
  const guideStepText = guideRoot.querySelector("[data-guide-step-text]");
  let activeGuideKey = "find";
  let activeGuideStep = 0;

  function applyGuideFocus(focus) {
    if (!guidePhone || !focus) return;
    guidePhone.style.setProperty("--focus-x", `${focus.x}%`);
    guidePhone.style.setProperty("--focus-y", `${focus.y}%`);
    guidePhone.style.setProperty("--focus-w", `${focus.w}%`);
    guidePhone.style.setProperty("--focus-h", `${focus.h}%`);
  }

  function renderGuideSteps() {
    const activeGuide = guideData[activeGuideKey];
    if (!guideSteps || !activeGuide) return;
    guideSteps.innerHTML = "";

    activeGuide.steps.forEach((step, index) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      const number = document.createElement("span");
      const body = document.createElement("span");
      const title = document.createElement("span");
      const text = document.createElement("span");

      button.type = "button";
      button.className = "guide-step-button";
      button.dataset.guideStep = String(index);
      button.setAttribute("aria-selected", String(index === activeGuideStep));

      number.className = "guide-step-number";
      number.textContent = String(index + 1);

      body.className = "guide-step-body";
      title.className = "guide-step-title";
      title.textContent = step.title;
      text.className = "guide-step-text";
      text.textContent = step.text;

      body.append(title, text);
      button.append(number, body);
      item.append(button);
      guideSteps.append(item);
    });
  }

  function setGuideStep(index) {
    const activeGuide = guideData[activeGuideKey];
    if (!activeGuide) return;
    const nextIndex = Math.max(0, Math.min(index, activeGuide.steps.length - 1));
    const step = activeGuide.steps[nextIndex];
    activeGuideStep = nextIndex;

    guideRoot.querySelectorAll("[data-guide-step]").forEach((button) => {
      const isActive = Number(button.dataset.guideStep) === activeGuideStep;
      button.classList.toggle("active", isActive);
      button.setAttribute("aria-selected", String(isActive));
    });

    if (guideCount) guideCount.textContent = `${activeGuideStep + 1} из ${activeGuide.steps.length}`;
    if (guideStepTitle) guideStepTitle.textContent = step.title;
    if (guideStepText) guideStepText.textContent = step.text;
    if (guideDot) guideDot.textContent = String(activeGuideStep + 1);
    if (guideFocus) guideFocus.hidden = false;
    applyGuideFocus(step.focus);

    if (guideImage) {
      guideImage.style.opacity = "0";
      window.setTimeout(() => {
        guideImage.src = step.src;
        guideImage.alt = step.alt;
        guideImage.style.opacity = "1";
      }, 120);
    }
  }

  function setGuideTab(key) {
    const nextGuide = guideData[key];
    if (!nextGuide) return;
    activeGuideKey = key;
    activeGuideStep = 0;

    guideTabs.forEach((button) => {
      const isActive = button.dataset.guideTab === key;
      button.classList.toggle("active", isActive);
      button.setAttribute("aria-selected", String(isActive));
    });

    if (guideTitle) guideTitle.textContent = nextGuide.title;
    if (guideLead) guideLead.textContent = nextGuide.lead;
    renderGuideSteps();
    setGuideStep(0);
  }

  guideTabs.forEach((button) => {
    button.setAttribute("role", "tab");
    button.addEventListener("click", () => setGuideTab(button.dataset.guideTab));
  });

  guideSteps?.addEventListener("click", (event) => {
    const stepButton = event.target.closest("[data-guide-step]");
    if (!stepButton) return;
    setGuideStep(Number(stepButton.dataset.guideStep));
  });

  if (guideImage) {
    guideImage.style.transition = "opacity 180ms ease";
  }
  setGuideTab(activeGuideKey);
}

const screenImage = document.querySelector("[data-screen-image]");
const screenButtons = Array.from(document.querySelectorAll("[data-screen]"));
const screenOrder = Array.from(new Set(screenButtons.map((button) => button.dataset.screen)));
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
