import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router";

import { AppShell } from "@/components/app-shell";
import { InvestigationDetailPage } from "@/pages/investigation-detail";
import { InvestigationsPage } from "@/pages/investigations";
import { NewInvestigationPage } from "@/pages/new-investigation";
import { NotFoundPage } from "@/pages/not-found";

import "./index.css";

const router = createBrowserRouter([
  {
    element: <AppShell />,
    children: [
      { index: true, element: <NewInvestigationPage /> },
      { path: "investigations", element: <InvestigationsPage /> },
      { path: "investigations/:id", element: <InvestigationDetailPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
