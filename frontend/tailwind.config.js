/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Semantic palette backed by CSS variables defined in src/index.css.
        // "midnight terminal" (ui-contract): deep near-black surfaces, one
        // electric cyan accent, amber as the warm secondary, green/red kept
        // strictly semantic (gains/losses).
        surface: "rgb(var(--color-surface) / <alpha-value>)",
        "surface-raised": "rgb(var(--color-surface-raised) / <alpha-value>)",
        border: "rgb(var(--color-border) / <alpha-value>)",
        // M185 hairline rule (docs/47 §5): the one token the Ledger wave
        // adds — in-component 1px rules (state rows, truth bar, table rules).
        // Decorative (WCAG 1.4.11 exempt), hence its own name distinct from
        // the card border above.
        rule: "rgb(var(--color-rule) / <alpha-value>)",
        // Form-control boundary (WCAG 1.4.11: 3:1 where the border IS the
        // component boundary). `border` above stays the decorative hairline.
        "border-control": "rgb(var(--color-border-control) / <alpha-value>)",
        text: "rgb(var(--color-text) / <alpha-value>)",
        muted: "rgb(var(--color-muted) / <alpha-value>)",
        accent: "rgb(var(--color-accent) / <alpha-value>)",
        // Lighter cyan used for accent gradients + chart fills.
        "accent-soft": "rgb(var(--color-accent-soft) / <alpha-value>)",
        // Ink color that stays readable on top of the accent.
        "accent-ink": "rgb(var(--color-accent-ink) / <alpha-value>)",
        // Low-alpha overlay base for chips/inputs/hovers (white in dark, ink
        // in light) + the opaque modal scrim wash.
        veil: "rgb(var(--color-veil) / <alpha-value>)",
        // Themed black/ink veil used for the modal scrim (FPages #9).
        overlay: "rgb(var(--color-overlay) / <alpha-value>)",
        scrim: "var(--scrim)",
        warn: "rgb(var(--color-warn) / <alpha-value>)",
        gain: "rgb(var(--color-gain) / <alpha-value>)",
        danger: "rgb(var(--color-danger) / <alpha-value>)",
      },
      boxShadow: {
        // Effect shadows are fully token-driven (src/index.css): dark emits
        // neon glows; light "daybreak" swaps them for elevation + rings.
        glow: "var(--shadow-glow)",
        "glow-sm": "var(--shadow-glow-sm)",
        "glow-danger": "var(--shadow-glow-danger)",
        card: "var(--shadow-card)",
      },
      borderRadius: {
        xl: "0.75rem",
        "2xl": "1rem",
      },
      animation: {
        shimmer: "shimmer 1.8s linear infinite",
        "drift-a": "drift-a 26s ease-in-out infinite alternate",
        "drift-b": "drift-b 34s ease-in-out infinite alternate",
        "pulse-ring": "pulse-ring 1.6s cubic-bezier(0.22, 1, 0.36, 1) infinite",
        rise: "rise 0.45s cubic-bezier(0.22, 1, 0.36, 1) both",
      },
    },
  },
  plugins: [],
};
