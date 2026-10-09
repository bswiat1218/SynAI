import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import "../index.css";

document.documentElement.classList.add("dark");
document.documentElement.dataset.theme = "slate-dark";

afterEach(() => {
  cleanup();
});
