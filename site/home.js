// Landing page: the Products list opens one item at a time and shows its screenshot.
(function () {
  const list = document.getElementById("prod-list"), img = document.getElementById("prod-img");
  if (!list || !img) return;
  const items = list.querySelectorAll("details");
  items.forEach((d) => d.addEventListener("toggle", () => {
    if (!d.open) return;
    items.forEach((o) => { if (o !== d) o.open = false; });
    img.src = d.dataset.img; img.alt = d.dataset.alt || "";
  }));
  // The stage scales the board to the page's width.
  const stage = document.querySelector(".stage"), board = document.getElementById("board");
  const fit = () => { if (stage && board) board.style.setProperty("--k", Math.min(1, stage.clientWidth / 1600).toFixed(3)); };
  fit(); window.addEventListener("resize", fit);
})();
