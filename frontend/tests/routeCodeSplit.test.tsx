// M49 route code-splitting contract (docs/09 "Route loading & code-splitting").
//
// Two channels pin it:
//  1. source contract — every file in src/pages/ is wired through the lazy
//     `route()` helper in App.tsx (none eagerly imported, none missing), so a
//     newly added page cannot silently rejoin the entry chunk;
//  2. behaviour — while the dynamic import is pending the route paints the
//     documented shimmer fallback (never a blank frame, never spinner text),
//     and a rejected chunk renders the error panel instead of nothing.
import { readFileSync, readdirSync } from "node:fs";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { route } from "../src/App";
import { RouteFallback } from "../src/components/ui/RouteFallback";

describe("route code-splitting source contract", () => {
  const source = readFileSync("src/App.tsx", "utf8");
  const pageModules = readdirSync("src/pages")
    .filter((file) => file.endsWith(".tsx"))
    .map((file) => file.replace(/\.tsx$/, ""))
    .sort();
  const dynamicImports = [...source.matchAll(/import\("\.\/pages\/([A-Za-z]+)"\)/g)]
    .map((match) => match[1])
    .sort();

  it("dynamic-imports exactly the page modules on disk", () => {
    expect(pageModules.length).toBeGreaterThanOrEqual(21);
    expect(dynamicImports).toEqual(pageModules);
  });

  it("imports no page module eagerly", () => {
    expect(source).not.toMatch(/^import .* from "\.\/pages\//m);
  });

  it("routes every page module through the lazy Suspense helper", () => {
    // 22 route entries -> 22 `route(pages.x)` elements (one per page module),
    // and the helper itself wraps the lazy component in Suspense + fallback.
    expect(source.match(/element: route\(pages\./g)).toHaveLength(pageModules.length);
    expect(source).toMatch(/<Suspense fallback=\{<RouteFallback \/>\}>/);
    expect(source).toMatch(/const Page = lazy\(load\)/);
    // Shell and auth stay eager (docs/09).
    expect(source).toMatch(/element: <AppShell \/>/);
    expect(source).toMatch(/element: <RequireAuth \/>/);
  });
});

describe("route loading behaviour", () => {
  it("renders the documented shimmer fallback while the chunk loads", async () => {
    let finishImport!: (mod: { default: () => JSX.Element }) => void;
    const chunk = new Promise<{ default: () => JSX.Element }>((resolve) => {
      finishImport = resolve;
    });

    render(route(() => chunk));

    const fallback = screen.getByRole("status", { name: "Loading page" });
    expect(fallback).toBeTruthy();
    expect(fallback.querySelector(".skeleton")).toBeTruthy();

    finishImport({ default: () => <div>page content</div> });
    expect(await screen.findByText("page content")).toBeTruthy();
    expect(screen.queryByRole("status", { name: "Loading page" })).toBeNull();
  });

  it("never shows spinner text in the fallback", () => {
    render(<RouteFallback />);
    expect(screen.queryByText(/loading/i)).toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("shows the reload action when a route chunk fails to load", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      render(
        route(() =>
          Promise.reject(new Error("Failed to fetch dynamically imported module")),
        ),
      );

      const panel = await screen.findByTestId("route-load-error");
      expect(panel).toBeTruthy();
      expect(screen.getByText(/Failed to fetch dynamically imported module/)).toBeTruthy();
      expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
      expect(screen.queryByRole("status", { name: "Loading page" })).toBeNull();
    } finally {
      consoleError.mockRestore();
    }
  });
});
