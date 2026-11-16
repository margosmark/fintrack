import { QueryClient } from "@tanstack/react-query";

/**
 * App-wide QueryClient singleton (docs/33 S2a): shared by
 * `QueryClientProvider` (main.tsx) and non-hook code such as the client-side
 * report loaders in `api/reports.ts`, so input datasets (rows, categories,
 * accounts, trades, fx) are fetched once and invalidated together.
 */
export const queryClient = new QueryClient();
