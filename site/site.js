// leasyd.com landing page: the hero's screenshot carousel, the product rows' arrows, a typing
// question in the MCP card, and the e-mail boxes (to the app's sign-up, e-mail filled in).
(function () {
  const still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Hero carousel
  const show = document.getElementById("showcase");
  if (show) {
    const imgs = show.querySelectorAll(".win img"), dots = show.querySelectorAll(".dots .d"), cap = document.getElementById("caption");
    let i = 0, timer;
    const go = (n) => {
      i = (n + imgs.length) % imgs.length;
      imgs.forEach((im, k) => { im.classList.toggle("on", k === i); if (k === i) im.loading = "eager"; });
      dots.forEach((d, k) => d.classList.toggle("on", k === i));
      cap.textContent = imgs[i].dataset.caption || "";
    };
    const auto = () => { clearInterval(timer); if (!still) timer = setInterval(() => go(i + 1), 4500); };
    dots.forEach((d, k) => d.addEventListener("click", () => { go(k); auto(); }));
    document.getElementById("prev").addEventListener("click", () => { go(i - 1); auto(); });
    document.getElementById("next").addEventListener("click", () => { go(i + 1); auto(); });
    auto();
  }

  // Product rows: arrows scroll by one card
  document.querySelectorAll(".psec").forEach((sec) => {
    const row = sec.querySelector(".cards");
    const by = (dir) => row.scrollBy({ left: dir * (row.querySelector(".c").offsetWidth + 18), behavior: still ? "auto" : "smooth" });
    sec.querySelector(".prev").addEventListener("click", () => by(-1));
    sec.querySelector(".next").addEventListener("click", () => by(1));
  });

  // The MCP card types questions
  document.querySelectorAll(".typed").forEach((el) => {
    const lines = (el.dataset.lines || "").split("|");
    if (still) { el.textContent = lines[0]; return; }
    let l = 0, c = 0, back = false;
    const tick = () => {
      const s = lines[l];
      if (!back) { c++; if (c > s.length) { back = true; setTimeout(tick, 1800); return; } }
      else { c -= 2; if (c <= 0) { c = 0; back = false; l = (l + 1) % lines.length; } }
      el.textContent = s.slice(0, c);
      setTimeout(tick, back ? 18 : 42);
    };
    tick();
  });

  // E-mail boxes: open the app's sign-up with the e-mail filled in
  document.querySelectorAll("form.signup").forEach((f) => f.addEventListener("submit", (e) => {
    e.preventDefault();
    const email = (f.querySelector("input").value || "").trim();
    window.location.assign("https://app.leasyd.com/#/signup" + (email ? "?email=" + encodeURIComponent(email) : ""));
  }));
})();
