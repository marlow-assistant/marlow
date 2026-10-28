import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import "./app/styles.css";
import { KV_TEXT_SIZE, kvGet } from "./lib/storage";
import { applyTextSize, parseTextSize } from "./lib/textSize";

// Not awaited: the chat itself waits on IndexedDB for the face, so by the time
// the transcript lays out this has landed; only the pairing screen could show
// the default size for a moment.
void kvGet<unknown>(KV_TEXT_SIZE)
  .then((v) => applyTextSize(parseTextSize(v)))
  .catch(() => {});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
