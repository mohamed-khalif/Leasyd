import { createRoot } from "react-dom/client";
import { App } from "./App";
import { loadConfig } from "./config";
import "./styles.css";

try {
  const saved = localStorage.getItem("leasyd.theme");
  if (saved === "light") document.documentElement.dataset.theme = "light";
} catch { /* storage unavailable: dark */ }

loadConfig().then(() => createRoot(document.getElementById("root")!).render(<App />));
