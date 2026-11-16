import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "./theme/ThemeProvider";
import App from "./App";
import "./index.css";

import { queryClient } from "./queryClient";
import { startPwa } from "./pwa";

// M72: installable PWA — register the app-shell service worker before the
// first render (production + secure context only, docs/09 §Installable PWA).
startPwa();

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <App />
      </ThemeProvider>
    </QueryClientProvider>
  </StrictMode>,
);
