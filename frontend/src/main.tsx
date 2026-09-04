import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";

// Self-hosted rather than fetched from Google Fonts: no third-party request on
// first paint, and the app keeps its typography offline.
import "@fontsource-variable/inter/index.css";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/layout.css";
import "./styles/components.css";

const container = document.getElementById("root");
if (!container) throw new Error("#root not found in index.html");

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
