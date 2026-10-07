import React from "react";
import ReactDOM from "react-dom/client";
import "@radix-ui/themes/styles.css";
import "./styles.css";
import "./collection.css";
import "./collection-edit-merge.css";
import { App } from "./App";
import { bootstrapShareSession } from "./share-bootstrap";
import { registerShareWorker } from "./share-registration";

bootstrapShareSession();
void registerShareWorker();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
